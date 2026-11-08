"""Verified lookup policies. The core retains session scheduling and captcha UI."""
from __future__ import annotations
import asyncio
import time
from config import settings
from utils.links import normalize_username
from . import lookup_core as _core
from .lookup_core import *


def __getattr__(name):
    return getattr(_core, name)


class OwnerLookup(_core.OwnerLookup):
    _SILENT_COOLDOWN = 600.0   # молчащий бот уходит в конец очереди на 10 мин (но НЕ выключается)
    _WARN_EVERY = 900.0        # одно и то же предупреждение — не чаще раза в 15 мин

    def _silent_map(self):
        m = getattr(self, "_silent_until", None)
        if m is None:
            self._silent_until = m = {}
        return m

    def _bot_pool(self):
        result, seen = [], set()
        for raw in [settings.lookup_bot_username] + list(settings.lookup_bots or []):
            bot = (raw or "").strip().lstrip("@")
            if bot and bot.casefold() not in seen:
                seen.add(bot.casefold())
                result.append(bot)
        # Бот, который недавно молчал, спрашиваем ПОСЛЕДНИМ — иначе каждый лот ждёт его таймаут
        # (из-за этого поиск тормозил). Из пула не убираем: если запасных нет, спросим его же.
        now, silent = time.monotonic(), self._silent_map()
        return sorted(result, key=lambda b: silent.get(b.casefold(), 0) > now)

    async def load_persisted_bot_deads(self):
        self._bot_dead_until.clear()
        return 0

    async def _known(self, name):
        # Old persistent records identify namesakes, not ownership of a new NFT.
        return []

    async def _find(self, seller_name, slug, unique_id, prio=0.0, deadline=None):
        key = _core._norm_name(seller_name)
        self._neg_cache.pop(key, None)
        left = min(90.0, deadline - time.time()) if deadline is not None else 90.0
        if left <= 0:
            raise _core._Expired()
        try:
            return await asyncio.wait_for(super()._find(seller_name, slug, unique_id, prio, deadline), left)
        finally:
            # One gift failing verification must not suppress every seller with that name.
            self._neg_cache.pop(key, None)

    def _warn_unavailable(self, bot, kind="silent"):
        """kind: silent — не отвечает; missing — не находится (снесли/переименовали); back — снова отвечает."""
        bot = (bot or "").lstrip("@")
        if not bot:
            return
        if kind != "back":
            self._silent_map()[bot.casefold()] = time.monotonic() + self._SILENT_COOLDOWN
        recent = getattr(self, "_warning_times", None)
        if recent is None:
            self._warning_times = recent = {}
        warned = getattr(self, "_warned_bots", None)
        if warned is None:
            self._warned_bots = warned = set()
        now, key = time.monotonic(), (bot.casefold(), kind)
        if kind == "back":
            if bot.casefold() not in warned:
                return                       # о восстановлении пишем только если раньше предупреждали
            warned.discard(bot.casefold())
        else:
            if key in recent and now - recent[key] < self._WARN_EVERY:
                return
            recent[key] = now
            warned.add(bot.casefold())
        if not self.on_bot_dead:
            return
        async def notify():
            try:
                await asyncio.wait_for(self.on_bot_dead(bot, None, kind), timeout=10)
            except TypeError:
                await asyncio.wait_for(self.on_bot_dead(bot, None), timeout=10)
            except Exception:
                log.warning("Could not send lookup warning for @%s", bot)
        task = asyncio.create_task(notify())
        if not hasattr(self, "_notify_tasks"):
            self._notify_tasks = set()
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _clear_bot_unresolved(self, bot):
        super()._clear_bot_unresolved(bot)
        key = (bot or "").lstrip("@").casefold()
        if self._silent_map().pop(key, None) is not None:
            log.info("Lookup: @%s снова отвечает", bot)
        self._warn_unavailable(bot, "back")

    def _register_bot_unresolved(self, bot, session=None):
        # Молчащий бот не выключается — только уходит в конец очереди и шлётся предупреждение.
        self._bot_dead_until.pop((bot or "").casefold(), None)
        self._warn_unavailable(bot, "silent")

    async def check_bots_liveness(self):
        results = {}
        sessions = self._pick_sessions()[:2]
        for bot in self._bot_pool():
            if not sessions:
                break
            results[bot] = None
            for session in sessions:
                try:
                    async def probe(client, bot=bot):
                        return await client.get_entity(bot)
                    entity = await asyncio.wait_for(self._run(session, probe, timeout=5), 6)
                    results[bot] = bool(getattr(entity, "bot", False) and not getattr(entity, "deleted", False))
                    if results[bot]:
                        break
                except (ValueError, TypeError):
                    results[bot] = False
                except Exception:
                    results[bot] = None
                    break
            if results[bot] is False:
                self._warn_unavailable(bot, "missing")
        return results

    async def _peer_any_session(self, session, candidate):
        """Открыть кандидата: сначала на своей сессии, а если она упёрлась в лимит ресолвов —
        на других сессиях поиска (раньше всё упиралось в одну сессию → resolve-limit и «не найден»)."""
        peer = await self._input_user(session, candidate)
        if peer is not None:
            return peer, session
        for other in self._pick_sessions():
            if other == session:
                continue
            peer = await self._input_user(other, candidate)
            if peer is not None:
                return peer, other
        return None, None

    async def _verify(self, session, cands, seller_name, slug, unique_id):
        """Владелец подтверждён, если:
          1) этот NFT виден в его профиле — точно он; или
          2) (большинство прячет подарки) бот поиска вернул РОВНО ОДНОГО человека с тем же именем,
             и его ТЕКУЩЕЕ имя в Telegram совпадает с именем продавца на маркете дословно,
             у него есть @username, он не бот и не удалён.
        @username всегда берём из Telegram сейчас, а не из базы бота — поэтому старых юзов нет."""
        unique = list({c.key(): c for c in cands}.values())
        matched = [c for c in unique if c.name and same_name(c.name, seller_name)]
        todo = matched or unique[:max(0, settings.lookup_nomatch_check)]
        todo = sorted(todo, key=lambda c: c.user_id is None)[:max(1, settings.lookup_max_candidates)]
        want = _core._norm_name(seller_name)
        name_only = []
        for candidate in todo:
            try:
                peer, used = await self._peer_any_session(session, candidate)
                if peer is None:
                    continue
                async def full(client, peer=peer):
                    return await client.get_entity(peer)
                user = await self._run(used, full, timeout=8)
                uid = getattr(user, "id", None)
                if (not uid or uid != peer.user_id or (candidate.user_id and uid != candidate.user_id)
                        or getattr(user, "bot", False) or getattr(user, "deleted", False)):
                    continue
                name = " ".join(v for v in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if v)
                if not same_name(name, seller_name):
                    continue
                username = normalize_username(getattr(user, "username", None) or next(
                    (u.username for u in getattr(user, "usernames", None) or [] if getattr(u, "active", False)), None))
                if not username:
                    continue
                owner = FoundOwner(uid, username, name, getattr(user, "premium", None), used)
                async def check(client, peer=peer):
                    return await profile_has_gift(client, peer, slug, unique_id)
                try:
                    has = await self._run(used, check, timeout=10)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    has = None            # подарки скрыты/не прочитались — неизвестно
                if has:
                    self.stats["verified_nft"] = self.stats.get("verified_nft", 0) + 1
                    return owner
                if _core._norm_name(name) == want:
                    name_only.append(owner)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
        if (getattr(settings, "lookup_trust_single_match", True) and len(matched) == 1
                and len(name_only) == 1):
            self.stats["trusted_match"] = self.stats.get("trusted_match", 0) + 1
            llog("LOOKUP", f"{seller_name!r} → u{name_only[0].user_id} @{name_only[0].username}: "
                           "единственное точное совпадение имени, подарки скрыты — принимаем")
            return name_only[0]
        return None

    async def _ask_bot_locked(self, session, bot, seller_name, _retry=True):
        gap = self._adaptive_interval() - (time.monotonic() - self._last_ask.get(session, 0))
        if gap > 0:
            await asyncio.sleep(gap)
        async def send(client):
            entity = await client.get_input_entity(bot)
            if (session, bot) not in self._started:
                if not await client.get_messages(entity, limit=1):
                    await client.send_message(entity, "/start")
                    await asyncio.sleep(1)
                self._started.add((session, bot))
            return await client.send_message(entity, self._bot_cmd(bot, seller_name))
        try:
            sent = await self._run(session, send, timeout=10)
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError):
            self._warn_unavailable(bot, "missing")   # username не резолвится — бота снесли/переименовали
            return None
        except Exception:
            self._warn_unavailable(bot, "silent")
            return None
        self._last_ask[session] = time.monotonic()
        self._last_response_text.pop(session, None)
        end, rounds = time.monotonic() + max(5, settings.lookup_timeout), 0
        previous = None
        while time.monotonic() < end:
            await asyncio.sleep(0.6)
            async def poll(client):
                entity = await client.get_input_entity(bot)
                messages = await client.get_messages(entity, limit=12, min_id=sent.id)
                return [m for m in reversed(messages) if not getattr(m, "out", False)
                        and m.id > sent.id and getattr(m, "reply_to_msg_id", None) in (None, sent.id)]
            try:
                messages = await self._run(session, poll, timeout=5)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if not messages:
                continue
            self._clear_bot_unresolved(bot)
            latest = max(messages, key=lambda m: m.id)
            if is_captcha(latest):
                if not _retry or rounds >= 2:
                    return None
                rounds += 1
                if not await self._wait_captcha(session, bot, latest):
                    return None
                end = time.monotonic() + max(5, settings.lookup_timeout)
                previous = None
                continue
            candidates = parse_candidates(messages, bot)
            text = "\n".join(getattr(m, "message", None) or "" for m in messages)
            self._last_response_text[session] = text
            waiting = any(w in text.lower() for w in _core._WAIT_WORDS)
            if candidates and not waiting:
                return candidates
            if not waiting and previous == text:
                return []
            previous = text
        # No reply is treated as a transient lookup miss, never as a global bot death.
        self._bot_dead_until.pop((bot or "").casefold(), None)
        self._warn_unavailable(bot, "silent")
        return None
