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
        # Keep the proven persisted market baseline from coordinator_core.
        # Forcing every existing collection through a brand-new full baseline on deploy
        # stalls large collections for hours and suppresses all live listings meanwhile.
        await super()._load_state()

    async def full_sync(self, st, reason):
        # Use the mature core sync algorithm. In fast mode it intentionally establishes
        # a safe top-of-market baseline instead of requiring tens of thousands of rows
        # before a collection may enter LIVE.
        return await super().full_sync(st, reason)

    async def _apply(self, st, cls, items, session, mode, count=None):
        if not getattr(settings, "strict_new_lots", True):
            return await super()._apply(st, cls, items, session, mode, count)
        self.stats["polls"] += 1
        if cls is None or not cls.decided:
            self.stats["resyncs"] += 1
            # In fast-sync mode, lack of an anchor in one bounded poll is not proof that
            # the collection needs an expensive full resync. Keep it live and retry the
            # next top-of-market poll; otherwise large collections oscillate back into
            # the sync queue and stop producing listings.
            if getattr(settings, "sync_mode", "fast") == "full":
                st.complete, st.needs_full_sync = False, True
            await self.db.upsert_market_items(self._row(it, MarketState.SILENT_OLD) for it in items)
            self._dirty.add(st.gift_id)
            return 0
        fresh, silent = list(cls.new), list(cls.silent)
        # coordinator_core already distinguishes safe baseline/recovery cases. Do not
        # silence every partial collection: large Telegram markets can remain partial
        # indefinitely, which previously blocked almost all live listings.
        quiet = (st.baseline_pending or (mode == "recover" and settings.start_fresh))
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
