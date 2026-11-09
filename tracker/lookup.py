"""
Автопоиск скрытого владельца через бота поиска (LOOKUP_BOT_USERNAME, например funstat / infostat).

Лот со скрытым владельцем (видно только имя) перед отправкой в канал проходит поиск:

  1. аккаунт поиска (LOOKUP_SESSIONS или первый рабочий) пишет боту «/human <имя продавца>»;
  2. ждёт ответ (бот может сначала прислать «ищу…» и потом отредактировать сообщение — это учтено);
  3. из ответа «01. Имя (@username)» достаёт кандидатов: @username, t.me/…, tg://user?id=…, ID;
  4. у каждого кандидата смотрит открытые NFT в профиле (payments.getSavedStarGifts) и ищет
     ИМЕННО этот лот по slug / id. Владелец найден только если NFT виден у него в профиле.

Найденный владелец становится обычным продавцом лота (@username, проверка профиля, «Написать»
в ЛС). Не найден и LOOKUP_REQUIRED=true — лот не отправляется.

На одном аккаунте — один разговор с ботом за раз; несколько аккаунтов поиска работают параллельно.
Ответы бота кэшируются по имени, чтобы не тратить его лимиты на одно и то же имя.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from telethon import types
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.functions.payments import GetSavedStarGiftsRequest

from config import settings
from utils.logger import get_logger, llog

log = get_logger("lookup")

_USERNAME_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
_TME_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#]|$)", re.I)
_ID_NEAR_RE = re.compile(r"\b(?:id|ид)\s*[:#№]?\s*(\d{5,12})\b", re.I)
_SERVICE_NAMES = {"joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "nft", "boost",
                  "c", "s", "iv", "m", "contact", "addlist", "invoice", "giftcode"}
_WAIT_WORDS = ("ищу", "поиск", "подожд", "загруж", "search", "wait", "loading", "⏳", "🔎")
# Rate-limit / слишком-часто сообщения: не считаем за «не найдено», не кешируем негативно
_RATE_LIMIT_WORDS = ("слишком часто", "too fast", "slow down", "rate limit", "подожди", "не спеши",
                     "лимит", "ограниче", "too many requests", "флуд", "flood",
                     "попробуйте позже", "попробуй позже", "try later", "try again later",
                     "cooldown", "кулдаун", "retry", "повторите позже")
_MAX_PENDING = int(os.environ.get("LOOKUP_MAX_PENDING") or "5000")   # не больше N лотов в очереди на поиск
_CACHE_TTL = 3600.0               # кандидаты по одному имени — час
_NEG_CACHE_TTL = 3600.0           # «не найдено» по имени — час (не долбим funstat повторно)
# Как долго (seconds) сессия помечена "не может писать боту" после "No user has / USERNAME_INVALID"
# и т.п. ответов, прежде чем попробовать снова. Раньше было 600s — слишком жёстко: funstat мог
# просто пропустить одно сообщение (сетевой glitch), а мы выводили аккаунт из пула на 10 минут.
# Для кейса YOU_BLOCKED_USER используется отдельный таймер 6 часов — пользователь должен
# разблокировать бота вручную в этом аккаунте.
_UNRESOLVABLE_COOLDOWN = 120.0
_YOU_BLOCKED_COOLDOWN = 6 * 3600.0
_LINE_RE = re.compile(r"\((?:@([A-Za-z][A-Za-z0-9_]{3,31})|id\s*(\d{3,12}))\)", re.I)
_LINE_PREFIX_RE = re.compile(r"^[\s\d.)\-#№]*(?:[^\w\s]+\s*)*")
_CAPTCHA_WORDS = ("captcha", "kapch", "капч", "rasmga", "click the button", "looks like the picture")


@dataclass
class Candidate:
    username: str | None = None
    user_id: int | None = None
    name: str | None = None              # имя, которое бот поиска показал рядом с юзом

    def key(self) -> str:
        return f"@{self.username.lower()}" if self.username else f"id{self.user_id}"


@dataclass
class FoundOwner:
    user_id: int
    username: str | None
    name: str | None
    premium: bool | None = None
    session: str | None = None          # аккаунт, который видит этого пользователя (есть access_hash)
    from_memory: bool = False           # найден из нашей базы — enrich выполнить обычным путём
    bot: str | None = None              # какой lookup-бот реально ответил (primary или fallback из LOOKUP_BOTS).
                                        # queue.py использует это как owner_found_via, иначе был бы указан
                                        # primary даже когда ответил запасной.


def _norm_name(s: str | None) -> str:
    return " ".join((s or "").casefold().split())


def _from_url(url: str) -> Candidate | None:
    url = (url or "").strip()
    if not url:
        return None
    low = url.lower()
    if low.startswith("tg://"):
        q = parse_qs(urlparse(url).query)
        for k in ("id", "user_id"):
            v = (q.get(k) or [None])[0]
            if v and v.isdigit():
                return Candidate(user_id=int(v))
        dom = (q.get("domain") or [None])[0]
        if dom and dom.lower() not in _SERVICE_NAMES:
            return Candidate(username=dom)
        return None
    m = _TME_RE.search(url)
    if m and m.group(1).lower() not in _SERVICE_NAMES and not m.group(1).lower().endswith("bot"):
        return Candidate(username=m.group(1))
    return None


def parse_candidates(messages: list[Any], bot_username: str) -> list[Candidate]:
    """Кандидаты из ответа бота поиска — в порядке появления, без дублей.
    Формат ответа у разных ботов разный, поэтому берём всё, что похоже на аккаунт:
    упоминания по ID, ссылки на профиль, @username, «ID: 123456» в тексте, URL-кнопки."""
    out: list[Candidate] = []
    seen: set[str] = set()
    skip = {bot_username.lower().lstrip("@")}

    def add(c: Candidate | None) -> None:
        if c is None:
            return
        if c.username and (c.username.lower() in skip or c.username.lower().endswith("bot")):
            return                       # сам бот поиска, его каналы-боты и прочие боты — не владельцы
        k = c.key()
        if k not in seen:
            seen.add(k)
            out.append(c)

    for msg in messages:
        text = getattr(msg, "message", None) or ""
        try:
            pairs = msg.get_entities_text() if getattr(msg, "entities", None) else []
        except Exception:
            pairs = []
        for ent, etext in pairs:
            if isinstance(ent, types.MessageEntityMentionName):
                # имя-ссылка на профиль: Telegram присылает этого юзера вместе с сообщением (с access_hash),
                # поэтому его можно открыть БЕЗ resolveUsername — бесплатно и без риска FloodWait
                add(Candidate(user_id=ent.user_id, name=etext.strip() or None))
            elif isinstance(ent, types.MessageEntityTextUrl):
                c = _from_url(ent.url)
                if c is not None and c.user_id and not c.username:
                    c.name = etext.strip() or None
                add(c)
            elif isinstance(ent, types.MessageEntityUrl):
                add(_from_url(etext))
            elif isinstance(ent, types.MessageEntityMention):
                add(Candidate(username=etext.lstrip("@")))
            elif isinstance(ent, (types.MessageEntityCode, types.MessageEntityPre)):
                s = etext.strip()
                if s.isdigit() and 5 <= len(s) <= 12:
                    add(Candidate(user_id=int(s)))
        for m in _USERNAME_RE.finditer(text):
            add(Candidate(username=m.group(1)))
        for m in _TME_RE.finditer(text):
            if m.group(1).lower() not in _SERVICE_NAMES:
                add(Candidate(username=m.group(1)))
        for m in _ID_NEAR_RE.finditer(text):
            add(Candidate(user_id=int(m.group(1))))
        markup = getattr(msg, "reply_markup", None)
        for row in getattr(markup, "rows", None) or []:
            for b in getattr(row, "buttons", None) or []:
                # Layer 229: KeyboardInlineButton(type=InlineButtonTypeUrl/UserProfile);
                # старые слои: KeyboardButtonUrl(url) / KeyboardButtonUserProfile(user_id)
                bt = getattr(b, "type", None)
                src = bt if bt is not None and not isinstance(bt, str) else b
                url = getattr(src, "url", None)
                if isinstance(url, str):
                    add(_from_url(url))
                uid = getattr(src, "user_id", None)
                if isinstance(uid, int):
                    add(Candidate(user_id=uid))
    # имена из строк вида «01. 💬 Имя (@username)» / «02. Имя (id123456)»
    names: dict[str, str] = {}
    for msg in messages:
        for line in (getattr(msg, "message", None) or "").splitlines():
            m = _LINE_RE.search(line)
            if not m:
                continue
            nm = _LINE_PREFIX_RE.sub("", line[:m.start()]).strip(" -—:|")
            if nm:
                k = f"@{m.group(1).lower()}" if m.group(1) else f"id{m.group(2)}"
                names.setdefault(k, nm)
    for c in out:
        c.name = c.name or names.get(c.key())
    return out


def same_name(a: str | None, b: str | None) -> bool:
    """Имя продавца из маркета и имя из ответа бота поиска — одно и то же?

    Бот пишет «похожими» буквами (ł вместо l и т.п.) и с украшениями.
    Сравниваем только буквы/цифры. Допускаем вложенность (один содержит другой):
    'Alex' ⊂ 'Alex Trader'. Минимальная длина общей части — 4 символа чтоб
    отсечь ложные ('An' в 'Anton' и 'Anna')."""
    def keep(t: str) -> str:              # буквы любых алфавитов + обычные цифры (୨ ୧ и т.п. — украшения)
        return "".join(ch for ch in _plain(t or "") if ch.isalpha() or ch in "0123456789")
    ca, cb = keep(a), keep(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    # допускаем вложенность, если короткое имя ≥4 символов и полностью содержится в длинном
    short, long = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    return len(short) >= 4 and short in long


def exact_name(a: str | None, b: str | None) -> bool:
    """Строгое совпадение имён (только буквы/цифры, без «вхождения»): 'Alex' ≠ 'Alexander'.
    Нужно там, где владельца принимаем БЕЗ подтверждения через NFT в профиле."""
    def keep(t: str) -> str:
        return "".join(ch for ch in _plain(t or "") if ch.isalpha() or ch in "0123456789")
    ca, cb = keep(a), keep(b)
    return bool(ca) and ca == cb


def _buttons(msg: Any) -> list[tuple[str, bytes | None]]:
    """Кнопки сообщения по порядку: (текст, callback data | None)."""
    out = []
    for row in getattr(getattr(msg, "reply_markup", None), "rows", None) or []:
        for b in getattr(row, "buttons", None) or []:
            bt = getattr(b, "type", None)
            data = getattr(bt, "data", None) if bt is not None and not isinstance(bt, str) else None
            if data is None:
                data = getattr(b, "data", None)
            out.append((getattr(b, "text", "") or "?", data if isinstance(data, bytes) else None))
    return out


# Боты поиска пишут текст капчи «подменёнными» буквами (кириллица, ŋ, ɩ, ł …), чтобы его не читали
# программы. Приводим похожие буквы к латинице и дополнительно узнаём капчу по виду сообщения.
_LOOKALIKE = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j", "ԁ": "d", "ɩ": "l", "ł": "l", "ŋ": "n", "ɡ": "g",
    "ϲ": "c", "α": "a", "ο": "o", "ρ": "p", "τ": "t", "κ": "k", "ν": "v", "ɑ": "a", "ʟ": "l", "ʜ": "h",
    "ᴛ": "t", "ᴄ": "c", "ᴋ": "k", "ɪ": "i", "ᴏ": "o", "ᴘ": "p", "ᴀ": "a", "ʙ": "b", "ᴇ": "e", "ɴ": "n",
})


def _plain(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.lower().translate(_LOOKALIKE)


def _emoji_only(t: str) -> bool:
    t = (t or "").strip()
    return 0 < len(t) <= 6 and not any(ch.isalnum() for ch in t)


def is_captcha(msg: Any) -> bool:
    """Бот поиска просит пройти капчу (картинка + «нажми кнопку с похожим значком»).

    Строгие требования — чтобы не словить /start-меню funstat с текстом про капчу:
      1. Есть медиа (картинка-капча).
      2. Есть кнопки с callback_data.
      3. Либо кнопки — только эмодзи, либо в тексте есть слово про капчу.
    """
    btns = _buttons(msg)
    cb = [t for t, d in btns if d]
    # funstat чаще всего показывает 4-6 кнопок, но иногда 8-10. Верхний предел 12 (разумно для эмодзи-капчи)
    if not cb or not (2 <= len(cb) <= 12):
        return False
    has_media = getattr(msg, "media", None) is not None or getattr(msg, "photo", None) is not None
    if not has_media:
        return False       # без картинки это не капча (меню/напоминание)
    all_emoji = all(_emoji_only(t) for t in cb)
    has_captcha_word = any(w in _plain(getattr(msg, "message", None) or "") for w in _CAPTCHA_WORDS)
    return all_emoji or has_captcha_word


def _gift_matches(saved: Any, slug: str | None, unique_id: int | None) -> bool:
    g = getattr(saved, "gift", None)
    if not isinstance(g, types.StarGiftUnique):
        return False
    if slug and (getattr(g, "slug", None) or "").lower() == slug.lower():
        return True
    return bool(unique_id) and getattr(g, "id", None) == unique_id


class ProfilePrivate(Exception):
    """Пользователь скрыл список подарков (privacy settings) — проверить нельзя.
    Это НЕ 'нет NFT' — вызывающий код должен считать это 'неопределённо'."""


async def profile_has_gift(client, entity, slug: str | None, unique_id: int | None,
                           max_pages: int = 3) -> bool:
    """Виден ли этот NFT в профиле пользователя (скрытые подарки Telegram не отдаёт).

    Возвращает:
      True  — NFT найден в профиле (точно он)
      False — прочитали все видимые подарки, этого нет
      raise ProfilePrivate — пользователь закрыл профиль/подарки, неопределённость
                             (вызывающий код решает — верить ли funstat по trust-fallback)
    """
    offset = ""
    for _ in range(max_pages):
        try:
            res = await client(GetSavedStarGiftsRequest(peer=entity, offset=offset, limit=100,
                                                        exclude_unlimited=True, exclude_upgradable=True,
                                                        exclude_unupgradable=True))
        except Exception as e:
            s = str(e).upper()
            # Ошибки приватности = "не можем прочесть", не "нет"
            if ("USER_PRIVACY" in s or "CHAT_PRIVATE" in s or "PEER_ID_INVALID" in s
                    or "PRIVATE" in s or "NOT_PARTICIPANT" in s):
                raise ProfilePrivate(str(e)) from e
            raise
        if any(_gift_matches(s, slug, unique_id) for s in getattr(res, "gifts", None) or []):
            return True
        offset = getattr(res, "next_offset", None) or ""
        if not offset:
            return False
    return False


class _Expired(Exception):
    """Лот устарел, пока ждал очереди к боту поиска."""


class _PrioLock:
    """Как asyncio.Lock, но следующим получает доступ ждущий с меньшим prio (дорогие лоты — первыми)."""

    def __init__(self):
        self._busy = False
        self._waiters: list[tuple[float, int, asyncio.Future]] = []
        self._seq = 0

    def locked(self) -> bool:
        return self._busy

    def waiting(self) -> int:
        return sum(1 for *_, f in self._waiters if not f.done())

    async def acquire(self, prio: float = 0.0) -> None:
        if not self._busy and not self._waiters:
            self._busy = True
            return
        self._seq += 1
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (prio, self._seq, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()
            else:
                self._waiters = [w for w in self._waiters if w[2] is not fut]
                heapq.heapify(self._waiters)
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)
                return
        self._busy = False


class OwnerLookup:
    """Поиск владельцев через бота поиска. На одном аккаунте — один разговор с ботом за раз
    (иначе ответы перепутаются); несколько аккаунтов поиска работают параллельно."""

    def __init__(self, sessions, db=None):
        self.sessions = sessions
        self.db = db                                     # kv: имя → уже найденные владельцы (переживает деплой)
        self._slocks: dict[str, _PrioLock] = {}
        self._peers: dict[tuple[str, str], Any] = {}     # (session, @юз/id) → InputPeerUser
        self._resolves: dict[str, list[float]] = {}      # session → когда открывали юзы (лимит в час)
        self._inflight: dict[str, asyncio.Future] = {}   # имя → ответ бота, который уже ждём
        self._anchor: str | None = None                  # аккаунт поиска, когда LOOKUP_SESSIONS пуст
        self._pending = 0
        self._cache: dict[str, tuple[float, list[Candidate]]] = {}
        # Негативный кеш: имя → когда funstat сказал «не найдено». TTL — час.
        # Экономит квоту и капчи: 'Maria' на разных лотах не долбим в funstat.
        self._neg_cache: dict[str, float] = {}
        self._started: set[tuple[str, str]] = set()      # (session, bot) — /start уже отправляли
        # (session, bot) → когда отметили как unresolvable. Через _UNRESOLVABLE_COOLDOWN
        # пробуем снова (сессия могла переподключиться, кеш Telegram мог обновиться).
        self._bot_unresolvable: dict[tuple[str, str], float] = {}
        # Глобальный блэклист ботов (bot_lowercase → monotonic_deadline). Используется когда ВСЕ
        # lookup-сессии подряд получают 'No user has' на этом боте — скорее всего бот снесли,
        # не долбимся в него минут 10, чтобы не тратить попытки, и сразу ротируем на следующего
        # бота из LOOKUP_BOTS. См. _ask_bot_locked и _bot_pool.
        self._bot_dead_until: dict[str, float] = {}
        self._bot_slow_until: dict[str, float] = {}   # молчал недавно → спрашиваем последним (НЕ «мёртв»)
        # Счётчик подряд идущих unresolvable-фейлов на одного бота по РАЗНЫМ сессиям.
        # Когда достигает ~половины LOOKUP_SESSIONS или 3 (меньшее) — помечаем бота как dead.
        self._bot_consec_unres: dict[str, int] = {}
        # Сессии, которые уже внесли вклад в счётчик unresolved для этого бота. Нужно
        # чтобы «2 подряд» действительно означало 2 РАЗНЫЕ сессии, а не одну и ту же
        # дважды подряд (как было раньше: любые 2 USERNAME-ошибки, даже с одной и той же
        # сессии в разные секунды, помечали бота как dead на 15 мин).
        self._bot_unres_sessions: dict[str, set[str]] = {}
        # Время последнего unresolved по боту — для time-decay (если за 5 мин ни одной
        # ошибки, счётчик сбрасывается; иначе одноразовый glitch Telegram'а копится в
        # счётчик неделями до реального шторма).
        self._bot_unres_last_at: dict[str, float] = {}
        # on_bot_dead(bot, next_bot) — уведомить админа что lookup-бот недоступен и мы переключились
        self.on_bot_dead = None
        self._bot_dead_notified: set[str] = set()
        # Ring-buffer последних lookup-операций — для /lookup_last команды диагностики.
        # Каждая запись: (time.time(), name, result_code, extra_text).
        # result_code: 'found' | 'from_memory' | 'not_found' | 'no_candidates' | 'too_many' | 'no_answer' |
        #              'rate_limited' | 'neg_cached' | 'trusted' | 'captcha'
        from collections import deque as _deque
        self._recent_lookups: _deque = _deque(maxlen=50)
        self._last_response_text: dict[str, str] = {}       # session → последний текст от funstat (для rate-limit детекта)
        self.stats = {"lookups": 0, "found": 0, "not_found": 0, "no_answer": 0, "skipped": 0,
                      "captcha": 0, "captcha_auto": 0, "too_many": 0, "from_memory": 0,
                      "no_candidates": 0, "nft_hidden": 0, "name_mismatch": 0, "trusted_match": 0,
                      # Новые поля из fallback / multi-bot / диагностики:
                      "no_bot_pool": 0,          # лотов отброшено — пул ботов пуст (все dead)
                      "no_session": 0,           # лотов отброшено — нет рабочих сессий
                      "bot_unresolved": 0,       # кол-во 'No user has' / USERNAME_INVALID по всем ботам
                      "bot_dead_marked": 0,      # сколько раз помечали бота dead глобально
                      "captcha_random": 0,       # сколько капч решили случайной кнопкой
                      "captcha_auto_fail": 0,    # сколько капч OCR+random не решили
                      "nft_private": 0,          # сколько раз профиль был закрыт (не 'нет NFT')
                      "rate_limited": 0,         # funstat вернул 'слишком часто'
                      "resolve_limit": 0,        # лимит ресолвов в час на сессию
                      "expired": 0,              # лот устарел пока ждал
                      "neg_cached": 0}           # не ходили к боту — имя в негативном кеше
        # капча: сначала пробуем решить сами (vision-API), если не вышло — отдаём админу.
        # on_captcha(token, session, photo_bytes, button_texts) — задаёт App
        # (шлёт админам картинку и кнопки); нажатие админа → solve_captcha(token, index).
        self.on_captcha = None
        # on_bot_blocked(session: str, bot: str) — задаёт App: шлёт админу «разблокируй бот».
        # Бьёт один раз в сутки на пару (session, bot) — не спамим.
        self.on_bot_blocked = None
        self._blocked_notified: dict[tuple[str, str], float] = {}
        # Random-click fallback: если OCR не распознал — пробуем 2 случайных кнопки перед админом.
        # Отключается через CAPTCHA_RANDOM_FALLBACK=false; по умолчанию включено (user хотел так).
        import os as _os
        self._random_fallback = (_os.environ.get("CAPTCHA_RANDOM_FALLBACK", "true").strip().lower()
                                 not in ("0", "false", "no", "off", "n"))
        try:
            from .captcha_solver import CaptchaSolver, _load_cache, captcha_cache_stats
            self._auto = CaptchaSolver()
            _load_cache()      # загружаем сохранённые ответы из файла
            cs = captcha_cache_stats()
            log.info("Captcha solver init: enabled=%s, ocr=%s, vision=%s, cache=%d/%d",
                     self._auto.enabled, self._auto._ocr_available,
                     self._auto._vision.enabled, cs["size"], cs["max"])
        except Exception as e:                               # noqa: BLE001
            log.warning("Lookup: авто-решатель капчи недоступен: %s", e)
            self._auto = None
        self._captcha: dict[str, dict] = {}              # token → {session, bot, msg_id, buttons, event}
        self._captcha_by_session: dict[str, str] = {}    # session → token активной капчи
        # _captcha_streak удалён — теперь антизацикливание через captcha_rounds в _ask_bot_locked
        self._last_ask: dict[str, float] = {}            # session → когда последний раз писали боту

    # ------------------------------------------------------------------ public
    def enabled(self) -> bool:
        return bool(settings.auto_lookup and self._bot_pool())

    def any_bot_configured(self) -> bool:
        """Задан ли хоть один lookup-бот (primary или в LOOKUP_BOTS) — даже если сейчас dead.
        Нужно для queue.py: при пустом пуле dead≠'не задано', политики разные."""
        if (settings.lookup_bot_username or "").strip():
            return True
        return any(b.strip() for b in (settings.lookup_bots or []))

    def all_bots_dead(self) -> bool:
        """Все настроенные lookup-боты в глобальном блэклисте dead → lookup физически невозможен
        на этот момент. Используется в queue.py: если lookup_required=true + все боты dead,
        не отсеиваем hidden-owner лоты, а шлём с пометкой owner_not_found."""
        return False   # ботов никогда не выключаем — даже «снесённого» продолжаем спрашивать

    def _bot_pool(self) -> list[str]:
        """Порядок попыток lookup-ботов: сначала основной (LOOKUP_BOT_USERNAME), затем
        запасные из LOOKUP_BOTS. Дубликаты убираются, @ удаляется. Пустой список = отключено.
        Если бот находится в глобальном блеклисте _bot_dead_until — пропускается до истечения."""
        now = time.monotonic()
        primary = (settings.lookup_bot_username or "").lstrip("@").strip()
        extras = [b.lstrip("@").strip() for b in (settings.lookup_bots or [])]
        order: list[str] = []
        seen: set[str] = set()
        for b in ([primary] if primary else []) + extras:
            if not b:
                continue
            k = b.lower()
            if k in seen:
                continue
            seen.add(k)
            order.append(b)
        # Молчащего/«снесённого» бота НЕ выкидываем — просто спрашиваем его последним,
        # чтобы лоты не ждали его таймаут, пока запасной отвечает. Работа не останавливается.
        return sorted(order, key=lambda b: now < self._bot_slow_until.get(b.lower(), 0.0))

    def _bot_cmd(self, bot: str, seller_name: str) -> str:
        """Команда для конкретного бота. Берёт per-bot из LOOKUP_CMDS, иначе общий
        шаблон lookup_cmd_template. '{name}' в шаблоне заменяется на имя."""
        tpl = (settings.lookup_cmds or {}).get(bot.lower()) or settings.lookup_cmd_template or "/human {name}"
        if "{name}" not in tpl:
            tpl = f"{tpl} {{name}}"
        return tpl.format(name=seller_name)

    def _register_bot_unresolved(self, bot: str, session: str | None = None) -> None:
        """Инкрементирует подряд идущий счётчик unresolved на бота. При достижении порога
        (≥N РАЗНЫХ сессий, а не любых N подряд) — помечаем бота как dead глобально на
        15 минут и уведомляем админа. На успешный ответ счётчик сбрасывается.

        Два изменения относительно старой версии:
        1) Считаем только УНИКАЛЬНЫЕ сессии. Раньше код инкрементировал счётчик при каждой
           USERNAME-ошибке, и две подряд с ОДНОЙ сессии (Telegram glitch) помечали бота
           мёртвым. Теперь нужно 3+ разных сессии.
        2) Time-decay: если с последнего unresolved прошло больше 5 минут, счётчик
           сбрасывается — одиночные транзиентные glitch'и не накапливаются неделями.

        Maker: 'пишет что боты мертвы хотя все живы' — это оно."""
        k = bot.lower()
        now = time.monotonic()
        last_at = self._bot_unres_last_at.get(k, 0.0)
        if last_at and now - last_at > 300:
            # 5 минут тишины — сбрасываем накопленное (одиночный glitch не накапливается)
            self._bot_consec_unres.pop(k, None)
            self._bot_unres_sessions.pop(k, None)
        self._bot_unres_last_at[k] = now
        sessions_hit = self._bot_unres_sessions.setdefault(k, set())
        if session:
            sessions_hit.add(session)
        self._bot_consec_unres[k] = self._bot_consec_unres.get(k, 0) + 1
        # «Разных сессий» — вот что реально означает dead. Если нет имени сессии (edge-case),
        # считаем по общему числу попыток, но с более высоким порогом.
        n = len(sessions_hit) if sessions_hit else self._bot_consec_unres[k]
        # Пороги: 3 разные сессии (раньше было 2), для больших пулов — половина пула.
        pool_size = max(1, len(settings.lookup_sessions or []))
        threshold = max(3, pool_size // 2)
        if n < threshold:
            return
        now = time.monotonic()
        # «Мёртвым» бота НЕ помечаем (просьба админа: боты не должны слетать) — только на 10 мин
        # ставим его в конец очереди, пока отвечает запасной. Из работы он не выпадает.
        self._bot_slow_until[k] = max(self._bot_slow_until.get(k, 0.0), now + 600)
        if k in self._bot_dead_notified:
            return
        self._bot_dead_notified.add(k)
        # Следующий бот из пула (кроме помеченного этого)
        next_bot = next((b for b in self._bot_pool() if b.lower() != k), None)
        log.error("Lookup: бот @%s недоступен (%d подряд unresolvable на разных сессиях). "
                  "Помечен dead на 15 мин. Fallback → @%s",
                  bot, n, next_bot or "— запасных нет, настрой LOOKUP_BOTS")
        if self.on_bot_dead:
            try:
                import asyncio as _a
                t = _a.get_running_loop().create_task(self.on_bot_dead(bot, next_bot, "silent"))
                # strong-ref: без него Python 3.11+ GC может собрать task до завершения
                if not hasattr(self, "_notify_tasks"):
                    self._notify_tasks: set = set()
                self._notify_tasks.add(t)
                t.add_done_callback(self._notify_tasks.discard)
            except Exception as e:
                log.debug("on_bot_dead notify failed: %s", e)

    def _clear_bot_unresolved(self, bot: str) -> None:
        """После успешного ответа от бота — сбрасываем счётчик подряд и снимаем dead-mark."""
        k = bot.lower()
        self._bot_consec_unres.pop(k, None)
        self._bot_unres_sessions.pop(k, None)
        self._bot_unres_last_at.pop(k, None)
        if k in self._bot_dead_notified and self.on_bot_dead:
            try:     # предупреждали, что бот молчит — сообщаем, что он снова отвечает
                t = asyncio.get_running_loop().create_task(self.on_bot_dead(bot, None, "back"))
                if not hasattr(self, "_notify_tasks"):
                    self._notify_tasks = set()
                self._notify_tasks.add(t)
                t.add_done_callback(self._notify_tasks.discard)
            except Exception:
                pass
        self._bot_dead_notified.discard(k)
        self._bot_slow_until.pop(k, None)
        if k in self._bot_dead_until:
            self._bot_dead_until.pop(k, None)
            self._persist_bot_dead(k, None)  # удаляем из БД
            log.info("Lookup: бот @%s снова отвечает — снимаю dead-mark", bot)

    def _persist_bot_dead(self, bot_key: str, until_monotonic: float | None) -> None:
        """Запишем dead-until в БД как wall-clock (time.time() + остаток). None = удалить.
        Fire-and-forget с strong-ref в self._persist_tasks (иначе GC может съесть
        pending-таск). Безопасно в контексте без running loop (RuntimeError игнорируется:
        при тестах или offline-коде это норма)."""
        if self.db is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return      # не в loop — не можем создать task (тесты / offline)
        key = f"bot_dead:{bot_key}"
        if until_monotonic is None:
            coro = self.db.kv_set(key, None)
        else:
            # monotonic → walltime: прибавляем остаток к time.time()
            remaining = until_monotonic - time.monotonic()
            if remaining <= 0:
                return
            wall_until = int(time.time() + remaining)
            coro = self.db.kv_set(key, str(wall_until))
        try:
            t = loop.create_task(coro)
            # strong-ref: иначе Python 3.11 GC может собрать задачу до завершения
            if not hasattr(self, "_persist_tasks"):
                self._persist_tasks: set = set()
            self._persist_tasks.add(t)
            t.add_done_callback(self._persist_tasks.discard)
        except Exception as e:
            log.debug("persist bot_dead failed: %s", e)

    async def load_persisted_bot_deads(self) -> int:
        """При старте — восстанавливаем 15-мин блокировки ботов из БД.
        Иначе каждый деплой на Render сбрасывал dead-marks и трекер снова долбился
        в снесённые боты до реактивной детекции."""
        if self.db is None:
            return 0
        count = 0
        try:
            # нет pattern-поиска, но ботов максимум ~10 — проходимся по всем настроенным
            primary = (settings.lookup_bot_username or "").lstrip("@").strip()
            extras = [b.lstrip("@").strip() for b in (settings.lookup_bots or [])]
            all_bots = {b.lower() for b in ([primary] if primary else []) + extras if b}
            now_t = time.time()
            for k in all_bots:
                raw = await self.db.kv_get(f"bot_dead:{k}")
                if not raw or not raw.isdigit():
                    continue
                wall_until = int(raw)
                remaining = wall_until - now_t
                if remaining <= 0:
                    await self.db.kv_set(f"bot_dead:{k}", None)
                    continue
                # старые «dead»-метки из прошлых версий просто стираем — ботов не выключаем
                await self.db.kv_set(f"bot_dead:{k}", None)
        except Exception as e:
            log.warning("load_persisted_bot_deads failed: %s", e)
        return count

    async def check_bots_liveness(self) -> dict[str, bool]:
        """Проактивная проверка настроенных lookup-ботов: пробуем get_input_entity для каждого
        через любую рабочую сессию. Возвращает {bot: True/False}. При False бот сразу помечается
        как dead (до первого реального успеха). Безопасно вызывать и из фонового loop, и однократно
        при старте — resolve-ы недорогие (а при успехе кэшируются Telethon'ом).

        Используется:
          - при старте трекера: заранее узнать что funstat снесён, не ждать первого лота
          - периодически (можно вешать на loop): раз в N минут обновлять статус"""
        primary = (settings.lookup_bot_username or "").strip()
        extras = list(settings.lookup_bots or [])
        all_bots: list[str] = []
        seen: set[str] = set()
        for b in ([primary] if primary else []) + extras:
            b = b.lstrip("@").strip()
            if b and b.lower() not in seen:
                seen.add(b.lower())
                all_bots.append(b)
        if not all_bots:
            return {}

        # Берём ЛЮБУЮ рабочую сессию (не привязываемся к LOOKUP_SESSIONS: liveness-check
        # не ест лимитов funstat — это resolveUsername, который Telethon кэширует)
        try:
            session_candidates = [w.name for w in self.sessions.usable(include_reserved=True)
                                  if w.is_usable()]
        except Exception:
            session_candidates = []
        if not session_candidates:
            log.debug("Lookup liveness: нет рабочих сессий — пропускаем")
            return {}

        results: dict[str, bool] = {}
        for bot in all_bots:
            alive = False
            for sess in session_candidates[:3]:    # максимум 3 сессии на бота — достаточно
                try:
                    async def _probe(client, b=bot):
                        return await client.get_input_entity(b)
                    res = await asyncio.wait_for(self._run(sess, _probe, timeout=10), timeout=12)
                    if res is not None:
                        alive = True
                        break
                except Exception as e:
                    s = str(e).lower()
                    # Только username-ошибки означают "бота нет". Flood / net / timeout — не повод.
                    if ("no user has" in s or "username_invalid" in s or "username_not_occupied" in s
                            or "cannot find any entity" in s):
                        continue       # пробуем следующую сессию (вдруг у неё кэш свежее)
                    # Любая другая ошибка — не выносим приговор боту, пропускаем
                    log.debug("Lookup liveness: probe %s on %s: %s — inconclusive", bot, sess, e)
                    alive = True       # не можем сказать что мёртв → считаем живым
                    break
            results[bot] = alive
            if alive:
                self._clear_bot_unresolved(bot)
                log.info("Lookup liveness: @%s — alive", bot)
            else:
                # Mark dead только если РЕАЛЬНО 3+ сессии подтвердили username-ошибку.
                # Раньше: 3 из 3 пробованных → сразу dead на 15 мин. Проблема в том что
                # на старте / после деплоя сессии ещё не имеют бота в своём peer-кэше
                # и contacts.resolveUsername МОГ вернуть transient 'USERNAME_NOT_OCCUPIED'
                # из-за реплики Telegram'а — все 3 попытки давали эту ошибку, бот
                # помечался мёртвым, Maker видел '💀 боты мертвы хотя все живы'.
                # Теперь проверяем ДВАЖДЫ: если <3 session_candidates, не хватает данных —
                # не маркируем.
                probed = min(3, len(session_candidates))
                if probed < 3:
                    log.warning("Lookup liveness: @%s — не смог проверить (пробовано %d сессий, "
                                "для dead-mark нужно 3+). Пропускаем.", bot, probed)
                    results[bot] = None     # inconclusive — не маркируем ни alive ни dead
                    continue
                # mark dead immediately: liveness подтвердил что все наши сессии не видят бота
                k = bot.lower()
                self._bot_slow_until[k] = time.monotonic() + 600   # не «мёртв», просто в конец очереди
                log.warning("Lookup liveness: @%s — не резолвится на %d сессиях, спрашиваю его после запасного",
                          bot, probed)
                if k not in self._bot_dead_notified and self.on_bot_dead:
                    self._bot_dead_notified.add(k)
                    next_bot = next((b for b in all_bots if b.lower() != k and results.get(b, True)), None)
                    try:
                        t = asyncio.get_running_loop().create_task(self.on_bot_dead(bot, next_bot, "missing"))
                        # strong-ref (см. _register_bot_unresolved)
                        if not hasattr(self, "_notify_tasks"):
                            self._notify_tasks = set()
                        self._notify_tasks.add(t)
                        t.add_done_callback(self._notify_tasks.discard)
                    except Exception:
                        pass
        return results

    async def find_owner(self, seller_name: str, slug: str | None, unique_id: int | None,
                         prio: float = 0.0, deadline: float | None = None) -> FoundOwner | None:
        """prio — чем меньше, тем раньше в очереди к боту поиска (передаём −цена: дорогие первыми)."""
        if not self.enabled() or not (slug or unique_id):
            return None
        seller_name = (seller_name or "").strip()
        # Защита: короткое/мусорное имя (<3 букв) → funstat вернёт слишком много людей →
        # лучше сразу None чем тратить квоту
        if len(seller_name) < 3:
            return None
        if self._pending >= _MAX_PENDING:
            self.stats["skipped"] += 1
            log.warning("Lookup queue full (%s) — %r skipped", self._pending, seller_name)
            return None
        self._pending += 1
        try:
            found = await self._find(seller_name, slug, unique_id, prio, deadline)
            if found is None:
                # Ник «шрифтом» (𝗠𝗜𝗡𝗥𝗜, ᴀʟᴇx, zalgo, с эмодзи) — бот поиска может не найти его как есть.
                # Пробуем ещё раз обычными буквами (один доп. запрос, только если имя реально другое).
                from utils.names import search_name
                alt = search_name(seller_name)
                if (alt and len(alt) >= 3 and _norm_name(alt) != _norm_name(seller_name)
                        and (deadline is None or time.time() < deadline)):
                    self.stats["fancy_retry"] = self.stats.get("fancy_retry", 0) + 1
                    llog("LOOKUP", f"{seller_name!r}: не нашли — ищу обычными буквами {alt!r}")
                    found = await self._find(alt, slug, unique_id, prio, deadline)
            return found
        except _Expired:
            self.stats["expired"] = self.stats.get("expired", 0) + 1
            return None
        finally:
            self._pending -= 1

    def pending_captchas(self) -> int:
        return len(self._captcha)

    async def solve_captcha(self, token: str, index: int) -> str:
        """Админ нажал кнопку капчи → жмём ту же кнопку на аккаунте поиска.
        Запоминаем ответ в кеш — следующая такая же капча решится автоматически."""
        c = self._captcha.get(token)
        if c is None:
            return "Эта капча уже неактуальна"
        if not (0 <= index < len(c["buttons"])) or c["buttons"][index][1] is None:
            return "Неизвестная кнопка"
        data = c["buttons"][index][1]
        bot, msg_id = c["bot"], c["msg_id"]

        async def _click(client):
            ent = await client.get_input_entity(bot)
            try:
                ans = await client(GetBotCallbackAnswerRequest(peer=ent, msg_id=msg_id, data=data))
                return getattr(ans, "message", None) or ""
            except Exception as e:          # бот часто не отвечает на callback (timeout) — нажатие всё равно прошло
                if "timeout" not in str(e).lower() and "BOT_RESPONSE_TIMEOUT" not in str(e):
                    raise
                return ""
        try:
            answer_text = await self._run(c["session"], _click, timeout=20)
        except Exception as e:
            return f"Не удалось нажать: {type(e).__name__}"
        # funstat/подобные боты могут ответить "Неверно!" или "Wrong!" в popup —
        # тогда ответ не нужно кешировать, и даже надо из кеша удалить (если был).
        # ВАЖНО: НЕ ставим event — иначе _wait_captcha воспримет неверный клик как успех
        # (админ в этом случае не получит шанс исправить, а funstat даст новую капчу → цикл).
        wrong_keywords = ("неверно", "wrong", "incorrect", "неправиль", "попробуй", "try again",
                          "yana", "ошиб", "error", "fail", "не то", "not that", "еще раз",
                          "ещё раз", "retry", "wrong button", "noto'g'ri", "notogri")
        if answer_text and any(w in answer_text.lower() for w in wrong_keywords):
            log.info("Lookup: кнопка %d на %s — funstat сказал неправильно (%r)",
                     index, c["session"], answer_text)
            try:
                from .captcha_solver import cache_pop, _cache_key
                photo = c.get("photo")
                if photo:
                    btn_texts = [t for t, _ in c["buttons"]]
                    cache_pop(_cache_key(photo, btn_texts))               # убираем кривой ответ
            except Exception:
                pass
            # event НЕ ставим — пусть _wait_captcha продолжает ждать (новая капча или админ)
            return f"Нажал, но funstat сказал: {answer_text[:80]}"
        # ответ админа кешируем — следующая такая же капча пройдёт сама.
        # НО не кешируем если answer_text пустой (callback timeout — не уверены что клик дошёл).
        if answer_text:
            try:
                from .captcha_solver import cache_put, _cache_key
                photo = c.get("photo")
                if photo:
                    btn_texts = [t for t, _ in c["buttons"]]
                    cache_put(_cache_key(photo, btn_texts), index)
            except Exception:
                pass
        c["event"].set()
        return "Нажал, продолжаю поиск"

    # ------------------------------------------------------------------ internals
    def _pick_sessions(self) -> list[str]:
        """Аккаунты для бота поиска: выделенные (LOOKUP_SESSIONS) по кругу, иначе первый рабочий.

        FALLBACK: если выделенный пул LOOKUP_SESSIONS весь мёртвый (FloodWait /
        unresolvable блеклист), берём любые is_usable() сессии трекера — иначе
        hidden-owner лоты отсеиваются глухо, пока пул не отойдёт (минуты-часы).

        ВАЖНО: фильтруем сессию по unresolvable ТОЛЬКО если она не может достучаться
        НИ ДО ОДНОГО бота из пула. Раньше мы смотрели только на primary — и когда
        primary dead, или сессия случайно получила USERNAME_INVALID на primary но
        прекрасно работает с fallback-ботами, _pick_sessions всё равно её
        выкидывал → _find получал пустой order → silent-skip через no_session, а
        боты живы ('не ищет иногда')."""
        want = [n for n in settings.lookup_sessions if n]
        now = time.monotonic()
        # Актуальный список ботов (без тех, что в глобальном dead-mark) — именно его
        # будет перебирать _find. Если пул пуст — без фильтра по бот-специфичным меткам,
        # _ok сводится к is_usable: нам всё равно не к кому писать, но пусть вызывающий
        # сам увидит это через bot_pool() (no_bot_pool), а не через no_session.
        active_bots = [b.lower() for b in self._bot_pool()]

        def _ok(n):
            w = self.sessions.get(n)
            if w is None or not w.is_usable():
                return False
            if not active_bots:
                return True
            # Сессия пригодна, если ХОТЬ ОДИН активный бот у неё не в unresolvable-блоке.
            for b in active_bots:
                unres = self._bot_unresolvable.get((n, b))
                if not unres or now - unres >= _UNRESOLVABLE_COOLDOWN:
                    return True
            return False

        def _rank(n):
            w = self.sessions.get(n)
            speed = getattr(w, "speed", 1.0) if w is not None else 0.0
            return (n in self._captcha_by_session, self._slock(n).locked(), -speed)

        if want:
            ok = [n for n in want if _ok(n)]
            if ok:
                self._rr = (getattr(self, "_rr", -1) + 1) % len(ok)
                order = ok[self._rr:] + ok[:self._rr]
                return sorted(order, key=_rank)
            # Выделенный пул мёртвый — fallback на любые рабочие сессии трекера.
            # Лучше временно долбить funstat с «мониторинговых» аккаунтов, чем
            # отсеивать все hidden-owner лоты.
            fallback = [x.name for x in self.sessions.usable(include_reserved=True)
                        if _ok(x.name)]
            if fallback:
                log.warning("Lookup: выделенный пул LOOKUP_SESSIONS (%d) весь недоступен — "
                            "fallback на %d обычных сессий", len(want), len(fallback))
                self._rr = (getattr(self, "_rr", -1) + 1) % len(fallback)
                order = fallback[self._rr:] + fallback[:self._rr]
                return sorted(order, key=_rank)
            return []
        # Отдельные аккаунты не выбраны → ищем с ОДНОГО аккаунта трекера. Если он во FloodWait —
        # ждём его, а не перескакиваем на следующий (иначе FloodWait по очереди ловят все аккаунты).
        w = self.sessions.get(self._anchor) if self._anchor else None
        if w is not None and getattr(getattr(w, "status", None), "value", "") == "FLOOD_WAIT":
            return [self._anchor] if w.is_usable() else []
        usable = sorted(x.name for x in self.sessions.usable(include_reserved=True))
        self._anchor = usable[0] if usable else None
        return usable[:1]

    def _slock(self, session: str) -> _PrioLock:
        return self._slocks.setdefault(session, _PrioLock())

    def queue_size(self) -> int:
        return self._pending

    # ---------------------------------------------------- память найденных владельцев (kv в БД)
    async def _known(self, name: str) -> list[Candidate]:
        if self.db is None:
            return []
        try:
            raw = await self.db.kv_get(f"owner:{_norm_name(name)}")
            items = json.loads(raw) if raw else []
        except Exception:
            return []
        # отрицательные id — pseudo (legacy до фикса в _remember). Фильтруем на чтении,
        # чтобы старые записи не портили результат. Нули/негатив — тоже выкидываем из поля id.
        out = []
        for i in items:
            uid = i.get("id") if (i.get("id") and i["id"] > 0) else None
            u = i.get("u") or None
            if u or uid:
                out.append(Candidate(username=u, user_id=uid, name=name))
        return out

    async def _remember(self, name: str, owner: FoundOwner) -> None:
        if self.db is None:
            return
        # Отрицательные user_id — наши псевдо-ID (хеш username) для dedup. Их нельзя сохранять
        # в память как «уже найденный владелец» — будущие lookup'ы возьмут их из _known() и
        # отдадут пайплайну бесполезный pseudo-ID вместо настоящего поиска через funstat.
        if not owner.user_id or owner.user_id < 0:
            return
        key = f"owner:{_norm_name(name)}"
        try:
            raw = await self.db.kv_get(key)
            items = [i for i in (json.loads(raw) if raw else []) if i.get("id") != owner.user_id]
            items.insert(0, {"id": owner.user_id, "u": owner.username})
            await self.db.kv_set(key, json.dumps(items[:5], ensure_ascii=False))
        except Exception as e:
            log.debug("remember owner failed: %s", e)

    async def debug(self, name: str) -> tuple[str | None, list[Candidate], str]:
        """Для /lookup: спросить бота поиска «как есть» → (аккаунт, кандидаты, текст ошибки)."""
        bots = self._bot_pool()
        if not bots:
            return None, [], "бот поиска не задан (LOOKUP_BOT_USERNAME пуст)"
        order = self._pick_sessions()
        if not order:
            return None, [], "нет рабочего аккаунта для поиска"
        last_err = ""
        for bot in bots:
            for session in order:
                cands = await self._ask_bot(session, bot, name, prio=-1e12)
                if cands is not None:
                    return session, cands, ""
            last_err = f"бот @{bot} не ответил ни на одной сессии"
        return order[0], [], last_err or "бот поиска не ответил (или капча не пройдена)"

    async def _run(self, session: str, fn, timeout: float = 30):
        res, _ = await self.sessions.run(fn, only=session, attempts=1, timeout=timeout)
        return res

    async def _find(self, seller_name: str, slug: str | None, unique_id: int | None,
                    prio: float = 0.0, deadline: float | None = None) -> FoundOwner | None:
        bots = self._bot_pool()
        if not bots:
            # Все боты в блеклисте (dead) или не настроены — не долбимся вхолостую.
            log.debug("Lookup: пул ботов пуст (все dead или не настроены) — пропускаем %r", seller_name)
            self.stats["no_bot_pool"] = self.stats.get("no_bot_pool", 0) + 1
            self._track_lookup(seller_name, "no_bot_pool", "все боты dead / не настроены")
            return None
        bot = bots[0]       # основной, используется для логов/метрик; fallback-боты пробуются в цикле ниже
        order = self._pick_sessions()
        if not order:
            # Было WARNING на каждый лот — засирало логи при шторме FloodWait.
            # Понижено до INFO с rate-limit: пишем раз в минуту, не чаще.
            now_t = time.monotonic()
            if now_t - getattr(self, "_last_no_session_log", 0.0) > 60:
                self._last_no_session_log = now_t
                log.info("Lookup: нет рабочего аккаунта для бота поиска%s "
                         "(пишется не чаще раза в минуту)",
                         f" ({', '.join(settings.lookup_sessions)})" if settings.lookup_sessions else "")
            self.stats["no_session"] = self.stats.get("no_session", 0) + 1
            self._track_lookup(seller_name, "no_session", "все сессии залипли")
            return None
        session = order[0]
        self.stats["lookups"] += 1
        # владельцы, уже найденные раньше под этим именем — доверяем без бота поиска, но
        # всё равно читаем профиль чтобы фильтры (PAID_MESSAGES, LEVEL_TOO_HIGH) могли сработать.
        # Память «имя → владелец» отключена: под тем же именем продаёт ДРУГОЙ человек, а юз в памяти
        # бывает старым → отсюда «не те продавцы» и старые юзы. Каждый раз проверяем заново.
        known = []
        # Раньше требовалось И user_id И username, иначе шли по новой к funstat. Но у многих
        # пользователей @юза нет — после первой проверки память хранит только user_id, и каждый
        # следующий лот этого продавца снова грыз лимит funstat'а. Теперь достаточно user_id
        # (user_id > 0): username (при наличии) всё равно подтянется из enrich_seller через
        # session.get_entity. Если user_id нет — через память не ходим: нужно резолвить username.
        for first in known:
            if first.user_id and first.user_id > 0:
                self.stats["found"] += 1
                self.stats["from_memory"] = self.stats.get("from_memory", 0) + 1
                llog("LOOKUP", f"{seller_name!r} → u{first.user_id} "
                               f"@{first.username or '—'} из памяти")
                self._track_lookup(seller_name, "from_memory",
                                   f"u{first.user_id} @{first.username or '—'}")
                return FoundOwner(first.user_id, first.username,
                                  first.name or seller_name, None, None, from_memory=True)
        key = _norm_name(seller_name)
        # негативный кеш: funstat недавно сказал «нет такого» → не долбим снова
        neg = self._neg_cache.get(key)
        if neg and time.monotonic() - neg < _NEG_CACHE_TTL:
            self.stats["not_found"] += 1
            self.stats["neg_cached"] = self.stats.get("neg_cached", 0) + 1
            llog("LOOKUP", f"{seller_name!r}: уже искали недавно — не найден, скип "
                           f"({(time.monotonic() - neg) / 60:.0f} мин назад)")
            self._track_lookup(seller_name, "neg_cached",
                               f"{(time.monotonic() - neg) / 60:.0f} мин назад")
            return None
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL:
            cands = cached[1]
        elif key in self._inflight:
            # то же имя уже спрашивают у бота (у продавца несколько лотов) — ждём тот ответ, а не спрашиваем снова
            try:
                cands = await asyncio.shield(self._inflight[key])
            except Exception:
                cands = None
            if cands is None:
                self.stats["no_answer"] += 1
                return None
        else:
            fut = asyncio.get_running_loop().create_future()
            self._inflight[key] = fut
            cands = None
            used_bot = bot                 # для логов — какой бот в итоге ответил (default = primary)
            try:
                # Мульти-бот fallback: сначала primary (funstat), затем запасные из LOOKUP_BOTS.
                # Если бот снесён и попал в _bot_dead_until, он выпадает из _bot_pool и мы его
                # не пробуем — сразу идём на следующего. Пробуем каждую сессию на каждом боте.
                expired_on_all = True
                for b in bots:
                    expired_on_bot = False
                    # молчащего/«снесённого» бота спрашиваем только с одной сессии, а не со всех
                    # (иначе лот ждал бы 15 с × число сессий)
                    b_dead = time.monotonic() < self._bot_slow_until.get(b.lower(), 0.0)
                    for session in (order[:1] if b_dead else order):
                        try:
                            cands = await self._ask_bot(session, b, seller_name, prio=prio, deadline=deadline)
                        except _Expired:
                            # Дедлайн истёк. Если есть ещё бот/сессия — пробуем их.
                            # Полностью expired только если ВСЕ пары (bot, session) истекли.
                            expired_on_bot = True
                            continue
                        expired_on_all = False
                        if cands is not None:
                            used_bot = b
                            break
                    if cands is not None:
                        break
                    if not expired_on_bot:
                        expired_on_all = False
                    # Все сессии провалились на этом боте — если он теперь в блеклисте, пишем лог
                    if b.lower() in self._bot_dead_until:
                        log.warning("Lookup: бот @%s помечен как недоступный — fallback на следующий", b)
                bot = used_bot
                # Если дедлайн истёк на ВСЕХ попытках и cands всё равно None — пробрасываем _Expired
                # чтобы _find увидел его и не кешировал результат как negative.
                if cands is None and expired_on_all and bots:
                    raise _Expired()
            finally:
                self._inflight.pop(key, None)
                if not fut.done():
                    fut.set_result(cands)
            if cands is None:
                self.stats["no_answer"] += 1
                self._track_lookup(seller_name, "no_answer", f"@{bot} молчал")
                return None
            if cands:
                self._cache[key] = (time.monotonic(), cands)
            # LRU-подобный trim: выкинуть 25% самых старых (по времени добавления) вместо
            # полного .clear(), иначе каждый 5001-й lookup обнуляет свежие кеши и тратит квоту funstat.
            if len(self._cache) > 5000:
                cutoff = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:len(self._cache) // 4]
                for k, _ in cutoff:
                    self._cache.pop(k, None)
        if not cands:
            # Проверяем: может это rate-limit от funstat, не настоящее «нет кандидатов»?
            # В таком случае не кешируем — попробуем снова позже.
            last_text = ""
            # ВАЖНО: session определён только в ветке «новый запрос»; из inflight-ветки
            # и cached-ветки его нет — иначе был бы NameError. locals() безопаснее getattr.
            session_var = locals().get("session")
            try:
                # cands_text собирали в _ask_bot_locked — тут его нет, но мы можем взять из _cache
                # вместо этого — проверяем по last-raw-text из _ask_bot
                if session_var is not None:
                    last_text = self._last_response_text.get(session_var, "")
            except AttributeError:
                last_text = ""
            if last_text and any(w in last_text.lower() for w in _RATE_LIMIT_WORDS):
                self.stats["rate_limited"] = self.stats.get("rate_limited", 0) + 1
                llog("LOOKUP", f"@{bot} /human {seller_name!r}: rate-limit, НЕ кешируем")
                self._track_lookup(seller_name, "rate_limited", f"@{bot}")
                return None
            self.stats["not_found"] += 1
            self.stats["no_candidates"] += 1
            self._neg_cache[key] = time.monotonic()
            # LRU-подобный trim (как в _cache): не выбрасываем всё разом
            if len(self._neg_cache) > 10000:
                cutoff = sorted(self._neg_cache.items(), key=lambda kv: kv[1])[:len(self._neg_cache) // 4]
                for k, _ in cutoff:
                    self._neg_cache.pop(k, None)
            llog("LOOKUP", f"@{bot} /human {seller_name!r}: кандидатов нет (кешируем на час)")
            self._track_lookup(seller_name, "no_candidates", f"@{bot}")
            return None
        require_nft = False
        if settings.lookup_skip_over and len(cands) >= settings.lookup_skip_over:
            # Частое имя: среди 20 обычно 1–3 с ТОЧНО таким же ником — проверяем только их
            # и только по NFT в профиле (без «доверия»: в списке funstat видны не все тёзки).
            exact = [c for c in cands if c.name and exact_name(c.name, seller_name)]
            if 1 <= len(exact) <= 3:
                self.stats["too_many_rescued"] = self.stats.get("too_many_rescued", 0) + 1
                llog("LOOKUP", f"/human {seller_name!r}: {len(cands)} кандидатов, точное имя у {len(exact)} — "
                               "проверяю их по NFT в профиле")
                cands, require_nft = exact, True
        if settings.lookup_skip_over and len(cands) >= settings.lookup_skip_over:
            # слишком частое имя — проверять всех долго, такой лот сразу пропускаем
            self.stats["not_found"] += 1
            self.stats["too_many"] = self.stats.get("too_many", 0) + 1
            self._track_lookup(seller_name, "too_many", f"{len(cands)} кандидатов")
            self._neg_cache[key] = time.monotonic()
            llog("LOOKUP", f"/human {seller_name!r}: {len(cands)} кандидатов (≥{settings.lookup_skip_over}) — "
                           f"скип (кешируем на час)")
            return None
        # сначала кандидаты с @username (их бот показывает как «01. Имя (@username)»), потом голые ID
        cands = sorted(cands, key=lambda c: c.username is None)
        found = await self._verify(session, cands,
                                   seller_name, slug, unique_id, require_nft=require_nft)
        if found:
            # Проставляем реально ответившего бота. В ветках cached/inflight used_bot может
            # не быть определён (UnboundLocalError на fallback'е) — тогда используем bot
            # как fallback (primary из _bot_pool). Так owner_found_via перестанет лгать,
            # что ответил funstat, когда реально отвечал запасной.
            try:
                found.bot = locals().get("used_bot") or bot
            except Exception:
                pass
            self.stats["found"] += 1
            await self._remember(seller_name, found)
            # session=None → trust-fallback (резолв упёрся в лимит / без проверки NFT), иначе _verify
            how = "trust" if found.session is None else "NFT в профиле"
            llog("LOOKUP", f"/human {seller_name!r} → u{found.user_id}"
                           + (f" @{found.username}" if found.username else "") + f" ({how})")
            self._track_lookup(seller_name, "found",
                               f"u{found.user_id} @{found.username or '—'} ({how})")
        else:
            self.stats["not_found"] += 1
            self.stats["nft_hidden"] += 1
            self._neg_cache[key] = time.monotonic()
            llog("LOOKUP", f"/human {seller_name!r}: {len(cands)} кандидат(ов), NFT ни у кого не виден "
                           f"(кешируем 'не найден' на час)")
            self._track_lookup(seller_name, "not_found", f"{len(cands)} cand, NFT не виден")
        return found

    def _track_lookup(self, name: str, result: str, extra: str = "") -> None:
        """Регистрирует lookup-операцию в ring-buffer последних ~50 для /lookup_last.
        Безопасно к падениям — если буфера нет, молча пропускает."""
        try:
            buf = getattr(self, "_recent_lookups", None)
            if buf is not None:
                buf.append((time.time(), name, result, extra))
        except Exception:
            pass

    async def _ask_bot(self, session: str, bot: str, seller_name: str,
                       prio: float = 0.0, deadline: float | None = None) -> list[Candidate] | None:
        """Отправить «/human <имя>» и дождаться ответа. None — бот не ответил / ошибка.
        deadline (time.time()) — после него лот уже не нужен: из очереди к боту уходим, не спрашивая."""
        lock = self._slock(session)
        if deadline is not None:
            left = deadline - time.time()
            if left <= 0:
                raise _Expired()
            try:
                await asyncio.wait_for(lock.acquire(prio), timeout=left)
            except asyncio.TimeoutError:
                raise _Expired() from None
        else:
            await lock.acquire(prio)
        try:
            return await self._ask_bot_locked(session, bot, seller_name)
        finally:
            lock.release()

    def _adaptive_interval(self) -> float:
        """Если за последние 5 мин было много капч — увеличиваем паузу между запросами к боту.
        funstat даёт капчу именно за частоту: чем чаще пишем, тем чаще капча. Автоматика:
          0-2 капчи / 5 мин    → базовый interval
          3-5                 → ×1.5
          6-10                → ×2
          11-20               → ×3
          21+                 → ×5 (шторм, явно нас тротлит)
        Накопленный список капч: self._captcha_recent (monotonic timestamps за 5 мин)."""
        base = settings.lookup_interval
        now = time.monotonic()
        # Храним timestamps всех capchтa за ~5 мин для скользящего окна
        recent = getattr(self, "_captcha_recent", None)
        if recent is None:
            self._captcha_recent = recent = []
        # очистим протухшие
        self._captcha_recent = recent = [t for t in recent if now - t < 300]
        n = len(recent)
        if n >= 21:
            factor = 5.0
        elif n >= 11:
            factor = 3.0
        elif n >= 6:
            factor = 2.0
        elif n >= 3:
            factor = 1.5
        else:
            factor = 1.0
        return base * factor

    async def _ask_bot_locked(self, session: str, bot: str, seller_name: str,
                              _retry: bool = True) -> list[Candidate] | None:
        cmd = self._bot_cmd(bot, seller_name)
        # пауза между запросами с одного аккаунта: частые запросы → бот поиска выдаёт капчу.
        # interval адаптивный: при штормах капчи ×2…×5 от базового lookup_interval.
        effective_interval = self._adaptive_interval()
        gap = effective_interval - (time.monotonic() - self._last_ask.get(session, 0.0))
        if gap > 0:
            await asyncio.sleep(gap)

        async def _send(client):
            ent = await client.get_input_entity(bot)
            if (session, bot) not in self._started:
                last = await client.get_messages(ent, limit=1)
                if not last:
                    await client.send_message(ent, "/start")       # бот ещё ни разу не запускался
                    await asyncio.sleep(1.5)
                self._started.add((session, bot))
            return await client.send_message(ent, cmd)

        unresolvable_since = self._bot_unresolvable.get((session, bot))
        if unresolvable_since and time.monotonic() - unresolvable_since < _UNRESOLVABLE_COOLDOWN:
            log.debug("Lookup: пропускаю %s — ранее не смогла резолвить @%s (<%.0f c назад)",
                      session, bot, _UNRESOLVABLE_COOLDOWN)
            return None
        try:
            sent = await self._run(session, _send)
        except Exception as e:
            s = str(e)
            # "No user has X as username" / "USERNAME_INVALID" / "USERNAME_NOT_OCCUPIED":
            # либо бот действительно удалён/переименован, либо кэш Telegram устарел — короткий cooldown
            if "No user has" in s or "USERNAME" in s.upper():
                self._bot_unresolvable[(session, bot)] = time.monotonic()
                log.warning("Lookup: @%s не резолвится с %s — на %.0f c не пробуем",
                            bot, session, _UNRESOLVABLE_COOLDOWN)
                # Глобальный счётчик "бот недоступен". Если N разных сессий подряд получают эту
                # ошибку на одном боте — бот скорее всего снесли. Помечаем его как dead глобально,
                # fallback на следующего из LOOKUP_BOTS автоматически (см. _bot_pool).
                self.stats["bot_unresolved"] = self.stats.get("bot_unresolved", 0) + 1
                self._register_bot_unresolved(bot, session=session)
            # Аккаунт заблокировал бота (владелец ЛИЧНО нажал /block в чате с ботом на этом
            # аккаунте). Пока не разблокирует — писать невозможно. Блочим на 6 часов (хранится
            # как absolute-time + смещение, cooldown-chek всегда "сейчас - stored < cooldown"
            # даст True пока not expired).
            elif "YOU_BLOCKED_USER" in s.upper() or "you blocked this user" in s.lower():
                # Сначала сами разблокируем бота на этом аккаунте — без участия админа
                try:
                    async def _unblock(client, b=bot):
                        from telethon.tl.functions.contacts import UnblockRequest
                        return await client(UnblockRequest(id=b))
                    await self._run(session, _unblock, timeout=15)
                    log.warning("Lookup: аккаунт %s блокировал @%s — разблокировал автоматически", session, bot)
                    self.stats["auto_unblocked"] = self.stats.get("auto_unblocked", 0) + 1
                    return None              # следующий запрос с этой сессии уже пройдёт
                except Exception as e3:
                    log.warning("Lookup: авто-разблокировка @%s на %s не удалась: %s", bot, session, e3)
                self._bot_unresolvable[(session, bot)] = time.monotonic() + _YOU_BLOCKED_COOLDOWN - _UNRESOLVABLE_COOLDOWN
                log.error("Lookup: аккаунт %s заблокировал @%s — не пробуем %.0f ч. "
                          "Разблокируйте бота в этом аккаунте вручную.",
                          session, bot, _YOU_BLOCKED_COOLDOWN / 3600)
                now_t = time.time()
                last = self._blocked_notified.get((session, bot), 0)
                if self.on_bot_blocked and now_t - last > 86400:
                    self._blocked_notified[(session, bot)] = now_t
                    try:
                        await self.on_bot_blocked(session, bot)
                    except Exception as e2:
                        log.debug("on_bot_blocked notify failed: %s", e2)
            else:
                log.warning("Lookup: не удалось написать @%s с %s: %s", bot, session, e)
            return None
        self._last_ask[session] = time.monotonic()
        self._last_response_text.pop(session, None)      # очистим перед новым запросом
        # резолв прошёл — чистим флаг «unresolvable», иначе 10мин блок висел бы даже после восстановления
        self._bot_unresolvable.pop((session, bot), None)
        # Бот ответил хотя бы одной сессии — снимаем глобальный dead-mark, если был
        self._clear_bot_unresolved(bot)
        sent_id = sent.id

        async def _poll(client, min_id_arg):
            ent = await client.get_input_entity(bot)
            if min_id_arg is None:
                msgs = await client.get_messages(ent, limit=5)  # после капчи: читаем всё, вдруг funstat редактирует
            else:
                msgs = await client.get_messages(ent, limit=10, min_id=min_id_arg)
            return [m for m in reversed(msgs) if not getattr(m, "out", False)]

        deadline = time.monotonic() + max(5.0, settings.lookup_timeout)
        prev_sig, stable, last_cands = None, 0, []
        got_answer = False
        captcha_rounds = 0        # защита от бесконечного цикла капча→клик→новая капча. 5 капч подряд —
                                  # уже явный бот-брутфорс, уходим. Обычно 1-2 капчи хватает.
        poll_min_id = sent_id     # после решения капчи ставим None (читаем всё — funstat может редактировать)
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            try:
                msgs = await self._run(session, lambda c, _mid=poll_min_id: _poll(c, _mid), timeout=15)
            except Exception as e:
                log.debug("Lookup poll failed: %s", e)
                continue
            if not msgs:
                continue
            cap = next((m for m in msgs if is_captcha(m)), None)
            if cap is not None:
                if not _retry or captcha_rounds >= 5:
                    # 5 капч подряд на одном /human — больше не вяжемся, уходим
                    # (user: 'если не прошел с 1 раза пусть дальше кидает' → даём больше попыток)
                    if captcha_rounds >= 5:
                        log.warning("Lookup: @%s зациклил капчу на %r (5-я подряд) — сдаёмся", bot, seller_name)
                        self.stats["captcha_loop_give_up"] = self.stats.get("captcha_loop_give_up", 0) + 1
                    return None
                captcha_rounds += 1
                solved = await self._wait_captcha(session, bot, cap)
                if not solved:
                    return None
                # Капчу прошли. НЕ шлём новый /human — funstat после решения капчи
                # САМ отвечает на предыдущую команду (повторная отправка → цикл).
                # Продляем дедлайн, читаем ВСЕ последние (funstat может редактировать капча-сообщение).
                deadline = time.monotonic() + max(5.0, settings.lookup_timeout)
                poll_min_id = None     # после капчи: читаем все последние, не только с min_id
                prev_sig, stable, last_cands = None, 0, []
                await asyncio.sleep(2.0)  # даём боту время обработать нажатие
                continue
            # funstat ответил чем-то кроме капчи — значит прошлая капча (если была) прошла нормально
            got_answer = True
            last_cands = parse_candidates(msgs, bot)
            text = "\n".join((m.message or "") for m in msgs)
            self._last_response_text[session] = text        # для rate-limit детекции в _find
            sig = (text, tuple(c.key() for c in last_cands),
                   tuple(getattr(m, "edit_date", None) for m in msgs))
            waiting = not last_cands and any(w in text.lower() for w in _WAIT_WORDS)
            if last_cands and not any(w in text.lower() for w in _WAIT_WORDS):
                break                      # список людей уже пришёл — дальше не ждём
            if sig == prev_sig and not waiting:
                stable += 1
                if stable >= 1:            # ответ не менялся между двумя опросами — готов
                    break
            else:
                stable = 0
            prev_sig = sig
        if not got_answer:
            log.warning("Lookup: @%s не ответил на %r за %.0f с", bot, cmd, settings.lookup_timeout)
            return None
        return last_cands

    async def _wait_captcha(self, session: str, bot: str, msg: Any) -> bool:
        """Бот поиска показал капчу: ВСЕГДА пробуем решить сами, админу — только
        если OCR вернул None (мусор, не читается)."""
        self.stats["captcha"] += 1
        # Регистрируем капчу в скользящем окне для адаптивного lookup_interval
        recent = getattr(self, "_captcha_recent", None)
        if recent is None:
            self._captcha_recent = recent = []
        recent.append(time.monotonic())
        # Не даём списку бесконтрольно расти: держим последние 5 мин
        if len(recent) > 500:
            cutoff = time.monotonic() - 300
            self._captcha_recent = [t for t in recent if t >= cutoff]
        token = f"{session}-{msg.id}"[-40:]
        try:
            return await self._wait_captcha_impl(session, bot, msg, token)
        finally:
            # Защита от утечек при любом сценарии (включая CancelledError)
            self._captcha.pop(token, None)
            if self._captcha_by_session.get(session) == token:
                self._captcha_by_session.pop(session, None)

    async def _wait_captcha_impl(self, session: str, bot: str, msg: Any, token: str) -> bool:
        if token not in self._captcha:
            buttons = _buttons(msg)
            ev = asyncio.Event()
            self._captcha[token] = {"session": session, "bot": bot, "msg_id": msg.id,
                                    "buttons": buttons, "event": ev, "photo": None}
            self._captcha_by_session[session] = token

            async def _photo(client):
                try:
                    return await client.download_media(msg, file=bytes)
                except Exception:
                    return None
            # 2 попытки скачать картинку — Telegram иногда возвращает FILE_REFERENCE_EXPIRED
            # или кратковременные ошибки, повторная загрузка через 1с обычно проходит
            photo = None
            for attempt in range(2):
                try:
                    photo = await self._run(session, _photo, timeout=30)
                except Exception:
                    photo = None
                if photo:
                    break
                if attempt == 0:
                    await asyncio.sleep(1.0)
            self._captcha[token]["photo"] = photo

            # ВСЕГДА пробуем сами. User явно просил не напрягать его капчами.
            # solve_ranked возвращает топ-3 кандидата от OCR — если funstat назвал
            # первую кнопку неверной, пробуем вторую-третью без прохода через админа.
            idxs: list[int] = []
            tried: set[int] = set()
            ocr_stale = False
            if self._auto is not None and getattr(self._auto, "enabled", False) and photo and buttons:
                t_start = time.monotonic()
                try:
                    idxs = await self._auto.solve_ranked(photo, [t for t, _ in buttons])
                except Exception as e:                               # noqa: BLE001
                    idxs = []
                    log.warning("Lookup: авто-решатель капчи упал: %s", e)
                for idx in idxs:
                    if idx in tried:
                        continue
                    tried.add(idx)
                    res = await self.solve_captcha(token, idx)
                    if res.startswith("Нажал") and "funstat сказал" not in res:
                        self.stats["captcha_auto"] += 1
                        solve_time = time.monotonic() - t_start
                        avg = self.stats.get("captcha_auto_avg_sec", solve_time)
                        self.stats["captcha_auto_avg_sec"] = avg * 0.8 + solve_time * 0.2
                        llog("LOOKUP", f"@{bot} на {session}: капчу решил авто-солвер за {solve_time:.1f}s → "
                                       f"кнопка {idx + 1}"
                                       + (f" (с {len(tried)}-й попытки)" if len(tried) > 1 else ""))
                        self._captcha.pop(token, None)
                        if self._captcha_by_session.get(session) == token:
                            self._captcha_by_session.pop(session, None)
                        await asyncio.sleep(2.0)
                        return True
                    log.info("Lookup: авто-клик %d (из топ-%d) не прошёл (%s)",
                             idx + 1, len(idxs), res)
                    # Капча стала «неактуальной» — funstat её сменил. Остальные idxs шлют в молоко,
                    # лучше дать боту прислать новую (следующая итерация _ask_bot_locked её прочтёт).
                    if "неактуальна" in res or "Неизвестная кнопка" in res:
                        ocr_stale = True
                        break
                    # funstat мог заменить капчу новой — пауза даёт боту время
                    await asyncio.sleep(1.5)
                    # перепроверим: токен ещё активен? Иначе funstat уже сдался или
                    # параллельно админ успел нажать и закрыть капчу
                    c_now = self._captcha.get(token)
                    if c_now is None or c_now.get("event", asyncio.Event()).is_set():
                        break
                if idxs:
                    log.warning("Lookup: все %d авто-кандидатов не прошли — передаю админам", len(idxs))
                    self.stats["captcha_auto_fail"] = self.stats.get("captcha_auto_fail", 0) + 1

            # 2a) РАНДОМ-фоллбек. user: «если не прошёл с 1 раза, тыкает по рандомному и всё».
            # ПОСЛЕДНИЙ ЭШЕЛОН: независим от OCR/vision — работает даже если солвер упал
            # при инициализации (self._auto = None), или отключён (CAPTCHA_AUTO_SOLVE=false),
            # или картинка не скачалась (photo = None — для ТЫКА картинка не нужна).
            # Отключается ТОЛЬКО через CAPTCHA_RANDOM_FALLBACK=false.
            random_enabled = self._random_fallback
            captcha_stale = ocr_stale   # унаследуем от OCR-цикла — не перезапускать рандом по мёртвой капче
            if buttons and random_enabled and not captcha_stale:
                import random as _r
                remaining = [i for i in range(len(buttons)) if i not in tried]
                _r.shuffle(remaining)
                for idx in remaining:
                    tried.add(idx)
                    res = await self.solve_captcha(token, idx)
                    if res.startswith("Нажал") and "funstat сказал" not in res:
                        self.stats["captcha_random"] = self.stats.get("captcha_random", 0) + 1
                        llog("LOOKUP", f"@{bot} на {session}: капчу решил РАНДОМ → кнопка {idx + 1} "
                                       f"(после {len(tried)} попыток)")
                        self._captcha.pop(token, None)
                        if self._captcha_by_session.get(session) == token:
                            self._captcha_by_session.pop(session, None)
                        await asyncio.sleep(2.0)
                        return True
                    log.info("Lookup: рандом-клик %d не прошёл (%s)", idx + 1, res)
                    # Капча «неактуальна» → funstat её сменил ИЛИ прислал новую (мы её увидим в
                    # следующем _ask_bot_locked poll). Если «Неизвестная кнопка» — больше нечего пробовать.
                    # «Wrong!» или DataInvalid — funstat уже заменил капчу: дальше тыкать в старую
                    # бессмысленно (в логах: «рандом-клик 3 … DataInvalidError»), ждём новую
                    if ("неактуальна" in res or "Неизвестная кнопка" in res
                            or "funstat сказал" in res or "DataInvalid" in res):
                        captcha_stale = True
                        break
                    await asyncio.sleep(1.5)
                    c_now = self._captcha.get(token)
                    if c_now is None or c_now.get("event", asyncio.Event()).is_set():
                        break

            # 2) без картинки — админ не сможет её решить, и без на кеш попасть нечему.
            #    Не ждём 90с впустую, сразу уходим — лот попробует следующую сессию.
            if photo is None:
                log.warning("Lookup: @%s на %s — капча без картинки (download failed?), уходим без ожидания",
                            bot, session)
                self._captcha.pop(token, None)
                if self._captcha_by_session.get(session) == token:
                    self._captcha_by_session.pop(session, None)
                return False
            # 2b) без кнопок админ тоже не может решить (не на что жать) — не вешаем ему пустую
            # клавиатуру и не ждём 90с; освобождаем сессию, лот пробует следующую
            if not buttons:
                log.warning("Lookup: @%s на %s — у капчи нет inline-кнопок (новый формат?), уходим",
                            bot, session)
                self._captcha.pop(token, None)
                if self._captcha_by_session.get(session) == token:
                    self._captcha_by_session.pop(session, None)
                return False
            # 2c) мы перепробовали ВСЕ кнопки (OCR + рандом), funstat сказал «wrong» на каждую.
            # Админ ничего нового не нажмёт (все кнопки «wrong»), только бессмысленно ждать 90с.
            # Выходим — funstat пришлёт новую капчу в следующей итерации _ask_bot_locked.
            if buttons and len(tried) >= len(buttons):
                log.warning("Lookup: @%s на %s — все %d кнопок перепробованы, funstat не принял — "
                            "выходим, ждём новую капчу", bot, session, len(buttons))
                self._captcha.pop(token, None)
                if self._captcha_by_session.get(session) == token:
                    self._captcha_by_session.pop(session, None)
                return False
            # 2c-bis) funstat явно сказал «капча неактуальна» (она сменилась или закрылась) —
            # ни ждать админа, ни пробовать ту же капчу бессмысленно.
            if captcha_stale:
                log.info("Lookup: @%s на %s — funstat сказал капча неактуальна, не шлём админу", bot, session)
                self._captcha.pop(token, None)
                if self._captcha_by_session.get(session) == token:
                    self._captcha_by_session.pop(session, None)
                return False
            # 2d) если event уже set (кто-то решил капчу параллельно — редко, но бывает) — не шлём админу
            c_check = self._captcha.get(token)
            if c_check and c_check.get("event", asyncio.Event()).is_set():
                log.info("Lookup: @%s на %s — капча решена параллельно, не шлём админу", bot, session)
                self._captcha.pop(token, None)
                if self._captcha_by_session.get(session) == token:
                    self._captcha_by_session.pop(session, None)
                return True
            # 3) fallback — админ
            if self.on_captcha is None:
                log.warning("Lookup: @%s просит капчу на %s — пройдите её вручную в этом аккаунте", bot, session)
                self._captcha.pop(token, None)
                if self._captcha_by_session.get(session) == token:
                    self._captcha_by_session.pop(session, None)
                return False
            notify_failed = False
            try:
                await self.on_captcha(token, session, photo, [t for t, _ in buttons])
            except Exception as e:
                log.warning("Lookup: не удалось отправить капчу админам: %s", e)
                notify_failed = True
            if notify_failed:
                # Админы не получили капчу (бот без прав, админов нет, сеть) — ждать админа
                # смысла нет. Освобождаем сессию для следующего лота.
                self._captcha.pop(token, None)
                if self._captcha_by_session.get(session) == token:
                    self._captcha_by_session.pop(session, None)
                return False
            llog("LOOKUP", f"@{bot} на {session} просит капчу — отправил админам")
        c = self._captcha[token]
        try:
            await asyncio.wait_for(c["event"].wait(), timeout=max(30.0, settings.lookup_captcha_wait))
            await asyncio.sleep(2.0)              # даём боту принять ответ
            return True
        except asyncio.TimeoutError:
            log.warning("Lookup: капчу на %s никто не прошёл за %.0f с", session, settings.lookup_captcha_wait)
            return False
        finally:
            self._captcha.pop(token, None)
            if self._captcha_by_session.get(session) == token:
                self._captcha_by_session.pop(session, None)

    async def _verify(self, session: str, cands: list[Candidate], seller_name: str,
                      slug: str | None, unique_id: int | None,
                      require_nft: bool = False) -> FoundOwner | None:
        """Проверка кандидатов: тот, у кого этот NFT открыт в профиле, и есть владелец.

        ВАЖНО: открыть аккаунт по @username (contacts.resolveUsername) — «дорогой» запрос:
        при частых вызовах Telegram даёт FloodWait на ~20 часов. Поэтому:
          • проверяем только кандидатов, у которых имя в ответе бота совпало с именем продавца
            (ни одно не совпало — не больше LOOKUP_NOMATCH_CHECK первых);
          • не больше LOOKUP_RESOLVE_PER_HOUR новых юзов в час на аккаунт;
          • уже открытые юзы запоминаем и повторно не открываем."""
        # дедуп: funstat часто возвращает одного и того же дважды (разные источники — @юз и ID)
        seen_keys: set[str] = set()
        unique_cands: list[Candidate] = []
        for c in cands:
            k = c.key()
            if k not in seen_keys:
                seen_keys.add(k)
                unique_cands.append(c)
        cands = unique_cands

        matched = [c for c in cands if c.name and same_name(c.name, seller_name)]
        todo = matched if matched else cands[:max(0, settings.lookup_nomatch_check)]
        # сначала те, кого можно открыть без resolveUsername (по ID из ответа бота)
        todo = sorted(todo, key=lambda c: c.user_id is None)
        todo = todo[:max(1, settings.lookup_max_candidates)]
        # «есть ровно одно совпадение по имени» — доверяем funstat даже без проверки видимости NFT
        # (многие прячут подарки → profile_has_gift даёт false даже у настоящего владельца)
        trust_without_nft = (not require_nft and settings.lookup_trust_name_match
                             and len(matched) == 1 and todo and todo[0] is matched[0])
        resolve_blocked_matched: Candidate | None = None   # для fallback-доверия, если резолв упёрся в лимит
        # кандидаты, которых мы ТОЧНО проверили (резолв прошёл) и забраковали (NFT нет / имя не то).
        # Их НЕЛЬЗЯ брать в trust-fallback: мы уже знаем, что это не владелец.
        verified_rejected: set[str] = set()

        for c in todo:
            sname, peer = await self._open_peer(session, c)
            if peer is None:
                # резолв не прошёл (лимит/флуд). Если это кандидат с совпавшим именем —
                # запомним как fallback: доверим ему, если ничего лучше не найдём.
                if c in matched and resolve_blocked_matched is None:
                    resolve_blocked_matched = c
                continue

            if not trust_without_nft:
                async def _check(client, peer=peer):
                    return await profile_has_gift(client, peer, slug, unique_id)
                try:
                    ok = await self._run(sname, _check, timeout=30)
                except ProfilePrivate as e:
                    # Профиль закрыт (USER_PRIVACY_RESTRICTED) — мы НЕ ЗНАЕМ есть у него NFT.
                    # НЕ добавляем в verified_rejected: trust-fallback снизу может решить
                    # доверять ему по совпадению имени (это основной кейс для приватных).
                    self.stats["nft_private"] = self.stats.get("nft_private", 0) + 1
                    log.info("Lookup: %s профиль закрыт (%s) — считаем неопределённо", c.key(), e)
                    # Если именно он единственный matched — доверяем сразу (аналог trust_without_nft)
                    if c in matched and resolve_blocked_matched is None:
                        resolve_blocked_matched = c
                    continue
                except Exception as e:
                    log.debug("Lookup: подарки %s не прочитались: %s", c.key(), e)
                    continue
                if not ok:
                    verified_rejected.add(c.key())
                    continue
            else:
                self.stats["trusted_match"] += 1
                llog("LOOKUP", f"{c.key()}: funstat вернул одного с совпавшим именем — "
                               f"верим без проверки NFT (LOOKUP_TRUST_NAME_MATCH)")
            # NFT у него в профиле — дополнительно сверяем НАСТОЯЩЕЕ имя аккаунта с именем владельца
            # и берём его ТЕКУЩИЙ @юз (база бота поиска бывает старой: юз мог перейти к другому человеку)
            async def _full(client, peer=peer):
                return await client.get_entity(peer)
            try:
                u = await self._run(sname, _full, timeout=15)
            except Exception as e:
                log.info("Lookup: профиль %s не прочитался: %s", c.key(), e)
                continue
            real = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x)
            if getattr(u, "bot", False) or getattr(u, "deleted", False):
                verified_rejected.add(c.key())
                continue
            if trust_without_nft and not exact_name(real, seller_name):
                # без NFT-подтверждения «Alex» ≠ «Alexander» — иначе выдаём чужого человека
                self.stats["name_mismatch"] = self.stats.get("name_mismatch", 0) + 1
                llog("LOOKUP", f"{c.key()}: имя {real!r} не точно {seller_name!r}, NFT не проверен — не он")
                verified_rejected.add(c.key())
                continue
            if settings.lookup_name_check and not same_name(real, seller_name):
                self.stats["name_mismatch"] = self.stats.get("name_mismatch", 0) + 1
                llog("LOOKUP", f"{c.key()}: NFT есть, но имя {real!r} ≠ {seller_name!r} — не он")
                verified_rejected.add(c.key())
                continue
            uname = getattr(u, "username", None) or next(
                (x.username for x in getattr(u, "usernames", None) or [] if getattr(x, "active", False)), None)
            return FoundOwner(peer.user_id, uname, real or seller_name, getattr(u, "premium", None), sname)

        # fallback: ничего не проверили (все резолвы упёрлись в лимит, или cands.name пустые).
        # Доверяем funstat если:
        #   1) есть кандидат с совпавшим именем, резолв которого упёрся в лимит, ИЛИ
        #   2) funstat вернул ровно 1 кандидата (всего), ИЛИ
        #   3) funstat вернул ≤3 кандидатов, из которых только ОДИН совпал по имени — берём его.
        # Это откроет множество лотов которые раньше терялись в 'NFT ни у кого не виден'.
        # ВАЖНО: если МЫ УЖЕ проверили хотя бы одного кандидата и ЗАБРАКОВАЛИ (NFT нет / имя не то)
        # — не доверяем funstat'у ВООБЩЕ. База funstat может быть стара, а наша проверка — свежая правда.
        if settings.lookup_trust_name_match and not verified_rejected and not require_nft:
            trust_cand = resolve_blocked_matched
            if trust_cand is None and len(cands) == 1:
                c0 = cands[0]
                if c0.user_id or c0.username:
                    trust_cand = c0
                    llog("LOOKUP", f"{trust_cand.key()}: funstat вернул единственного кандидата — "
                                   f"доверяем (LOOKUP_TRUST_NAME_MATCH)")
            elif trust_cand is None and 2 <= len(cands) <= 3:
                # берём только из тех, кто совпал по имени
                matched_valid = [c for c in matched if c.user_id or c.username]
                if len(matched_valid) == 1:
                    trust_cand = matched_valid[0]
                    llog("LOOKUP", f"{trust_cand.key()}: funstat вернул {len(cands)} кандидатов, "
                                   f"один из них совпал по имени — доверяем ему")
            if trust_cand is not None and (trust_cand.user_id or trust_cand.username):
                # Юз из базы funstat бывает СТАРЫМ (юз мог перейти к другому человеку) — поэтому
                # без проверки не постим: открываем аккаунт, берём ТЕКУЩИЕ имя и @username.
                name, peer = await self._open_peer(session, trust_cand)
                owner = await self._confirm_current(name, peer, seller_name) if peer is not None else None
                if owner is not None:
                    self.stats["trusted_match"] += 1
                    return owner
                llog("LOOKUP", f"{trust_cand.key()}: юз из funstat не подтверждён в Telegram — не шлём")
        return None

    async def _open_peer(self, session: str, c: Candidate):
        """Открыть кандидата (InputPeerUser) так, чтобы НЕ терять его из-за часового лимита одной сессии:
          1) своя сессия: кэш или запрос в пределах её лимита;
          2) кэш любой другой сессии — бесплатно;
          3) по @username через здоровый аккаунт, у которого ещё есть лимит (≤2 попытки).
        → (имя сессии, peer) или (None, None). Дальнейшие запросы делать на ЭТОЙ сессии
        (access_hash привязан к аккаунту)."""
        peer = await self._input_user(session, c)
        if peer is not None:
            return session, peer
        for n, w in list(self.sessions.workers.items()):
            if n == session:
                continue
            sess = getattr(getattr(w, "client", None), "session", None)
            if sess is None or not w.is_usable():
                continue
            for k in ((c.user_id,) if c.user_id else ()) + ((c.username,) if c.username else ()):
                try:
                    got = sess.get_input_entity(k)
                except Exception:
                    continue
                if isinstance(got, types.InputPeerUser):
                    self._peers[(n, c.key())] = got
                    return n, got
        if not c.username:
            return None, None
        now_r = time.monotonic()

        def _used(n):
            return sum(1 for t in self._resolves.get(n, []) if now_r - t < 3600)
        calm = [w for w in self.sessions.usable(include_reserved=True)
                if w.name != session and w.is_usable()
                and _used(w.name) < settings.lookup_resolve_per_hour
                and not any(now_r - t < 600 for t in getattr(w, "recent_floods", []))]
        # сначала быстрые (speed ≥ 0.5); если весь флот замедлен — любой спокойный аккаунт
        pool = [w for w in calm if getattr(w, "speed", 1.0) >= 0.5] or calm
        pool.sort(key=lambda w: (-getattr(w, "speed", 1.0), _used(w.name)))
        for w in pool[:2]:
            got = await self._input_user(w.name, c)
            if got is not None:
                self.stats["resolve_other_session"] = self.stats.get("resolve_other_session", 0) + 1
                return w.name, got
        return None, None

    async def _confirm_current(self, name: str, peer, seller_name: str) -> FoundOwner | None:
        """Открыть профиль на сессии name и вернуть владельца с ТЕКУЩИМ @username,
        только если сейчас его зовут точно так же, как продавца на маркете."""
        async def _full(client, peer=peer):
            return await client.get_entity(peer)
        try:
            u = await self._run(name, _full, timeout=10)
        except Exception as e:
            log.info("Lookup: профиль u%s не прочитался на %s: %s", getattr(peer, "user_id", "?"), name, e)
            return None
        if getattr(u, "bot", False) or getattr(u, "deleted", False):
            return None
        real = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x)
        if not exact_name(real, seller_name):
            self.stats["name_mismatch"] = self.stats.get("name_mismatch", 0) + 1
            llog("LOOKUP", f"u{peer.user_id}: сейчас зовут {real!r} ≠ {seller_name!r} — не он")
            return None
        uname = getattr(u, "username", None) or next(
            (x.username for x in getattr(u, "usernames", None) or [] if getattr(x, "active", False)), None)
        if not uname:
            return None
        return FoundOwner(peer.user_id, uname, real, getattr(u, "premium", None), name)

    async def _input_user(self, session: str, c: Candidate):
        """InputPeerUser кандидата: из своего кэша, из кэша сессии Telethon (без запросов), и только
        потом — запросом к Telegram, с лимитом в час (чтобы не словить FloodWait на сутки).
        ВАЖНО: access_hash специфичен к сессии — не используем кэш ДРУГОЙ сессии, иначе получим
        'entity invalid' при реальном запросе на этой сессии."""
        key = (session, c.key())
        if key in self._peers:
            return self._peers[key]
        peer = None
        w = self.sessions.get(session)
        sess = getattr(getattr(w, "client", None), "session", None)
        if sess is not None:
            for k in ((c.username,) if c.username else ()) + ((c.user_id,) if c.user_id else ()):
                try:
                    peer = sess.get_input_entity(k)
                    break
                except Exception:
                    continue
        if peer is None:
            now = time.monotonic()
            hist = [t for t in self._resolves.get(session, []) if now - t < 3600]
            if len(hist) >= settings.lookup_resolve_per_hour:
                self.stats["resolve_limit"] = self.stats.get("resolve_limit", 0) + 1
                log.info("Lookup: лимит %s новых юзов в час на %s — %s пропущен",
                         settings.lookup_resolve_per_hour, session, c.key())
                self._resolves[session] = hist
                return None
            hist.append(now)
            self._resolves[session] = hist

            async def _resolve(client):
                if c.username:
                    return await client.get_input_entity(c.username)
                return await client.get_input_entity(types.PeerUser(c.user_id))
            try:
                peer = await self._run(session, _resolve, timeout=20)
            except Exception as e:
                log.info("Lookup: %s не открылся на %s: %s", c.key(), session, e)
                return None
        if not isinstance(peer, types.InputPeerUser):
            return None
        self._peers[key] = peer
        if len(self._peers) > 20000:
            self._peers.clear()
        return peer
