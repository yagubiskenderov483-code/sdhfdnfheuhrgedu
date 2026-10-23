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
_MAX_PENDING = 300                # не больше N лотов в очереди на поиск (дальше — пропускаем)
_CACHE_TTL = 3600.0               # кандидаты по одному имени — час
_CAPTCHA_WORDS = ("captcha", "капч", "rasmga", "click the button")


@dataclass
class Candidate:
    username: str | None = None
    user_id: int | None = None

    def key(self) -> str:
        return f"@{self.username.lower()}" if self.username else f"id{self.user_id}"


@dataclass
class FoundOwner:
    user_id: int
    username: str | None
    name: str | None
    premium: bool | None = None
    session: str | None = None          # аккаунт, который видит этого пользователя (есть access_hash)


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
                add(Candidate(user_id=ent.user_id))
            elif isinstance(ent, types.MessageEntityTextUrl):
                add(_from_url(ent.url))
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
    return out


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


def is_captcha(msg: Any) -> bool:
    """Бот поиска просит пройти капчу (картинка + «нажми кнопку с похожим значком»)."""
    text = (getattr(msg, "message", None) or "").lower()
    return any(w in text for w in _CAPTCHA_WORDS) and any(d for _, d in _buttons(msg))


def _gift_matches(saved: Any, slug: str | None, unique_id: int | None) -> bool:
    g = getattr(saved, "gift", None)
    if not isinstance(g, types.StarGiftUnique):
        return False
    if slug and (getattr(g, "slug", None) or "").lower() == slug.lower():
        return True
    return bool(unique_id) and getattr(g, "id", None) == unique_id


async def profile_has_gift(client, entity, slug: str | None, unique_id: int | None,
                           max_pages: int = 5) -> bool:
    """Виден ли этот NFT в профиле пользователя (скрытые подарки Telegram не отдаёт)."""
    offset = ""
    for _ in range(max_pages):
        res = await client(GetSavedStarGiftsRequest(peer=entity, offset=offset, limit=100,
                                                    exclude_unlimited=True, exclude_upgradable=True,
                                                    exclude_unupgradable=True))
        if any(_gift_matches(s, slug, unique_id) for s in getattr(res, "gifts", None) or []):
            return True
        offset = getattr(res, "next_offset", None) or ""
        if not offset:
            return False
    return False


