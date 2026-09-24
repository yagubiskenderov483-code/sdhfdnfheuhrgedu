"""
MarketplaceCoordinator — единая точка, которая решает, КАКУЮ коллекцию и КОГДА опрашивать,
и раздаёт запросы свободным sessions. Одна коллекция никогда не опрашивается двумя sessions
одновременно (per-collection lock) → нет бессмысленных дублирующих запросов.

Режимы:  INITIALIZING → INITIAL_SYNC (новые коллекции, без отправки) → RECOVERY (после рестарта,
         догоняем пропущенное, отправляем) → LIVE 🟢

Планировщик LIVE:
  • у каждой коллекции есть due-время; каждую коллекцию стараемся опрашивать раз в TRACKER_INTERVAL;
  • параллельно выполняется не больше min(MAX_CONCURRENT_SESSIONS, активных sessions) опросов;
  • каждые CATALOG_INTERVAL секунд payments.getStarGifts: если у коллекции изменился
    availability_resale — она опрашивается вне очереди (hot);
  • реальная частота опроса = (кол-во коллекций) / (sessions × SESSION_RPS) — видно в /status.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from telethon import errors

from config import settings
from database.db import Database
from database.models import utcnow_iso
from sessions.manager import SessionManager
from tracker.marketplace import ResaleItem, fetch_catalog, fetch_resale_page
from tracker.queue import ListingPipeline
from tracker.state import CollectionState, KnownItem, classify
from tracker.worker import WorkerUnavailable
from utils.logger import get_logger

log = get_logger("coordinator")


class TrackerMode:
    INITIALIZING = "INITIALIZING"
    INITIAL_SYNC = "INITIAL_SYNC"
    RECOVERY = "RECOVERY"
    LIVE = "LIVE"
    STOPPED = "STOPPED"


class MarketplaceCoordinator:
    def __init__(self, db: Database, sessions: SessionManager, pipeline: ListingPipeline):
        self.db = db
        self.sessions = sessions
        self.pipeline = pipeline
        self.mode = TrackerMode.INITIALIZING
        self.states: dict[int, CollectionState] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._due: dict[int, float] = {}
        self._running: set[int] = set()
        self._dirty: set[int] = set()
        self._last_poll_mono: dict[int, float] = {}
        self._poll_gap: dict[int, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.stats = {"scanned": 0, "polls": 0, "poll_errors": 0, "silent": 0, "price_moves": 0}
        self.started_at = time.time()
        self.last_catalog_at: float | None = None

    # ------------------------------------------------------------------ helpers
    def _wanted(self, gift_id: int, title: str) -> bool:
        if not settings.collections:
            return True
        wanted = {c.lower() for c in settings.collections}
        return str(gift_id) in wanted or title.lower() in wanted

    def avg_cycle_seconds(self) -> float | None:
        gaps = [g for g in self._poll_gap.values() if g > 0]
        return sum(gaps) / len(gaps) if gaps else None

    async def _load_state(self) -> None:
        known = await self.db.load_market_items()
        for row in await self.db.load_collection_states():
            if not self._wanted(row["gift_id"], row["title"] or ""):
                continue
            st = CollectionState(
                gift_id=row["gift_id"], title=row["title"] or str(row["gift_id"]),
                synced=bool(row["synced"]), complete=bool(row["complete"]), last_cursor=row["last_cursor"],
                last_seen_listing_id=row["last_seen_listing_id"],
                last_seen_timestamp=row["last_seen_timestamp"],
                availability_resale=row["availability_resale"], last_poll=row["last_poll"],
                last_session=row["last_session"])
            st.known = {uid: KnownItem(*v) for uid, v in known.get(st.gift_id, {}).items()}
            self.states[st.gift_id] = st
        for k in ("scanned", "polls"):
            v = await self.db.kv_get(f"stat_{k}")
            if v and v.isdigit():
                self.stats[k] = int(v)
        log.info("Loaded state: %s collections, %s known market items",
                 len(self.states), sum(len(s.known) for s in self.states.values()))

    async def _wait_for_sessions(self) -> None:
        last_log = 0.0
        while not self.sessions.usable():
            if time.monotonic() - last_log > 15:
                log.warning("Waiting for at least one ACTIVE session (add one with /add_session)...")
                last_log = time.monotonic()
            await asyncio.sleep(1)

    # ------------------------------------------------------------------ catalog
    async def refresh_catalog(self) -> None:
        catalog, _ = await self.sessions.run(fetch_catalog)
        self.last_catalog_at = time.time()
        added, hot = 0, 0
        for c in catalog:
            if not self._wanted(c.gift_id, c.title):
                continue
            st = self.states.get(c.gift_id)
            if st is None:
                st = CollectionState(gift_id=c.gift_id, title=c.title, availability_resale=c.availability_resale)
                self.states[c.gift_id] = st
                self._due[c.gift_id] = 0.0
                self._dirty.add(c.gift_id)
                added += 1
                continue
            if st.title != c.title:
                st.title = c.title
                self._dirty.add(c.gift_id)
            if c.availability_resale != st.availability_resale:
                st.availability_resale = c.availability_resale
                self._due[c.gift_id] = 0.0  # hot → опросить немедленно
                self._dirty.add(c.gift_id)
                hot += 1
            self._due.setdefault(c.gift_id, 0.0)
        if added or hot:
            log.info("Catalog: %s collections tracked (+%s new, %s changed)", len(self.states), added, hot)

    # ------------------------------------------------------------------ polling one collection
    async def poll_collection(self, st: CollectionState, mode: str) -> int:
        lock = self._locks.setdefault(st.gift_id, asyncio.Lock())
        async with lock:
            if mode != "sync" and not st.synced:
                mode = "sync"
            limit = 100 if mode in ("sync", "recover") else settings.page_limit
            max_pages = {"sync": settings.initial_sync_pages, "recover": settings.recovery_max_pages}.get(
                mode, settings.live_max_pages)

            items: list[ResaleItem] = []
            seen: set[int] = set()
            offset = ""
            reached_end = False
            cls = None
            session = None
            gid = st.gift_id
            for _ in range(max_pages):
                off = offset
                page, session = await self.sessions.run(lambda c: fetch_resale_page(c, gid, off, limit))
                self.stats["scanned"] += len(page.items)
                for it in page.items:
                    if it.unique_id not in seen:
                        seen.add(it.unique_id)
                        items.append(it)
                reached_end = not page.next_offset or not page.items
                if mode != "sync":
                    cls = classify(st, items, reached_end)
                    if cls.decided:
                        break
                if reached_end:
                    break
                offset = page.next_offset or ""

            self.stats["polls"] += 1
            now_iso = utcnow_iso()
            new: list[ResaleItem] = []

            if mode == "sync":
                st.synced = True
                st.complete = reached_end
                if not settings.initial_sync:
                    new = items   # INITIAL_SYNC=false: существующие лоты тоже обрабатываются
            else:
                if cls is None or not cls.decided:
                    log.warning("[%s] no anchor in %s page(s) — silent resync (%s items)",
                                st.title, max_pages, len(items))
                    st.complete = False
                    self.stats["silent"] += len(items)
                else:
                    new = cls.new
                    self.stats["silent"] += len(cls.silent)
                    self.stats["price_moves"] += len(cls.moved)
                    if reached_end:
                        st.complete = True

            # 1) глобальная дедупликация + запись NEW-листингов в БД (до пометки known — crash-safe)
            if new:
                await self.pipeline.admit(new, session, mode)
                st.last_seen_listing_id = new[0].listing_id
                st.last_seen_timestamp = now_iso
            # 2) обновить state рынка
            rows = st.remember(items)
            if rows:
                await self.db.upsert_market_items(rows)
            if items:
                st.last_cursor = str(items[0].unique_id)
            st.last_poll = now_iso
            st.last_session = session
            self._dirty.add(gid)
            if mode == "sync":
                await self.db.save_collection_state(st.to_row())
                self._dirty.discard(gid)
            return len(new)

    async def _poll_safe(self, st: CollectionState, mode: str) -> int:
        try:
            return await self.poll_collection(st, mode)
        except errors.RPCError as e:
            self.stats["poll_errors"] += 1
            log.warning("[%s] RPC error: %s", st.title, e)
        except (WorkerUnavailable, TimeoutError) as e:
            self.stats["poll_errors"] += 1
            log.warning("[%s] no session available: %s", st.title, e)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.stats["poll_errors"] += 1
            log.exception("[%s] poll failed", st.title)
        return 0

    async def _run_batch(self, states: list[CollectionState], mode: str) -> int:
        sem = asyncio.Semaphore(max(1, min(settings.max_concurrent_sessions, len(self.sessions.usable()) or 1)))

        async def one(s: CollectionState) -> int:
            async with sem:
                return await self._poll_safe(s, mode)

        results = await asyncio.gather(*(one(s) for s in states))
        return sum(results)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.mode = TrackerMode.INITIALIZING
        await self._load_state()
        await self.pipeline.resume_unfinished()
        await self._wait_for_sessions()
        log.info("Marketplace tracker started")

        while True:
            try:
                await self.refresh_catalog()
                break
            except Exception as e:
                log.error("Catalog fetch failed: %s — retry in 10s", e)
                await asyncio.sleep(10)

        unsynced = [s for s in self.states.values() if not s.synced]
        synced = [s for s in self.states.values() if s.synced]

        if unsynced:
            self.mode = TrackerMode.INITIAL_SYNC
            log.info("Initial marketplace sync... (%s collections)", len(unsynced))
            before = sum(len(s.known) for s in self.states.values())
            await self._run_batch(unsynced, "sync")
            after = sum(len(s.known) for s in self.states.values())
            log.info("Existing listings saved: %s", after - before)

        if synced:
            self.mode = TrackerMode.RECOVERY
            log.info("Recovering after restart: checking %s collections for missed listings...", len(synced))
            missed = await self._run_batch(synced, "recover")
            log.info("Recovery done: %s missed new listing(s) queued", missed)

        await self._persist()
        log.info("Initial sync completed")
        self.mode = TrackerMode.LIVE
        log.info("LIVE TRACKER ENABLED 🟢")
        await self.db.kv_set("tracker_mode", self.mode)

        now = time.monotonic()
        for gid in self.states:
            self._due.setdefault(gid, now)
        self._tasks = [
            asyncio.create_task(self._live_loop(), name="live-loop"),
            asyncio.create_task(self._catalog_loop(), name="catalog-loop"),
            asyncio.create_task(self._persist_loop(), name="persist-loop"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self._persist()
        except Exception:
            log.exception("Final persist failed")
        self.mode = TrackerMode.STOPPED

    # ------------------------------------------------------------------ loops
    async def _live_loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            capacity = max(1, min(settings.max_concurrent_sessions, len(self.sessions.usable())))
            due = sorted((t, gid) for gid, t in self._due.items() if t <= now and gid not in self._running)
            for _, gid in due:
                if len(self._running) >= capacity:
                    break
                st = self.states.get(gid)
                if st is None:
                    self._due.pop(gid, None)
                    continue
                self._running.add(gid)
                asyncio.create_task(self._live_poll(st), name=f"poll-{gid}")
            await asyncio.sleep(0.02)

    async def _live_poll(self, st: CollectionState) -> None:
        gid = st.gift_id
        try:
            await self._poll_safe(st, "live")
        finally:
            now = time.monotonic()
            prev = self._last_poll_mono.get(gid)
            if prev is not None:
                g = now - prev
                self._poll_gap[gid] = g if gid not in self._poll_gap else self._poll_gap[gid] * 0.8 + g * 0.2
            self._last_poll_mono[gid] = now
            self._due[gid] = now + settings.tracker_interval
            self._running.discard(gid)

    async def _catalog_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(settings.catalog_interval)
            try:
                await self.refresh_catalog()
            except Exception as e:
                log.warning("Catalog refresh failed: %s", e)

    async def _persist_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5)
            try:
                await self._persist()
            except Exception:
                log.exception("Persist failed")

    async def _persist(self) -> None:
        dirty, self._dirty = self._dirty, set()
        for gid in dirty:
            st = self.states.get(gid)
            if st:
                await self.db.save_collection_state(st.to_row())
        latest = max((s for s in self.states.values() if s.last_seen_timestamp),
                     key=lambda s: s.last_seen_timestamp, default=None)
        kv = {"stat_scanned": self.stats["scanned"], "stat_polls": self.stats["polls"],
              "tracker_mode": self.mode,
              "last_persist": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if latest:
            kv["last_seen_listing_id"] = latest.last_seen_listing_id
            kv["last_seen_timestamp"] = latest.last_seen_timestamp
        await self.db.kv_set_many(kv)
