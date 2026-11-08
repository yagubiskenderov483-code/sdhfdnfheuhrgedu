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
    def _bot_pool(self):
        result, seen = [], set()
        for raw in [settings.lookup_bot_username] + list(settings.lookup_bots or []):
            bot = (raw or "").strip().lstrip("@")
            if bot and bot.casefold() not in seen:
                seen.add(bot.casefold())
                result.append(bot)
        return result

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

    def _warn_unavailable(self, bot):
        recent = getattr(self, "_warning_times", None)
        if recent is None:
            self._warning_times = recent = {}
        now, key = time.monotonic(), bot.casefold()
        if key in recent and now - recent[key] < 300:
            return
        recent[key] = now
        if not self.on_bot_dead:
            return
        async def notify():
            try:
                await asyncio.wait_for(self.on_bot_dead(bot, None), timeout=10)
            except Exception:
                log.warning("Could not send lookup warning for @%s", bot)
        task = asyncio.create_task(notify())
        if not hasattr(self, "_notify_tasks"):
            self._notify_tasks = set()
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _register_bot_unresolved(self, bot, session=None):
        self._warn_unavailable(bot)

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
                self._warn_unavailable(bot)
        return results

    async def _verify(self, session, cands, seller_name, slug, unique_id):
        unique = list({c.key(): c for c in cands}.values())
        matched = [c for c in unique if c.name and same_name(c.name, seller_name)]
        todo = matched or unique[:max(0, settings.lookup_nomatch_check)]
        for candidate in todo[:max(1, settings.lookup_max_candidates)]:
            try:
                peer = await self._input_user(session, candidate)
                if peer is None:
                    continue
                async def full(client):
                    return await client.get_entity(peer)
                user = await self._run(session, full, timeout=8)
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
                async def check(client):
                    return await profile_has_gift(client, peer, slug, unique_id)
                if not await self._run(session, check, timeout=10):
                    continue
                return FoundOwner(uid, username, name, getattr(user, "premium", None), session)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
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
        except Exception:
            self._warn_unavailable(bot)
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
        self._warn_unavailable(bot)
        return None
