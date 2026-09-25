"""
MarketplaceCoordinator — единая точка, которая решает, КАКУЮ коллекцию и КОГДА опрашивать,
и раздаёт запросы свободным sessions. Одна коллекция никогда не опрашивается двумя sessions
одновременно (per-collection lock) → нет бессмысленных дублирующих запросов.

Режимы:  INITIALIZING → INITIAL_SYNC (ПОЛНАЯ тихая синхронизация новых коллекций: читаются все
         страницы, ничего не отправляется) → RECOVERY (после рестарта: только лоты выше надёжного
         anchor в полностью синхронизированных коллекциях) → LIVE 🟢

Алгоритм определения новизны — tracker/state.py. Источник истины — SQLite (market_items, listings).
Sessions — только транспорт: общего состояния у них нет, всё хранится в одной БД.

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
from tracker.state import CollectionState, KnownItem, MarketState, classify
from tracker.worker import WorkerUnavailable
from utils.logger import get_logger, llog

log = get_logger("coordinator")


def _price(stars: int | None, ton_nano: int | None) -> str:
    parts = []
    if stars is not None:
        parts.append(f"{stars}⭐")
    if ton_nano is not None:
        parts.append(f"{ton_nano / 1e9:g}TON")
    return "/".join(parts) or "none"


def _log_task_death(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception() is not None:
        log.error("Background task %s crashed: %r", t.get_name(), t.exception())


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
        self._sync_retry_at: dict[int, float] = {}
        self._hot_since: dict[int, float] = {}
        self.paused = False
        self.skipped: set[int] = set()
        self._baseline_pending: set[int] = set()  # коллекции, чей первый live-опрос после initial sync — тихий
        self.low_prio: set[int] = set()           # флор ниже порога, режим slow: отслеживаются реже            # коллекции с флором ниже COLLECTION_MIN_FLOOR
        self.hot_latency: float | None = None     # сек от изменения счётчика в каталоге до опроса коллекции
        self._stop = asyncio.Event()
        self.stats = {"scanned": 0, "polls": 0, "poll_errors": 0, "silent": 0, "price_moves": 0,
                      "resyncs": 0}
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
        # Лоты в RAM не грузим — для каждой страницы они берутся из SQLite (lookup_market_items).
        for row in await self.db.load_collection_states():
            if not self._wanted(row["gift_id"], row["title"] or ""):
                continue
            st = CollectionState(
                gift_id=row["gift_id"], title=row["title"] or str(row["gift_id"]),
                synced=bool(row["synced"]), complete=bool(row["complete"]), last_cursor=row["last_cursor"],
                last_seen_listing_id=row["last_seen_listing_id"],
                last_seen_timestamp=row["last_seen_timestamp"],
                availability_resale=row["availability_resale"], last_poll=row["last_poll"],
                last_session=row["last_session"],
                partial=bool(row.get("partial") or 0), last_count=row.get("last_count"))
            if st.synced and st.last_count is None:
                # состояние от версии без проверки неполных коллекций → один раз тихо перечитать
                st.needs_full_sync = True
            self.states[st.gift_id] = st
        for k in ("scanned", "polls"):
            v = await self.db.kv_get(f"stat_{k}")
            if v and v.isdigit():
                self.stats[k] = int(v)
        log.info("Loaded state: %s collections, %s known market items",
                 len(self.states), await self.db.count_market_items())

    async def _wait_for_sessions(self) -> None:
        last_log = 0.0
        while not self.sessions.usable():
            if time.monotonic() - last_log > 15:
                log.warning("Waiting for at least one ACTIVE session (add one with /add_session)...")
                last_log = time.monotonic()
            await asyncio.sleep(1)

    # ------------------------------------------------------------------ catalog
    async def refresh_catalog(self) -> None:
        await self._wait_if_paused()
        catalog, _ = await self.sessions.run(fetch_catalog)
        self.last_catalog_at = time.time()
        added, hot = 0, 0
        skipped_before = set(self.skipped)
        for c in catalog:
            if not self._wanted(c.gift_id, c.title):
                continue
            st = self.states.get(c.gift_id)
            if st is None:
                st = CollectionState(gift_id=c.gift_id, title=c.title, availability_resale=c.availability_resale,
                                     floor_stars=c.resell_min_stars)
                self.states[c.gift_id] = st
                self._due[c.gift_id] = 0.0
                self._dirty.add(c.gift_id)
                added += 1
                self._apply_floor_rule(st, c.resell_min_stars, first=True)
                continue
            if self._apply_floor_rule(st, c.resell_min_stars):
                st.floor_stars = c.resell_min_stars
                st.availability_resale = c.availability_resale
                continue
            if st.title != c.title:
                st.title = c.title
                self._dirty.add(c.gift_id)
            st.floor_stars = c.resell_min_stars
            if c.availability_resale != st.availability_resale:
                st.availability_resale = c.availability_resale
                if c.gift_id in self.low_prio:
                    # дешёвая коллекция: опрос при изменении, но не чаще раза в LOW_PRIORITY_HOT_MIN
                    soon = self._last_poll_mono.get(c.gift_id, 0.0) + settings.low_priority_hot_min
                    self._due[c.gift_id] = min(self._due.get(c.gift_id, soon), max(0.0, soon))
                else:
                    self._due[c.gift_id] = 0.0  # hot → опросить немедленно
                self._hot_since.setdefault(c.gift_id, time.monotonic())
                self._dirty.add(c.gift_id)
                hot += 1
            self._due.setdefault(c.gift_id, 0.0)
        if added or self.skipped != skipped_before:
            live, total = self.sync_progress()
            log.info("Catalog: %s collections tracked, %s skipped by floor < %s Stars (+%s new)",
                     total, len(self.skipped), settings.collection_min_floor, added)

    def _apply_floor_rule(self, st: CollectionState, floor: int | None, first: bool = False) -> bool:
        """Коллекции, где самый дешёвый лот на маркете (флор) ниже COLLECTION_MIN_FLOOR, не отслеживаются —
        там почти все лоты дешевле нужного. Возвращает True, если коллекция пропущена.
        Гистерезис 10%: уже отслеживаемая коллекция выключается, только если флор упал ниже порога на 10%+,
        чтобы она не «мигала» от одной дешёвой продажи."""
        th = settings.collection_min_floor
        gid = st.gift_id
        if th <= 0:
            if (gid in self.skipped) and st.synced:
                st.needs_full_sync = True   # включаем ранее пропущенную → сперва тихий ре-синк
                self._due[gid] = 0.0
            self.skipped.discard(gid)
            self.low_prio.discard(gid)
            return False
        cheap_before = gid in self.skipped or gid in self.low_prio
        tracked = not cheap_before and not first
        limit = th * 0.9 if tracked else th
        cheap = floor is None or floor < limit
        if cheap and settings.collection_floor_mode == "skip":
            self.skipped.add(gid)
            self.low_prio.discard(gid)
            return True
        was_skipped = gid in self.skipped
        self.skipped.discard(gid)
        if cheap:
            # режим slow: дешёвая коллекция тоже отслеживается — сразу при появлении лота (по каталогу),
            # но страховочный обход реже; так дорогие лоты в «дешёвых» коллекциях не теряются
            self.low_prio.add(gid)
        else:
            self.low_prio.discard(gid)
        if was_skipped:
            # Пока коллекция была пропущена, рынок жил — её complete-база устарела. Нельзя сразу слать
            # то, что накопилось (иначе смена порога = выброс старых лотов как «новых»). Заставляем
            # сначала тихую полную ре-синхронизацию, и только потом идут по-настоящему новые лоты.
            if st.synced:
                st.needs_full_sync = True
            self._due[gid] = 0.0
        return False

    # ------------------------------------------------------------------ polling one collection
    # ------------------------------------------------------------------ sharding
    def shard_owner(self, gid: int) -> str | None:
        """За каждой коллекцией закреплена своя session (равномерно по кругу).
        Если она занята/недоступна — запрос берёт любая свободная, коллекция не простаивает."""
        names = sorted(w.name for w in self.sessions.usable())
        if not names:
            return None
        order = sorted(g for g in self.states if g not in self.skipped)
        # коллекция могла только что стать «пропущенной» по флору — тогда просто любой аккаунт
        idx = order.index(gid) if gid in order else gid
        return names[idx % len(names)]

    def shard_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for gid in self.states:
            if gid in self.skipped:
                continue
            n = self.shard_owner(gid)
            if n:
                out[n] = out.get(n, 0) + 1
        return out

    async def _wait_if_paused(self) -> None:
        while self.paused and not self._stop.is_set():
            await asyncio.sleep(1)

    async def set_paused(self, paused: bool) -> None:
        """/pause и /resume: пока на паузе, трекер не делает ни одного запроса к Telegram."""
        self.paused = paused
        await self.db.kv_set("paused", int(paused))
        log.info("Tracker %s", "PAUSED ⏸ — no Telegram requests" if paused else "RESUMED ▶️")

    async def _fetch(self, gid: int, offset: str, limit: int):
        await self._wait_if_paused()
        page, session = await self.sessions.run(lambda c: fetch_resale_page(c, gid, offset, limit),
                                                prefer=self.shard_owner(gid))
        self.stats["scanned"] += len(page.items)
        return page, session

    async def _lookup(self, items: list[ResaleItem]) -> dict[int, KnownItem]:
        rows = await self.db.lookup_market_items(it.unique_id for it in items)
        return {u: KnownItem(*v) for u, v in rows.items()}

    @staticmethod
    def _row(it: ResaleItem, state: str) -> tuple:
        return (it.unique_id, it.gift_id, it.listing_id, it.seller_key, it.stars, it.ton_nano, state)

    async def full_sync(self, st: CollectionState, reason: str) -> int:
        """Тихая ПОЛНАЯ синхронизация: читаем все страницы, регистрируем все лоты, НИЧЕГО не отправляем.
        После неё коллекция complete=True — лот, которого нет в БД, гарантированно не существовал.

        Пока мы листаем, рынок живёт: продажи сдвигают выдачу, и лот на границе страниц может
        проскочить. Поэтому сравниваем число прочитанных уникальных лотов с ResaleStarGifts.count
        и при нехватке делаем ещё проход (до SYNC_PASSES)."""
        gid = st.gift_id
        seen: set[int] = set()
        session, first_id, reached_end, count, pages, stable = None, None, False, 0, 0, False
        max_pages = settings.initial_sync_pages or 10**9
        for pass_no in range(1, settings.sync_passes + 1):
            offset, reached_end, pass_pages = "", False, 0
            seen_before = len(seen)
            while pass_pages < max_pages:
                page, session = await self._fetch(gid, offset, 100)
                pass_pages += 1
                count = page.count
                if pass_no == 1 and pass_pages == 1 and page.items:
                    first_id = page.items[0].listing_id
                fresh = [it for it in page.items if it.unique_id not in seen]
                seen.update(it.unique_id for it in page.items)
                await self.db.upsert_market_items(self._row(it, MarketState.KNOWN) for it in fresh)
                if not page.next_offset or not page.items:
                    reached_end = True
                    break
                offset = page.next_offset
            pages += pass_pages
            gap = count - len(seen)
            added = len(seen) - seen_before
            if not reached_end or gap <= max(1, count // 500):
                break
            if pass_no > 1 and added == 0:
                # повторный проход ничего нового не дал → мы видим всё, что API реально отдаёт
                # (count может учитывать лоты, которые в выдачу не попадают)
                stable = True
                break
            log.info('[%s] collection="%s" pass %s: read %s of %s, re-reading to close gaps',
                     "INITIAL_SYNC" if reason == "initial" else "RESYNC", st.title, pass_no, len(seen), count)
        gap = count - len(seen)
        complete = reached_end and (stable or gap <= max(1, count // 500))
        # Большие коллекции Telegram иногда отдаёт не целиком (1101 из 10153): повторные проходы
        # ничего не добавляют. Такую коллекцию читаем дальше, но помечаем partial — для неё
        # «незнакомый лот наверху» ещё не значит «новый» (см. _apply: сверка с изменением count).
        partial = complete and gap > max(1, count // 100)
        if reason == "initial" and complete:
            # первый опрос сразу после initial sync — тихий: пока мы листали, рынок менялся,
            # и «незнакомый лот наверху» на первом live-опросе не отличить от старого. Один опрос
            # закрепляет базу, после него уходят только по-настоящему новые лоты.
            self._baseline_pending.add(gid)
        st.synced = True
        st.complete = complete
        st.partial = partial
        st.last_count = count
        st.needs_full_sync = False
        st.last_cursor = first_id or st.last_cursor
        st.last_poll = utcnow_iso()
        st.last_session = session
        await self.db.save_collection_state(st.to_row())
        self._dirty.discard(gid)
        tag = "INITIAL_SYNC" if reason == "initial" else "RESYNC"
        llog(tag, f'collection="{st.title}" registered={len(seen)} api_count={count} pages={pages} '
                  f'complete={complete} partial={partial} sent=0 reason="{reason}"')
        if not complete:
            log.warning('[%s] collection="%s" incomplete (read %s of %s) — NOTHING from it will be sent '
                        'until a full resync succeeds', tag, st.title, len(seen), count)
        return len(seen)

    async def poll_collection(self, st: CollectionState, mode: str) -> int:
        """mode: sync | recover | live. Возвращает число НОВЫХ листингов, допущенных в pipeline."""
        lock = self._locks.setdefault(st.gift_id, asyncio.Lock())
        async with lock:
            if mode == "sync" or not st.synced:
                await self.full_sync(st, "initial")
                return 0
            if st.needs_full_sync or not st.complete:
                await self.full_sync(st, "collection_incomplete")
                if not st.complete:
                    self._due[st.gift_id] = time.monotonic() + settings.resync_retry_seconds
                return 0
            cls, items, session, count = await self._scan_top(st, mode)
            return await self._apply(st, cls, items, session, mode, count)

    async def _scan_top(self, st: CollectionState, mode: str):
        """Читает верх рынка, пока не найдётся anchor. Сначала мелкими страницами (дёшево),
        при всплеске — заново крупными страницами до RECOVERY_MAX_PAGES."""
        gid = st.gift_id
        plans = [(settings.page_limit, settings.live_max_pages)] if mode == "live" else []
        plans.append((100, settings.recovery_max_pages))
        cls, items, session, count = None, [], None, None
        for limit, max_pages in plans:
            items, seen, offset, reached_end = [], set(), "", False
            for _ in range(max_pages):
                page, session = await self._fetch(gid, offset, limit)
                if count is None or not offset:
                    count = page.count
                for it in page.items:
                    if it.unique_id not in seen:
                        seen.add(it.unique_id)
                        items.append(it)
                reached_end = not page.next_offset or not page.items
                known = await self._lookup(items)
                cls = classify(st, items, known, reached_end)
                if cls.decided or reached_end:
                    break
                offset = page.next_offset or ""
            if cls is not None and cls.decided:
                break
        return cls, items, session, count

    async def _apply(self, st: CollectionState, cls, items: list[ResaleItem], session: str | None,
                     mode: str, count: int | None = None) -> int:
        gid = st.gift_id
        self.stats["polls"] += 1
        now_iso = utcnow_iso()

        if cls is None or not cls.decided:
            # anchor не найден даже после дочитывания → ничего не отправляем, тихо пересинхронизируем
            self.stats["resyncs"] += 1
            log.warning('[RESYNC] collection="%s" reason="anchor_not_found" scanned=%s — nothing sent',
                        st.title, len(items))
            st.complete = False
            st.needs_full_sync = True
            await self.db.upsert_market_items(self._row(it, MarketState.SILENT_OLD) for it in items
)
            self._due[gid] = 0.0   # полная синхронизация — при следующем опросе
            self._dirty.add(gid)
            return 0

        # 1) PRICE_MOVED — тот же листинг, новая цена: обновить, НЕ отправлять
        for it, k in cls.moved:
            self.stats["price_moves"] += 1
            llog("PRICE_MOVE", f"listing={it.listing_id} old={_price(k.stars, k.ton_nano)} "
                               f"new={_price(it.stars, it.ton_nano)}")
        # 2) SILENT_OLD — старые лоты, впервые попавшие в окно: запомнить, НЕ отправлять
        for it, reason in cls.silent:
            self.stats["silent"] += 1
            llog("SILENT_OLD", f'listing={it.listing_id} reason="{reason}"')
        if cls.reason in ("above_anchor_but_incomplete", "end_of_list_incomplete"):
            st.needs_full_sync = True

        # 2b) Коллекция, которую Telegram отдаёт не целиком: незнакомый лот наверху может быть
        #     старым лотом, которого мы не видели, со сменённой ценой. Новый листинг увеличивает
        #     count, смена цены — нет. Пропускаем как NEW не больше, чем вырос count; иначе — тихо.
        if cls.new and gid in self._baseline_pending:
            for it in cls.new:
                self.stats["silent"] += 1
                llog("SILENT_OLD", f'listing={it.listing_id} reason="post_initial_baseline"')
            cls.silent += [(it, "post_initial_baseline") for it in cls.new]
            cls.new = []
        self._baseline_pending.discard(gid)

        if cls.new and st.partial:
            delta = (count - st.last_count) if (count is not None and st.last_count is not None) else 0
            if len(cls.new) > max(delta, 0):
                for it in cls.new:
                    self.stats["silent"] += 1
                    llog("SILENT_OLD", f'listing={it.listing_id} reason="partial_collection_uncertain" '
                                       f'new={len(cls.new)} count_delta={delta}')
                cls.silent += [(it, "partial_collection_uncertain") for it in cls.new]
                cls.new = []
        if count is not None:
            st.last_count = count

        # 3) NEW — глобальная дедупликация (INSERT в listings) ДО записи в market_items:
        #    при падении процесса листинг останется в listings со статусом NEW и будет дообработан.
        admitted = 0
        if cls.new:
            if mode == "recover":
                llog("RECOVERY", f'collection="{st.title}" new_during_downtime={len(cls.new)}')
            admitted = await self.pipeline.admit(cls.new, session, mode, floor_stars=st.floor_stars)
            st.last_seen_listing_id = cls.new[0].listing_id
            st.last_seen_timestamp = now_iso

        rows = [self._row(it, MarketState.NEW) for it in cls.new]
        rows += [self._row(it, MarketState.PRICE_MOVED) for it, _ in cls.moved]
        rows += [self._row(it, MarketState.SILENT_OLD) for it, _ in cls.silent]
        await self.db.upsert_market_items(rows)

        if items:
            st.last_cursor = items[0].listing_id
        st.last_poll = now_iso
        st.last_session = session
        self._dirty.add(gid)
        return admitted

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

    async def _run_batch(self, states: list[CollectionState], mode: str, concurrency: int | None = None) -> int:
        sem = asyncio.Semaphore(concurrency or max(1, min(settings.max_concurrent_sessions,
                                                          len(self.sessions.usable()) or 1)))

        async def one(s: CollectionState) -> int:
            async with sem:
                return await self._poll_safe(s, mode)

        results = await asyncio.gather(*(one(s) for s in states))
        return sum(results)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.mode = TrackerMode.INITIALIZING
        await self._load_state()
        self.paused = (await self.db.kv_get("paused", "0")) == "1"
        if self.paused:
            log.info("Tracker is PAUSED ⏸ (use /resume)")
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

        ready = [s for s in self.states.values() if self._is_live(s)]
        pending = self._pending_sync()
        if ready:
            self.mode = TrackerMode.RECOVERY
            log.info("[RECOVERY] checking %s collection(s) for listings that appeared while offline...",
                     len(ready))
            missed = await self._run_batch(ready, "recover")
            llog("RECOVERY", f"done: {missed} new listing(s) found during downtime")

        await self._persist()
        self.mode = TrackerMode.LIVE
        log.info("LIVE TRACKER ENABLED 🟢 (%s collections live, %s waiting for silent full sync)",
                 len(ready), len(pending))
        if pending:
            log.info("[INITIAL_SYNC] %s collection(s) will be synced in background, smallest first; "
                     "each goes LIVE right after its own sync. Nothing from them is sent before that.",
                     len(pending))
        await self.db.kv_set("tracker_mode", self.mode)

        now = time.monotonic()
        for gid in self.states:
            self._due.setdefault(gid, now)
        self._tasks = [
            asyncio.create_task(self._live_loop(), name="live-loop"),
            asyncio.create_task(self._catalog_loop(), name="catalog-loop"),
            asyncio.create_task(self._persist_loop(), name="persist-loop"),
            asyncio.create_task(self._sync_loop(), name="sync-loop"),
        ]
        for t in self._tasks:
            t.add_done_callback(_log_task_death)

    def _is_live(self, st: CollectionState) -> bool:
        return st.synced and st.complete and not st.needs_full_sync and st.gift_id not in self.skipped

    def _pending_sync(self) -> list[CollectionState]:
        now = time.monotonic()
        out = [st for st in self.states.values()
               if st.gift_id not in self.skipped and not self._is_live(st) and self._sync_retry_at.get(st.gift_id, 0.0) <= now
               and st.gift_id not in self._running]
        # сначала дорогие коллекции, внутри — маленькие первыми
        return sorted(out, key=lambda st: (st.gift_id in self.low_prio, st.availability_resale or 0))

    def sync_progress(self) -> tuple[int, int]:
        """(коллекций в LIVE, всего коллекций)"""
        tracked = [st for st in self.states.values() if st.gift_id not in self.skipped]
        return sum(1 for st in tracked if self._is_live(st)), len(tracked)

    async def sync_pending(self) -> None:
        """Тихо синхронизировать все коллекции, которые ещё не в LIVE (используется и тестами)."""
        pending = self._pending_sync()
        if not pending:
            return
        before = await self.db.count_market_items()
        await self._run_batch(pending, "sync", concurrency=self._sync_concurrency())
        after = await self.db.count_market_items()
        for st in pending:
            if not self._is_live(st):
                self._sync_retry_at[st.gift_id] = time.monotonic() + settings.resync_retry_seconds
        llog("INITIAL_SYNC", f"completed: {after - before} existing listings registered, 0 sent "
                             f"({self.sync_progress()[0]}/{len(self.states)} collections live)")

    def _sync_concurrency(self) -> int:
        # полная синхронизация занимает не больше половины sessions — остальные держат LIVE
        return max(1, min(settings.max_concurrent_sessions, len(self.sessions.usable()) // 2))

    async def _sync_loop(self) -> None:
        while not self._stop.is_set():
            if self.paused:
                await asyncio.sleep(1)
                continue
            try:
                pending = self._pending_sync()
                if pending:
                    sem = asyncio.Semaphore(self._sync_concurrency())

                    async def one(st: CollectionState) -> None:
                        async with sem:
                            if self._stop.is_set():
                                return
                            self._running.add(st.gift_id)
                            try:
                                await self._poll_safe(st, "sync")
                            finally:
                                self._running.discard(st.gift_id)
                                if not self._is_live(st):
                                    self._sync_retry_at[st.gift_id] = (time.monotonic()
                                                                       + settings.resync_retry_seconds)
                                self._due[st.gift_id] = time.monotonic()

                    await asyncio.gather(*(one(st) for st in pending))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Background sync failed")
            await asyncio.sleep(5)

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
            try:
                await self._live_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Live loop tick failed (continuing)")
                await asyncio.sleep(1)

    async def _live_tick(self) -> None:
        if self.paused:
            await asyncio.sleep(0.5)
            return
        now = time.monotonic()
        capacity = max(1, min(settings.max_concurrent_sessions, max(1, len(self.sessions.usable()))))
        due = sorted((t, gid) for gid, t in self._due.items()
                     if t <= now and gid not in self._running
                     and gid in self.states and self._is_live(self.states[gid]))
        live_running = sum(1 for g in self._running if g in self.states and self._is_live(self.states[g]))
        for _, gid in due:
            if live_running >= capacity:
                break
            live_running += 1
            st = self.states.get(gid)
            if st is None:
                self._due.pop(gid, None)
                continue
            self._running.add(gid)
            asyncio.create_task(self._live_poll(st), name=f"poll-{gid}")
        await asyncio.sleep(0.02)

    async def _live_poll(self, st: CollectionState) -> None:
        gid = st.gift_id
        found = 0
        hot_at = self._hot_since.pop(gid, None)
        if hot_at is not None:
            lat = time.monotonic() - hot_at
            self.hot_latency = lat if self.hot_latency is None else self.hot_latency * 0.8 + lat * 0.2
        try:
            found = await self._poll_safe(st, "live")
        finally:
            now = time.monotonic()
            prev = self._last_poll_mono.get(gid)
            if prev is not None:
                g = now - prev
                self._poll_gap[gid] = g if gid not in self._poll_gap else self._poll_gap[gid] * 0.8 + g * 0.2
            self._last_poll_mono[gid] = now
            # коллекция, где только что были новые лоты, остаётся «горячей»; остальные — страховочный
            # опрос раз в COLD_POLL_INTERVAL, а мгновенно — когда каталог покажет изменение счётчика
            cold = settings.low_priority_poll_interval if gid in self.low_prio else settings.cold_poll_interval
            self._due[gid] = now + (settings.tracker_interval if found else cold)
            self._running.discard(gid)

    async def _catalog_loop(self) -> None:
        # ровный ритм: время самого запроса входит в интервал (раньше интервал + запрос ≈ 2× дольше)
        while not self._stop.is_set():
            started = time.monotonic()
            if not self.paused:
                try:
                    await self.refresh_catalog()
                except Exception as e:
                    log.warning("Catalog refresh failed: %s", e)
            await asyncio.sleep(max(0.2, settings.catalog_interval - (time.monotonic() - started)))

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