class OwnerLookup:
    """Поиск владельцев через бота поиска. На одном аккаунте — один разговор с ботом за раз
    (иначе ответы перепутаются); несколько аккаунтов поиска работают параллельно."""

    def __init__(self, sessions):
        self.sessions = sessions
        self._slocks: dict[str, asyncio.Lock] = {}
        self._pending = 0
        self._cache: dict[str, tuple[float, list[Candidate]]] = {}
        self._started: set[tuple[str, str]] = set()      # (session, bot) — /start уже отправляли
        self.stats = {"lookups": 0, "found": 0, "not_found": 0, "no_answer": 0, "skipped": 0, "captcha": 0}
        # капча: решает человек. on_captcha(token, session, photo_bytes, button_texts) — задаёт App
        # (шлёт админам картинку и кнопки); нажатие админа → solve_captcha(token, index).
        self.on_captcha = None
        self._captcha: dict[str, dict] = {}              # token → {session, bot, msg_id, buttons, event}
        self._captcha_by_session: dict[str, str] = {}    # session → token активной капчи
        self._last_ask: dict[str, float] = {}            # session → когда последний раз писали боту

    # ------------------------------------------------------------------ public
    def enabled(self) -> bool:
        return bool(settings.auto_lookup and settings.lookup_bot_username)

    async def find_owner(self, seller_name: str, slug: str | None, unique_id: int | None) -> FoundOwner | None:
        if not self.enabled() or not seller_name or not (slug or unique_id):
            return None
        if self._pending >= _MAX_PENDING:
            self.stats["skipped"] += 1
            log.warning("Lookup queue full (%s) — %r skipped", self._pending, seller_name)
            return None
        self._pending += 1
        try:
            return await self._find(seller_name, slug, unique_id)
        finally:
            self._pending -= 1

    def pending_captchas(self) -> int:
        return len(self._captcha)

    async def solve_captcha(self, token: str, index: int) -> str:
        """Админ нажал кнопку капчи → жмём ту же кнопку на аккаунте поиска."""
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
                await client(GetBotCallbackAnswerRequest(peer=ent, msg_id=msg_id, data=data))
            except Exception as e:          # бот часто не отвечает на callback (timeout) — нажатие всё равно прошло
                if "timeout" not in str(e).lower() and "BOT_RESPONSE_TIMEOUT" not in str(e):
                    raise
        try:
            await self._run(c["session"], _click, timeout=20)
        except Exception as e:
            return f"Не удалось нажать: {type(e).__name__}"
        c["event"].set()
        return "Нажал, продолжаю поиск"

    # ------------------------------------------------------------------ internals
    def _pick_sessions(self) -> list[str]:
        """Аккаунты для бота поиска: выделенные (LOOKUP_SESSIONS) по кругу, иначе первый рабочий."""
        want = [n for n in settings.lookup_sessions if n]
        if want:
            ok = [n for n in want if (w := self.sessions.get(n)) is not None and w.is_usable()]
            if ok:
                self._rr = (getattr(self, "_rr", -1) + 1) % len(ok)
                order = ok[self._rr:] + ok[:self._rr]
                # свободные аккаунты (не ведут сейчас разговор с ботом) — первыми
                return sorted(order, key=lambda n: (n in self._captcha_by_session, self._slock(n).locked()))
            return []
        usable = sorted(w.name for w in self.sessions.usable(include_reserved=True))
        return usable[:1]

    def _slock(self, session: str) -> asyncio.Lock:
        return self._slocks.setdefault(session, asyncio.Lock())

    async def _run(self, session: str, fn, timeout: float = 30):
        res, _ = await self.sessions.run(fn, only=session, attempts=1, timeout=timeout)
        return res

    async def _find(self, seller_name: str, slug: str | None, unique_id: int | None) -> FoundOwner | None:
        bot = settings.lookup_bot_username.lstrip("@")
        order = self._pick_sessions()
        if not order:
            log.warning("Lookup: нет рабочего аккаунта для бота поиска%s",
                        f" ({', '.join(settings.lookup_sessions)})" if settings.lookup_sessions else "")
            return None
        session = order[0]
        self.stats["lookups"] += 1
        key = _norm_name(seller_name)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL:
            cands = cached[1]
        else:
            cands = None
            for session in order:          # аккаунт не смог (лимит бота, FloodWait) — пробуем следующий
                cands = await self._ask_bot(session, bot, seller_name)
                if cands is not None:
                    break
            if cands is None:
                self.stats["no_answer"] += 1
                return None
            self._cache[key] = (time.monotonic(), cands)
            if len(self._cache) > 5000:
                self._cache.clear()
        if not cands:
            self.stats["not_found"] += 1
            llog("LOOKUP", f"@{bot} /human {seller_name!r}: кандидатов нет")
            return None
        # сначала кандидаты с @username (их бот показывает как «01. Имя (@username)»), потом голые ID
        cands = sorted(cands, key=lambda c: c.username is None)
        found = await self._verify(session, cands[:max(1, settings.lookup_max_candidates)],
                                   seller_name, slug, unique_id)
        if found:
            self.stats["found"] += 1
            llog("LOOKUP", f"/human {seller_name!r} → u{found.user_id}"
                           + (f" @{found.username}" if found.username else "") + " (NFT в профиле)")
        else:
            self.stats["not_found"] += 1
            llog("LOOKUP", f"/human {seller_name!r}: {len(cands)} кандидат(ов), NFT ни у кого не виден")
        return found

    async def _ask_bot(self, session: str, bot: str, seller_name: str) -> list[Candidate] | None:
        """Отправить «/human <имя>» и дождаться ответа. None — бот не ответил / ошибка."""
        async with self._slock(session):
            return await self._ask_bot_locked(session, bot, seller_name)

    async def _ask_bot_locked(self, session: str, bot: str, seller_name: str,
                              _retry: bool = True) -> list[Candidate] | None:
        cmd = f"/human {seller_name}"
        # пауза между запросами с одного аккаунта: частые запросы → бот поиска выдаёт капчу
        gap = settings.lookup_interval - (time.monotonic() - self._last_ask.get(session, 0.0))
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

        try:
            sent = await self._run(session, _send)
        except Exception as e:
            log.warning("Lookup: не удалось написать @%s с %s: %s", bot, session, e)
            return None
        self._last_ask[session] = time.monotonic()
        sent_id = sent.id

        async def _poll(client):
            ent = await client.get_input_entity(bot)
            msgs = await client.get_messages(ent, limit=10, min_id=sent_id)
            return [m for m in reversed(msgs) if not getattr(m, "out", False)]

        deadline = time.monotonic() + max(5.0, settings.lookup_timeout)
        prev_sig, stable, last_cands = None, 0, []
        got_answer = False
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            try:
                msgs = await self._run(session, _poll, timeout=15)
            except Exception as e:
                log.debug("Lookup poll failed: %s", e)
                continue
            if not msgs:
                continue
            cap = next((m for m in msgs if is_captcha(m)), None)
            if cap is not None:
                if not _retry:
                    return None
                solved = await self._wait_captcha(session, bot, cap)
                if not solved:
                    return None
                # капчу прошли — повторяем запрос (бот обычно не выполняет команду, на которой спросил капчу)
                return await self._ask_bot_locked(session, bot, seller_name, _retry=False)
            got_answer = True
            last_cands = parse_candidates(msgs, bot)
            text = "\n".join((m.message or "") for m in msgs)
            sig = (text, tuple(c.key() for c in last_cands),
                   tuple(getattr(m, "edit_date", None) for m in msgs))
            waiting = not last_cands and any(w in text.lower() for w in _WAIT_WORDS)
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
        """Бот поиска показал капчу: отправляем её админам и ждём, пока кто-то нажмёт нужную кнопку."""
        self.stats["captcha"] += 1
        if self.on_captcha is None:
            log.warning("Lookup: @%s просит капчу на %s — пройдите её вручную в этом аккаунте", bot, session)
            return False
        token = f"{session}-{msg.id}"[-40:]
        if token not in self._captcha:
            buttons = _buttons(msg)
            ev = asyncio.Event()
            self._captcha[token] = {"session": session, "bot": bot, "msg_id": msg.id,
                                    "buttons": buttons, "event": ev}
            self._captcha_by_session[session] = token

            async def _photo(client):
                try:
                    return await client.download_media(msg, file=bytes)
                except Exception:
                    return None
            try:
                photo = await self._run(session, _photo, timeout=30)
            except Exception:
                photo = None
            try:
                await self.on_captcha(token, session, photo, [t for t, _ in buttons])
            except Exception as e:
                log.warning("Lookup: не удалось отправить капчу админам: %s", e)
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
                      slug: str | None, unique_id: int | None) -> FoundOwner | None:
        """Проверка кандидатов: тот, у кого этот NFT открыт в профиле, и есть владелец.
        Сначала проверяются кандидаты с тем же именем, что у продавца."""
        want = _norm_name(seller_name)
        resolved: list[tuple[bool, Any]] = []
        for c in cands:
            async def _resolve(client, c=c):
                if c.username:
                    return await client.get_entity(c.username)
                return await client.get_entity(types.PeerUser(c.user_id))
            try:
                user = await self._run(session, _resolve, timeout=20)
            except Exception as e:
                log.debug("Lookup: кандидат %s не открылся: %s", c.key(), e)
                continue
            if not isinstance(user, types.User) or getattr(user, "bot", False):
                continue
            name = _norm_name(" ".join(x for x in (user.first_name, user.last_name) if x))
            resolved.append((name == want, user))
        resolved.sort(key=lambda t: not t[0])           # совпавшие по имени — первыми
        for _, user in resolved:
            async def _check(client, user=user):
                return await profile_has_gift(client, user, slug, unique_id)
            try:
                ok = await self._run(session, _check, timeout=30)
            except Exception as e:
                log.debug("Lookup: подарки u%s не прочитались: %s", user.id, e)
                continue
            if ok:
                uname = getattr(user, "username", None)
                if not uname:
                    for u in getattr(user, "usernames", None) or []:
                        if getattr(u, "active", False):
                            uname = u.username
                            break
                full = " ".join(x for x in (user.first_name, user.last_name) if x) or None
                return FoundOwner(user.id, uname, full, getattr(user, "premium", None), session)
        return None
