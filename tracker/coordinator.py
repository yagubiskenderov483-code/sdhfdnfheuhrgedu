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
        # Keep the mature fast-sync/count-delta logic from coordinator_core. The
        # reliability wrapper previously replaced it and silently dropped burst/new
        # listing handling, causing collections to oscillate into resync.
        return await super()._apply(st, cls, items, session, mode, count)
