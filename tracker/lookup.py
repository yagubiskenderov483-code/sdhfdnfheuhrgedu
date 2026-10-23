"""
Автопоиск скрытого владельца через бота поиска (LOOKUP_BOT_USERNAME, например funstat).

Раньше в посте была кнопка «🔍 Клик», которая открывала бота с предзаполненным «/human <имя>» —
отправлять команду и проверять кандидатов приходилось вручную. Теперь это делает трекер сам:

  1. один из аккаунтов (LOOKUP_SESSION или первый рабочий) пишет боту «/human <имя продавца>»;
  2. ждёт ответ (бот может сначала прислать «ищу…» и потом отредактировать сообщение — это учтено);
  3. из ответа достаёт кандидатов: @username, ссылки t.me/…, tg://user?id=…, упоминания по ID;
  4. у каждого кандидата смотрит открытые NFT в профиле (payments.getSavedStarGifts) и ищет
     ИМЕННО этот лот по slug / id. Владелец считается найденным только если NFT виден у него в профиле.

Пост в канал уходит сразу, как и раньше (поиск не замедляет отправку); когда владелец найден,
пост редактируется: в строке продавца появляется @username, кнопка «Написать» ведёт к нему.

Запросы к боту идут строго по одному (иначе ответы перепутаются), ответы кэшируются по имени,
чтобы не тратить лимиты бота поиска на одно и то же имя.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from telethon import types
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
_MAX_PENDING = 30                 # не больше N лотов в очереди на поиск (дальше — пропускаем)
_CACHE_TTL = 3600.0               # кандидаты по одному имени — час


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
    """Очередь поиска владельцев через бота поиска. Один разговор с ботом за раз."""

    def __init__(self, sessions):
        self.sessions = sessions
        self._lock = asyncio.Lock()
        self._pending = 0
        self._cache: dict[str, tuple[float, list[Candidate]]] = {}
        self._started: set[tuple[str, str]] = set()      # (session, bot) — /start уже отправляли
        self.stats = {"lookups": 0, "found": 0, "not_found": 0, "no_answer": 0, "skipped": 0}

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
            async with self._lock:
                return await self._find(seller_name, slug, unique_id)
        finally:
            self._pending -= 1

    # ------------------------------------------------------------------ internals
    def _pick_session(self) -> str | None:
        want = (settings.lookup_session or "").strip()
        if want:
            w = self.sessions.get(want)
            return want if w is not None and w.is_usable() else None
        usable = sorted(w.name for w in self.sessions.usable())
        return usable[0] if usable else None

    async def _run(self, session: str, fn, timeout: float = 30):
        res, _ = await self.sessions.run(fn, only=session, attempts=1, timeout=timeout)
        return res

    async def _find(self, seller_name: str, slug: str | None, unique_id: int | None) -> FoundOwner | None:
        bot = settings.lookup_bot_username.lstrip("@")
        session = self._pick_session()
        if session is None:
            log.warning("Lookup: нет рабочего аккаунта%s", f" {settings.lookup_session}"
                        if settings.lookup_session else "")
            return None
        self.stats["lookups"] += 1
        key = _norm_name(seller_name)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL:
            cands = cached[1]
        else:
            cands = await self._ask_bot(session, bot, seller_name)
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
        cmd = f"/human {seller_name}"

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
                return FoundOwner(user.id, uname, full, getattr(user, "premium", None))
        return None
