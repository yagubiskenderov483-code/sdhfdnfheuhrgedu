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
  • каталог (payments.getStarGifts) каждые CATALOG_INTERVAL, запросы внахлёст: выросло число лотов
    коллекции или упал её флор → коллекция «горячая» и опрашивается первой;
  • затем дорогие коллекции (флор от PRIORITY_FLOOR) — раз в PRIORITY_POLL_INTERVAL, но не больше
    PRIORITY_SHARE реальной мощности аккаунтов; остальные — страховочный обход раз в COLD_POLL_INTERVAL
    (долго ждущая дешёвая коллекция встаёт в ряд с дорогими);
  • параллельных опросов — по числу рабочих аккаунтов минус один (под проверку продавцов);
    темп каждого аккаунта ограничивает сам worker (SESSION_RPS × скорость после FloodWait).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import time
from datetime import datetime, timezone

from telethon import errors

from config import settings
from database.db import Database
from database.models import utcnow_iso
from sessions.manager import SessionManager
from tracker.marketplace import ResaleItem, fetch_catalog, fetch_resale_page
from tracker.queue import ListingPipeline
from tracker.state import CollectionState, KnownItem, MarketState, classify, is_strong
from tracker.worker import WorkerUnavailable
from utils.logger import get_logger, llog

log = get_logger("coordinator")
_URGENT: contextvars.ContextVar[bool] = contextvars.ContextVar("urgent_poll", default=False)


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
        self._polls: set[asyncio.Task] = set()     # живые опросы (сильные ссылки, иначе GC может их съесть)
        self._cat_applied = 0
        self._sync_busy = False        # идёт массовая быстрая синхронизация (после запуска)
        self._sync_retry_at: dict[int, float] = {}
        self._hot_since: dict[int, float] = {}
        self._poll_t0: dict[int, float] = {}
        self._poll_fails: dict[int, int] = {}
        self.paused = False
        self.skipped: set[int] = set()
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
                partial=bool(row.get("partial") or 0), last_count=row.get("last_count"),
                baseline_pending=bool(row.get("baseline") or 0))
            if st.last_cursor and st.last_cursor.startswith("["):
                try:
                    st.prev_top = json.loads(st.last_cursor)
                except ValueError:
                    st.prev_top = []
                st.last_cursor = st.prev_top[0] if st.prev_top else None
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
    async def refresh_catalog(self, seq: int | None = None) -> None:
        await self._wait_if_paused()
        catalog, _ = await self.sessions.run(fetch_catalog)
        if seq is not None:
            # запросы каталога идут внахлёст: более старый ответ, пришедший позже свежего, не применяем
            if seq <= self._cat_applied:
                return
            self._cat_applied = seq
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
            # заметное падение флора (>1%): мелкие колебания бывают от курса TON у лотов в TON
            floor_dropped = bool(st.floor_stars and c.resell_min_stars
                                 and c.resell_min_stars < st.floor_stars * 0.99)
            if floor_dropped:
                if not (st.prev_floor and time.monotonic() - st.prev_floor_at < 120):
                    st.prev_floor = st.floor_stars
                st.prev_floor_at = time.monotonic()
            st.floor_stars = c.resell_min_stars
            if c.availability_resale != st.availability_resale or floor_dropped:
                # флор упал при том же числе лотов = кто-то выставил дешевле (и одновременно что-то продано)
                grew = (c.availability_resale or 0) > (st.availability_resale or 0) or floor_dropped
                st.availability_resale = c.availability_resale
                self._dirty.add(c.gift_id)
                if not grew:
                    # счётчик уменьшился (продажа / снятие) — срочный опрос не нужен, но новый лот мог
                    # совпасть с продажами; проверяем в ближайшие секунды обычным (не срочным) опросом
                    soon = time.monotonic() + 8.0
                    self._due[c.gift_id] = min(self._due.get(c.gift_id, soon), soon)
                    continue
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
                # включаем ранее пропущенную → сперва тихий ре-синк; complete=False персистится,
                # чтобы рестарт до ре-синка не выбросил накопленное через RECOVERY (как в ветке ниже)
                st.needs_full_sync = True
                st.complete = False
                self._dirty.add(gid)
                self._due[gid] = 0.0
            self.skipped.discard(gid)
            self.low_prio.discard(gid)
            return False
        cheap_before = gid in self.skipped or gid in self.low_prio
        tracked = not cheap_before and not first
        limit = th * 0.9 if tracked else th
        # флора ещё нет (новая коллекция, лотов на рынке пока нет) — НЕ дешёвая: отслеживаем сразу,
        # иначе первые лоты новой коллекции ушли бы в «тихую» ре-синхронизацию и не попали в канал
        cheap = floor is not None and floor < limit
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
            # complete=False персистится в БД → если процесс рестартнёт до ре-синка, коллекция не
            # попадёт в RECOVERY (которая отправляет), а честно пере-синхронизируется молча.
            if st.synced:
                st.needs_full_sync = True
                st.complete = False
                self._dirty.add(gid)
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
        """/pause и /resume: пока на паузе, трекер не делает ни одного запроса к Telegram.
        На resume НЕ глушим накопленное: лоты, появившиеся за паузу, по-настоящему новые и нужны —
        дедуп по unique_id и так не даст повторить уже известные, а старьё отсекает anchor/partial."""
        self.paused = paused
        await self.db.kv_set("paused", int(paused))
        log.info("Tracker %s", "PAUSED ⏸ — no Telegram requests" if paused else "RESUMED ▶️")

    async def _fetch(self, gid: int, offset: str, limit: int):
        await self._wait_if_paused()
        # опрос коллекции, где каталог только что показал новый лот, не ждёт полную паузу аккаунта
        page, session = await self.sessions.run(lambda c: fetch_resale_page(c, gid, offset, limit),
                                                prefer=self.shard_owner(gid),
                                                priority=_URGENT.get() and not offset)   # только 1-я страница
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
        fast = settings.sync_mode == "fast"
        max_pages = settings.fast_sync_pages if fast else (settings.initial_sync_pages or 10**9)
        for pass_no in range(1, (1 if fast else settings.sync_passes) + 1):
            offset, reached_end, pass_pages = "", False, 0
            seen_before = len(seen)
            while pass_pages < max_pages:
                page, session = await self._fetch(gid, offset, 100)
                pass_pages += 1
                count = page.count
                if pass_no == 1 and pass_pages == 1 and page.items:
                    first_id = page.items[0].listing_id
                    st.prev_top = [it.listing_id for it in page.items[:settings.page_limit]]
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
        if fast:
            # Быстрый режим: база — верхняя страница рынка (самые свежие лоты по времени выставления/смены цены).
            # Полное чтение больших коллекций с малым числом аккаунтов идёт часами, а Telegram часто
            # вообще не отдаёт их целиком — из-за этого половина коллекций НИЧЕГО не слала.
            # Теперь незнакомый лот над якорем = новый (или старый лот, только что перевыставленный
            # с новой ценой — он тоже свежий на рынке).
            complete, gap = True, 0
        # Большие коллекции Telegram иногда отдаёт не целиком (1101 из 10153): повторные проходы
        # ничего не добавляют. Такую коллекцию читаем дальше, но помечаем partial — для неё
        # «незнакомый лот наверху» ещё не значит «новый» (см. _apply: сверка с изменением count).
        partial = complete and gap > max(1, count // 100)
        if complete and not fast:
            # (быстрый режим читает 1-2 страницы за секунды — тихий опрос не нужен, иначе лоты,
            # выставленные до первого live-опроса, терялись бы)
            # первый опрос сразу после ЛЮБОЙ полной синхронизации — тихий: пока мы листали, рынок
            # менялся, и «незнакомый лот наверху» на первом live-опросе не отличить от старого. Один
            # опрос закрепляет базу, после него уходят только по-настоящему новые лоты. Это важно и
            # для ре-синка (не только initial): full_sync мог пропустить лот на границе страниц.
            st.baseline_pending = True
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
                return 0
            cls, items, session, count = await self._scan_top(st, mode)
            return await self._apply(st, cls, items, session, mode, count)

    async def _scan_top(self, st: CollectionState, mode: str):
        """Читает верх рынка, пока не найдётся anchor. Сначала мелкими страницами (дёшево),
        при всплеске — заново крупными страницами до RECOVERY_MAX_PAGES."""
        gid = st.gift_id
        plans = [(settings.page_limit, settings.live_max_pages)] if mode == "live" else []
        # в быстром режиме не листаем глубоко: якоря не нашлось за 3 страницы → быстрая пересинхронизация
        # (иначе после деплоя RECOVERY по всем коллекциям занимала минуты, и новые лоты ждали)
        deep = min(settings.recovery_max_pages, 3) if settings.sync_mode == "fast" else settings.recovery_max_pages
        plans.append((100, deep))
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
                if reached_end:
                    break
                if cls.decided:
                    # якорь найден, но count вырос больше, чем видно незнакомых лотов → настоящие новые
                    # ещё ниже (ложный якорь сверху: перевыставление по той же цене) — дочитываем
                    seen_unknown = len(cls.new) + sum(1 for _, w in cls.silent if w == "first_seen_below_anchor")
                    more = (st.complete and count is not None and st.last_count is not None
                            and count - st.last_count > seen_unknown)
                    if (settings.sync_mode == "fast" and st.prev_top and cls.silent
                            and not any(it.listing_id in set(st.prev_top) for it in items)):
                        more = True     # ещё не дошли до прошлого верха — граница «изменилось после опроса» ниже
                    if not more:
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

        if (cls is not None and not cls.decided and settings.sync_mode == "fast" and st.complete
                and count is not None and st.last_count is not None):
            # Быстрый режим: якоря нет даже на 3 страницах (всплеск лотов или массовая смена цен).
            # Раньше это значило «ничего не шлём + пересинхронизация» — и весь всплеск терялся.
            # Сколько лотов реально добавилось, видно по счётчику коллекции: столько верхних
            # незнакомых и считаем новыми, остальное запоминаем молча. Коллекция остаётся в работе.
            delta = max(0, count - st.last_count)
            unknown = [it for it, why in cls.silent if why == "no_anchor"]
            take = unknown[:delta]
            ids = {id(it) for it in take}
            cls.silent = [(it, why) for it, why in cls.silent if id(it) not in ids]
            cls.new = take
            cls.decided = True
            cls.reason = "no_anchor_count_delta"
            log.info('[NO_ANCHOR] collection="%s" scanned=%s count_delta=%s → new=%s',
                     st.title, len(items), delta, len(take))

        if cls is None or not cls.decided:
            # anchor не найден даже после дочитывания → ничего не отправляем, тихо пересинхронизируем
            self.stats["resyncs"] += 1
            log.warning('[RESYNC] collection="%s" reason="anchor_not_found" scanned=%s — nothing sent',
                        st.title, len(items))
            st.complete = False
            st.needs_full_sync = True
            await self.db.upsert_market_items(self._row(it, MarketState.SILENT_OLD) for it in items
)
            self._dirty.add(gid)          # пересинхронизацию сделает фоновая синхронизация (_sync_loop)
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
        was_baseline = st.baseline_pending
        if st.baseline_pending:
            for it in cls.new:
                self.stats["silent"] += 1
                llog("SILENT_OLD", f'listing={it.listing_id} reason="post_sync_baseline"')
            cls.silent += [(it, "post_sync_baseline") for it in cls.new]
            cls.new = []
            st.baseline_pending = False
            self._dirty.add(gid)

        # 2c) «ложный якорь»: сверху встал старый лот (перевыставили / чуть сменили цену), а настоящие
        #     новые оказались ПОД ним. Счётчик коллекции вырос на delta → столько новых точно есть.
        #     Недостающих до delta берём из невиданных лотов ниже якоря (самые верхние), остальные — тихо.
        if (st.complete and not st.partial and not was_baseline and count is not None
                and st.last_count is not None and cls.silent):
            missing = (count - st.last_count) - len(cls.new)
            if missing > 0:
                below = [it for it, why in cls.silent if why == "first_seen_below_anchor"][:missing]
                if below:
                    ids = {id(it) for it in below}
                    cls.silent = [(it, why) for it, why in cls.silent if id(it) not in ids]
                    cls.new = cls.new + below
                    self.stats["silent"] -= len(below)
                    for it in below:
                        llog("NEW_BELOW_ANCHOR", f"listing={it.listing_id} count_delta={count - st.last_count}")

        # 2e) владелец скрыт: NFT перепродали другому скрытому продавцу — по ключу это «тот же листинг
        #     с новой ценой». Если счётчик коллекции вырос больше, чем найдено новых, недостающие
        #     берём из таких «смен цены» со скрытым продавцом.
        if (st.complete and not was_baseline and count is not None and st.last_count is not None
                and cls.moved):
            missing = (count - st.last_count) - len(cls.new)
            if missing > 0:
                # (ключи должны различаться: при одинаковом «n» это тот же listing_id — его не перешлёшь)
                weak = [(it, k) for it, k in cls.moved
                        if it.seller_key != k.seller_key
                        and not (is_strong(it.seller_key) and is_strong(k.seller_key))][:missing]
                if weak:
                    ids = {id(it) for it, _ in weak}
                    cls.moved = [(it, k) for it, k in cls.moved if id(it) not in ids]
                    cls.new = cls.new + [it for it, _ in weak]
                    self.stats["price_moves"] -= len(weak)
                    for it, _ in weak:
                        llog("NEW_HIDDEN_RESALE", f"listing={it.listing_id}")

        # 2d) «ложный якорь» без роста счётчика (перевыставили по той же цене + продажа рядом):
        #     всё, что в выдаче ВЫШЕ прошлого верхнего лота, изменилось после прошлого опроса —
        #     незнакомый лот там новый, даже если оказался под якорем.
        if st.complete and st.last_cursor and not was_baseline and cls.silent:
            pos = {it.listing_id: i for i, it in enumerate(items)}
            # верхний лот прошлого опроса мог быть продан/перепродан → берём первый сохранившийся
            marks = [l for l in (st.prev_top or [st.last_cursor]) if l in pos]
            if settings.sync_mode == "fast":
                # всё, что выше САМОГО НИЖНЕГО сохранившегося лота прошлого верха, менялось после
                # прошлого опроса (сохранившийся лот мог только подняться, не опуститься)
                top_idx = max((pos[l] for l in marks), default=None)
            else:
                top_idx = pos[marks[0]] if marks else None
            if top_idx:
                fresh = {id(it) for it in items[:top_idx]}
                rescue = [it for it, why in cls.silent if why == "first_seen_below_anchor" and id(it) in fresh]
                if rescue:
                    ids = {id(it) for it in rescue}
                    cls.silent = [(it, why) for it, why in cls.silent if id(it) not in ids]
                    order = {id(it): i for i, it in enumerate(items)}
                    cls.new = sorted(cls.new + rescue, key=lambda it: order.get(id(it), 0))
                    self.stats["silent"] -= len(rescue)
                    for it in rescue:
                        llog("NEW_ABOVE_LAST_TOP", f"listing={it.listing_id}")

        if cls.new and st.partial and settings.sync_mode == "full":
            delta = (count - st.last_count) if (count is not None and st.last_count is not None) else 0
            keep = max(delta, 0)
            if len(cls.new) > keep:
                # count вырос на delta → столько лотов точно новые. Оставляем верхние keep (они
                # свежее всех по времени изменения цены), остальные — тихо (могут быть старые,
                # всплывшие из непрочитанной части). Раньше глушили ВЕСЬ пакет и теряли настоящие новые.
                extra = cls.new[keep:]
                for it in extra:
                    self.stats["silent"] += 1
                    llog("SILENT_OLD", f'listing={it.listing_id} reason="partial_collection_uncertain" '
                                       f'new={len(cls.new)} kept={keep} count_delta={delta}')
                cls.silent += [(it, "partial_collection_uncertain") for it in extra]
                cls.new = cls.new[:keep]
        if count is not None:
            st.last_count = count

        # 3) NEW — глобальная дедупликация (INSERT в listings) ДО записи в market_items:
        #    при падении процесса листинг останется в listings со статусом NEW и будет дообработан.
        admitted = 0
        if cls.new:
            if mode == "recover":
                llog("RECOVERY", f'collection="{st.title}" new_during_downtime={len(cls.new)}')
            admitted = await self.pipeline.admit(cls.new, session, mode,
                                                 floor_stars=st.ref_floor(time.monotonic()),
                                                 t_detect=self._poll_t0.get(gid) if mode == "live" else None)
            st.last_seen_listing_id = cls.new[0].listing_id
            st.last_seen_timestamp = now_iso

        if cls.known:
            try:
                await self.db.touch_market_items(it.unique_id for it in cls.known)
            except Exception:
                pass
        rows = [self._row(it, MarketState.NEW) for it in cls.new]
        rows += [self._row(it, MarketState.PRICE_MOVED) for it, _ in cls.moved]
        rows += [self._row(it, MarketState.SILENT_OLD) for it, _ in cls.silent]
        await self.db.upsert_market_items(rows)

        if items:
            st.last_cursor = items[0].listing_id
            st.prev_top = [it.listing_id for it in items[:settings.page_limit]]
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
            return 0      # ошибка самого запроса — повторять часто бессмысленно
        except (WorkerUnavailable, TimeoutError) as e:
            self.stats["poll_errors"] += 1
            log.warning("[%s] no session available: %s", st.title, e)
            return None   # временно нет свободного аккаунта — повторить скоро
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
        return sum(r or 0 for r in results)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.mode = TrackerMode.INITIALIZING
        await self._load_state()
        self.paused = (await self.db.kv_get("paused", "0")) == "1"
        if self.paused:
            log.info("Tracker is PAUSED ⏸ (use /resume)")
        await self._wait_for_sessions()
        log.info("Marketplace tracker started")

        while True:
            try:
                await self.refresh_catalog()
                break
            except Exception as e:
                log.error("Catalog fetch failed: %s — retry in 10s", e)
                await asyncio.sleep(10)
        # недообработанные лоты прошлого запуска — после каталога, чтобы был известен флор коллекций
        await self.pipeline.resume_unfinished()

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
        n = len(self.sessions.usable())
        if settings.sync_mode == "fast":
            # быстрая база — 2 запроса на коллекцию; пока её нет, лоты коллекции не ловятся вообще,
            # поэтому после запуска синхронизация идёт всеми аккаунтами
            return max(1, min(settings.max_concurrent_sessions, n))
        return max(1, min(settings.max_concurrent_sessions, n // 2))

    async def _sync_loop(self) -> None:
        while not self._stop.is_set():
            if self.paused:
                await asyncio.sleep(1)
                continue
            try:
                pending = self._pending_sync()
                if pending:
                    self._sync_busy = settings.sync_mode == "fast" and len(pending) >= 3
                    sem = asyncio.Semaphore(self._sync_concurrency())

                    async def one(st: CollectionState) -> None:
                        async with sem:
                            if self._stop.is_set():
                                return
                            self._running.add(st.gift_id)
                            r = None
                            try:
                                r = await self._poll_safe(st, "sync")
                            finally:
                                self._running.discard(st.gift_id)
                                if not self._is_live(st):
                                    # нет свободного аккаунта — повторить через 15 с, а не через 5 минут
                                    self._sync_retry_at[st.gift_id] = time.monotonic() + (
                                        15.0 if r is None else settings.resync_retry_seconds)
                                self._due[st.gift_id] = time.monotonic()

                    try:
                        await asyncio.gather(*(one(st) for st in pending))
                    finally:
                        self._sync_busy = False
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Background sync failed")
            await asyncio.sleep(5)

    async def stop(self) -> None:
        self._stop.set()
        for t in list(self._tasks) + list(self._polls):
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
        # Темп каждого аккаунта уже ограничен в worker (SESSION_RPS × его скорость), поэтому
        # параллельных опросов — по числу аккаунтов (один оставляем под проверку продавцов).
        # Больше параллелизма не ускоряет отдельный аккаунт и не добавляет FloodWait.
        n_usable = len(self.sessions.usable())
        capacity = max(1, min(settings.max_concurrent_sessions, n_usable - (1 if n_usable > 2 else 0)))
        # 1) коллекции, где каталог показал новый лот (hot) — всегда первыми;
        # 2) затем дорогие (флор от PRIORITY_FLOOR); 3) остальные по времени
        # внутри каждого уровня дорогие коллекции идут раньше дешёвых
        # Обычная коллекция, которая ждёт дольше 3× COLD_POLL_INTERVAL, встаёт в один ряд с дорогими —
        # иначе при нехватке аккаунтов дешёвые не опрашивались бы никогда.
        starve = settings.cold_poll_interval * 3

        def key(gid: int, t: float) -> tuple:
            prio = self._is_priority(gid)
            if gid in self._hot_since:
                tier = 0 if prio else 1
            elif prio or now - t > starve:
                tier = 2
            else:
                tier = 3
            return (tier, t, gid)

        due = sorted(key(gid, t) for gid, t in self._due.items()
                     if t <= now and gid not in self._running
                     and gid in self.states and self._is_live(self.states[gid]))
        if self._sync_busy:
            # идёт быстрая синхронизация новых коллекций: пока у них нет базы, их лоты не ловятся вовсе,
            # поэтому плановые опросы ждут — опрашиваем только «горячие» (каталог показал новый лот)
            due = [d for d in due if d[0] <= 1]
        live_running = sum(1 for g in self._running if g in self.states and self._is_live(self.states[g]))
        for _, _, gid in due:
            if live_running >= capacity:
                break
            live_running += 1
            st = self.states.get(gid)
            if st is None:
                self._due.pop(gid, None)
                continue
            self._running.add(gid)
            task = asyncio.create_task(self._live_poll(st), name=f"poll-{gid}")
            self._polls.add(task)
            task.add_done_callback(self._polls.discard)
        await asyncio.sleep(0.02)

    def _priority_interval(self) -> float:
        """Частый опрос дорогих коллекций, но не больше PRIORITY_SHARE мощности аккаунтов —
        иначе остальные коллекции и «горячие» лоты встают в очередь, а аккаунты ловят FloodWait."""
        n = sum(1 for g in self.states if self._is_priority(g))
        eff = max(1.0, self.sessions.effective())
        budget = eff * max(0.05, settings.session_rps) * settings.priority_share
        return max(settings.priority_poll_interval, n / budget if budget else 60.0)

    def _is_priority(self, gid: int) -> bool:
        st = self.states.get(gid)
        return bool(settings.priority_floor and st is not None and st.floor_stars
                    and st.floor_stars >= settings.priority_floor)

    async def _live_poll(self, st: CollectionState) -> None:
        gid = st.gift_id
        found = 0
        hot_at = self._hot_since.pop(gid, None)
        started = time.monotonic()
        # момент обнаружения: изменение счётчика в каталоге (если было) или начало опроса
        self._poll_t0[gid] = hot_at if (hot_at is not None and started - hot_at < 120) else started
        if hot_at is not None:
            lat = started - hot_at
            if lat < 120:          # старые отметки (после паузы/синхронизации) не портят среднее
                self.hot_latency = lat if self.hot_latency is None else self.hot_latency * 0.8 + lat * 0.2
        token = _URGENT.set(hot_at is not None)
        try:
            found = await self._poll_safe(st, "live")
        finally:
            _URGENT.reset(token)
            now = time.monotonic()
            prev = self._last_poll_mono.get(gid)
            if found is not None:
                if prev is not None:
                    g = now - prev
                    self._poll_gap[gid] = g if gid not in self._poll_gap else self._poll_gap[gid] * 0.8 + g * 0.2
                self._last_poll_mono[gid] = now
            # коллекция, где только что были новые лоты, остаётся «горячей»; остальные — страховочный
            # опрос раз в COLD_POLL_INTERVAL, а мгновенно — когда каталог покажет изменение счётчика
            cold = settings.low_priority_poll_interval if gid in self.low_prio else settings.cold_poll_interval
            if self._is_priority(gid):
                # дорогие коллекции опрашиваются чаще — но не реже обычных
                cold = min(cold, self._priority_interval())
            if found is None:
                # нет свободной session — повторить скоро (с нарастающей паузой) и не терять «горячесть»
                fails = self._poll_fails.get(gid, 0) + 1
                self._poll_fails[gid] = fails
                if hot_at is not None:
                    self._hot_since.setdefault(gid, hot_at)
                nxt = now + min(cold, 2.0 ** min(fails - 1, 6))
            elif gid in self._hot_since:
                self._poll_fails.pop(gid, None)
                nxt = now      # пока шёл опрос, каталог показал ещё изменение — опросить сразу
            else:
                self._poll_fails.pop(gid, None)
                nxt = now + (settings.tracker_interval if found else cold)
            self._due[gid] = nxt
            self._running.discard(gid)

    async def _catalog_loop(self) -> None:
        """Каталог (счётчики лотов всех коллекций) — главный способ мгновенно заметить новый лот.
        Ответ идёт ~1-5 с (аккаунт может ждать свою очередь), поэтому запросы идут внахлёст:
        новый стартует каждые CATALOG_INTERVAL, одновременно до 2-3 штук."""
        seq = 0
        inflight: set[asyncio.Task] = set()

        async def once(n: int) -> None:
            try:
                await self.refresh_catalog(n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Catalog refresh failed: %s", e)

        try:
            while not self._stop.is_set():
                limit = 1 if len(self.sessions.usable()) < 4 else (2 if len(self.sessions.usable()) < 10 else 3)
                if not self.paused and len(inflight) < limit:
                    seq += 1
                    t = asyncio.create_task(once(seq), name=f"catalog-{seq}")
                    inflight.add(t)
                    t.add_done_callback(inflight.discard)
                await asyncio.sleep(max(0.2, settings.catalog_interval))
        finally:
            for t in list(inflight):
                t.cancel()

    async def _persist_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5)
            try:
                await self._persist()
            except Exception:
                log.exception("Persist failed")

    async def _persist(self) -> None:
        for gid in list(self._dirty):
            st = self.states.get(gid)
            self._dirty.discard(gid)      # до записи: изменение во время записи снова пометит коллекцию
            if st:
                try:
                    await self.db.save_collection_state(st.to_row())
                except Exception:
                    self._dirty.add(gid)
                    raise
        latest = max((s for s in self.states.values() if s.last_seen_timestamp),
                     key=lambda s: s.last_seen_timestamp, default=None)
        kv = {"stat_scanned": self.stats["scanned"], "stat_polls": self.stats["polls"],
              "tracker_mode": self.mode,
              "last_persist": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if latest:
            kv["last_seen_listing_id"] = latest.last_seen_listing_id
            kv["last_seen_timestamp"] = latest.last_seen_timestamp
        await self.db.kv_set_many(kv)
