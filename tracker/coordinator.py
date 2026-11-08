"""Conservative new-listing detection over the existing scheduling engine."""
from __future__ import annotations
import time
from config import settings
from tracker.state import MarketState
from database.models import utcnow_iso
from . import coordinator_core as _core
from .coordinator_core import *


def __getattr__(name):
    return getattr(_core, name)


class MarketplaceCoordinator(_core.MarketplaceCoordinator):
    async def _load_state(self):
        await super()._load_state()
        if getattr(settings, "strict_new_lots", True):
            for st in self.states.values():
                if await self.db.kv_get(f"strict_baseline:v1:{st.gift_id}") != "1":
                    st.complete = False
                    st.needs_full_sync = True

    async def full_sync(self, st, reason):
        if not getattr(settings, "strict_new_lots", True):
            return await super().full_sync(st, reason)
        seen, first_top = set(), []
        session, count, reached_end = None, 0, False
        max_pages = settings.initial_sync_pages or 10**9
        for _ in range(max(1, settings.sync_passes)):
            offset, reached_end, offsets = "", False, set()
            for page_number in range(max_pages):
                if offset in offsets:
                    break  # Broken/repeated API cursors must not hang a worker.
                offsets.add(offset)
                page, session = await self._fetch(st.gift_id, offset, 100)
                count = page.count
                if page_number == 0:
                    first_top = [it.listing_id for it in page.items[:settings.page_limit]]
                fresh = [it for it in page.items if it.unique_id not in seen]
                seen.update(it.unique_id for it in page.items)
                await self.db.upsert_market_items(self._row(it, MarketState.KNOWN) for it in fresh)
                if not page.next_offset or not page.items:
                    reached_end = True
                    break
                offset = page.next_offset
            if reached_end and len(seen) >= count:
                break
            if not reached_end:
                break
        st.synced = True
        st.complete = reached_end and len(seen) >= count
        st.partial = not st.complete
        st.needs_full_sync = not st.complete
        st.baseline_pending = bool(seen)
        st.last_count = count
        st.prev_top = first_top
        st.last_cursor = first_top[0] if first_top else None
        st.last_poll, st.last_session = utcnow_iso(), session
        await self.db.save_collection_state(st.to_row())
        await self.db.kv_set(f"strict_baseline:v1:{st.gift_id}", "1" if st.complete else "0")
        self._dirty.discard(st.gift_id)
        log.info("Strict baseline %s: seen=%s count=%s complete=%s; sent=0", st.title, len(seen), count, st.complete)
        return len(seen)

    async def _apply(self, st, cls, items, session, mode, count=None):
        if not getattr(settings, "strict_new_lots", True):
            return await super()._apply(st, cls, items, session, mode, count)
        self.stats["polls"] += 1
        if cls is None or not cls.decided:
            self.stats["resyncs"] += 1
            st.complete, st.needs_full_sync = False, True
            await self.db.upsert_market_items(self._row(it, MarketState.SILENT_OLD) for it in items)
            self._dirty.add(st.gift_id)
            return 0
        fresh, silent = list(cls.new), list(cls.silent)
        quiet = (not st.complete or st.partial or st.baseline_pending
                 or (mode == "recover" and settings.start_fresh))
        if quiet:
            silent += [(it, "unverified_or_baseline") for it in fresh]
            fresh = []
        st.baseline_pending = False
        self.stats["price_moves"] += len(cls.moved)
        self.stats["silent"] += len(silent)
        admitted = 0
        if fresh:
            admitted = await self.pipeline.admit(
                fresh, session, mode, floor_stars=st.ref_floor(time.monotonic()),
                t_detect=self._poll_t0.get(st.gift_id) if mode == "live" else None)
            st.last_seen_listing_id, st.last_seen_timestamp = fresh[0].listing_id, utcnow_iso()
        rows = [self._row(it, MarketState.NEW) for it in fresh]
        rows += [self._row(it, MarketState.PRICE_MOVED) for it, _ in cls.moved]
        rows += [self._row(it, MarketState.SILENT_OLD) for it, _ in silent]
        await self.db.upsert_market_items(rows)
        if cls.known:
            await self.db.touch_market_items(it.unique_id for it in cls.known)
        if count is not None:
            st.last_count = count
        if items:
            st.last_cursor = items[0].listing_id
            st.prev_top = [it.listing_id for it in items[:settings.page_limit]]
        st.last_poll, st.last_session = utcnow_iso(), session
        self._dirty.add(st.gift_id)
        return admitted
