"""Strict delivery policy over the retained queue/dedup/session engine."""
from __future__ import annotations
import asyncio
import time
from config import settings
from database.models import ListingStatus
from filters.seller_filter import SellerResult
from utils.links import listing_gift_link, normalize_username, username_link
from . import queue_core as _core
from .queue_core import *
from .reliability import remaining_seconds


def __getattr__(name):
    return getattr(_core, name)


class ListingPipeline(_core.ListingPipeline):
    async def _discard(self, l, reason, code=None):
        l.status = ListingStatus.FILTERED_SELLER if code else ListingStatus.ERROR
        l.reason = reason
        if code:
            l.seller_filter_result = code
        key = "filtered_seller" if code else "stale"
        self.stats[key] = self.stats.get(key, 0) + 1
        await self.db.update_listing(l)

    async def process(self, ev):
        l = ev.listing
        l.gift_link = listing_gift_link(l)
        l.seller_username = normalize_username(l.seller_username)
        l.seller_profile_link = username_link(l.seller_username)
        if not l.gift_link:
            await self._discard(l, "missing canonical NFT link")
            return
        l.gift_slug = l.gift_link.rsplit("/", 1)[-1]
        # Свежесть: если трекер увидел лот позже MAX_DETECT_SECS после его появления — это уже
        # «старый» лот (его успели увидеть другие), не шлём. Проверяем один раз, до поиска владельца.
        limit = getattr(settings, "max_detect_secs", 0)
        t_detect, t_found = getattr(l, "t_detect", None), getattr(l, "t_found", None)
        if (ev.mode != "lookup" and limit > 0 and t_detect is not None and t_found is not None
                and t_found - t_detect > limit):
            await self._discard(l, f"stale: detected {t_found - t_detect:.0f}s after listing (> {limit}s)")
            return
        left = remaining_seconds(l, settings)
        if left <= 0:
            await self._discard(l, "stale: delivery deadline expired")
            return
        try:
            await asyncio.wait_for(super().process(ev), timeout=left)
        except asyncio.TimeoutError:
            await self._discard(l, "stale: processing/delivery deadline expired; not retried")

    def _needs_lookup(self, l):
        return len(self._lookup_tasks) < 128 and super()._needs_lookup(l)

    async def _lookup_then_process(self, l):
        # Keep the core lookup failure policy: a bot timeout/exception is an
        # infrastructure failure, not proof that the listing/seller is invalid.
        # Core also re-checks whether the NFT is still on sale before continuing.
        return await super()._lookup_then_process(l)

    async def _resolve_hidden_owner(self, l):
        # The old users[] display-name guess is not proof of NFT ownership.
        # The lookup verifier checks the actual gift instead.
        return None

    async def _enrich_seller(self, l):
        cached = self._seller_cache.get(l.seller_id)
        if cached and time.monotonic() - cached[0] >= min(settings.seller_cache_ttl, 60):
            self._seller_cache.pop(l.seller_id, None)
        await super()._enrich_seller(l)
        cached = self._seller_cache.get(l.seller_id)
        if cached:
            l.seller_username = normalize_username(cached[1].username)
            l.seller_profile_link = username_link(l.seller_username)

    async def _check_seller(self, l):
        # Preserve the core infrastructure-failure fallback for verified lookup owners
        # and hidden-owner timeout cases. The wrapper used to convert temporary profile
        # read failures into hard rejects, which amplified FloodWait into lost lots.
        if l.seller_username:
            l.seller_username = normalize_username(l.seller_username)
            l.seller_profile_link = username_link(l.seller_username)
        return await super()._check_seller(l)

    async def _still_on_sale(self, l):
        from telethon import types
        from telethon.tl.functions.payments import GetUniqueStarGiftRequest
        from tracker.marketplace import parse_price

        async def check(client):
            response = await client(GetUniqueStarGiftRequest(slug=l.gift_slug))
            gift = getattr(response, "gift", None)
            if gift is None:
                return None
            if getattr(gift, "id", None) != l.gift_unique_id or not getattr(gift, "resell_amount", None):
                return False
            stars, ton = parse_price(gift.resell_amount)
            if bool(getattr(gift, "resale_ton_only", False)) != bool(l.ton_only):
                return False
            if (l.ton_only or l.stars_price is None):
                if ton != l.ton_nano:
                    return False
            elif stars != l.stars_price:
                return False
            peer = getattr(gift, "owner_id", None) or getattr(gift, "host_id", None)
            if peer is not None:
                if l.seller_type == "user":
                    if not isinstance(peer, types.PeerUser) or peer.user_id != l.seller_id:
                        return False
                elif l.seller_type == "channel":
                    if not isinstance(peer, types.PeerChannel) or peer.channel_id != l.seller_id:
                        return False
                else:
                    return False
            if l.seller_type == "user":
                current = await client.get_entity(l.seller_username)
                if (getattr(current, "id", None) != l.seller_id or getattr(current, "deleted", False)
                        or getattr(current, "bot", False)):
                    return False
                if peer is None:
                    from tracker.lookup import profile_has_gift
                    if not await profile_has_gift(client, current, l.gift_slug, l.gift_unique_id):
                        return False
            elif peer is None:
                return False
            return True

        try:
            left = remaining_seconds(l, settings)
            if left <= 0 or not l.gift_slug:
                return False
            response, _ = await asyncio.wait_for(
                self.sessions.run(check, prefer=l.session_source, attempts=1, timeout=min(10, left), priority=True),
                timeout=min(12, left))
            return response
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _send_stage(self, ev, l, base, t_start):
        # Жёстко: только продавец-человек с реальным @username (не «владелец не найден», не канал).
        if (not normalize_username(l.seller_username) or getattr(l, "owner_not_found", False)
                or l.seller_type != "user" or not l.seller_id or l.seller_id <= 0):
            await self._reject_no_username(l)
            return
        if remaining_seconds(l, settings) <= 0:
            await self._discard(l, "stale: expired before sending")
            return
        sale_state = await self._still_on_sale(l)
        if sale_state is False:
            await self._discard(l, "listing changed or sold during final revalidation")
            return
        if sale_state is None:
            # A transient Telegram/FloodWait/session failure is not evidence that the
            # listing disappeared. Core pipeline historically treats an inconclusive
            # recheck as non-blocking; otherwise healthy lots are mass-dropped during
            # account throttling.
            log.warning("Final sale revalidation inconclusive for %s; continuing", l.listing_id)
        left = remaining_seconds(l, settings)
        if left <= 0:
            await self._discard(l, "stale: expired during sale verification")
            return
        try:
            await asyncio.wait_for(super()._send_stage(ev, l, base, t_start), timeout=left)
        except asyncio.TimeoutError:
            await self._discard(l, "stale: sending deadline expired; not retried")
