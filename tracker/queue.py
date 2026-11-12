"""
Конвейер обработки новых листингов.

  Coordinator ──admit()──►  Global Deduplication (INSERT OR IGNORE по listing_id, статус NEW)
                               │ только реально новые
                               ▼
                         NewListingsQueue (asyncio.Queue)
                               │  PROCESSOR_WORKERS параллельных обработчиков
                               ▼
               PriceFilter → (getFullUser: Level/Premium/сообщения) → SellerFilter
                               ▼
                   Database (финальный статус) → TelegramSender → sent=1

Каждый листинг получает конечный статус: FILTERED_PRICE / FILTERED_SELLER / SENT /
TEST_ONLY / SEND_FAILED / ERROR. Если процесс упал посреди обработки, при следующем запуске
resume_unfinished() дообработает все листинги со статусом NEW.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


from datetime import datetime, timedelta, timezone

from bot.formatter import MessageFormatter, listing_keyboard
from bot.sender import TelegramSender
from config import settings
from database.db import Database
from database.models import Listing, ListingStatus
from filters.price_filter import PriceFilter
from filters.seller_filter import SellerFilter, SellerResult
from sessions.manager import SessionManager
from tracker.marketplace import (ResaleItem, SellerFull, fetch_extra_texts, fetch_full_user, fetch_gift_owner,
                                 fetch_full_user_failover, fetch_gift_texts,
                                 fetch_nft_count)
from utils.links import gift_link, gift_slug, username_link
from utils.logger import get_logger, llog

log = get_logger("pipeline")


def _username_of(entity: Any) -> str | None:
    if entity is None:
        return None
    u = getattr(entity, "username", None)
    if u:
        return u
    for un in getattr(entity, "usernames", None) or []:
        if getattr(un, "active", False):
            return un.username
    return None


def _messages_status(paid: int | None, require_premium: bool | None) -> str:
    if paid:
        return f"платно ({paid} ⭐)"
    if require_premium:
        return "только Premium"
    return "бесплатно"


def build_listing(it: ResaleItem, session: str | None) -> Listing:
    l = Listing(
        listing_id=it.listing_id,
        gift_unique_id=it.unique_id,
        gift_id=it.gift_id,
        gift_name=it.title,
        gift_num=it.num,
        gift_slug=gift_slug(it.slug, it.title, it.num),
        gift_link=gift_link(gift_slug(it.slug, it.title, it.num)),
        model=it.model,
        backdrop=it.backdrop,
        symbol=it.symbol,
        model_rarity=getattr(it, "model_rarity", None),
        backdrop_rarity=getattr(it, "backdrop_rarity", None),
        symbol_rarity=getattr(it, "symbol_rarity", None),
        seller_key=it.seller_key,
        seller_id=it.owner_id,
        seller_type=it.owner_type,
        stars_price=it.stars,
        ton_nano=it.ton_nano,
        ton_only=it.ton_only,
        session_source=session,
    )
    if it.owner_type == "user":
        u = it.user
        l.seller_username = _username_of(u)
        if u is not None:
            name = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x)
            l.seller_name = name or None
            l.premium = getattr(u, "premium", None)
            l.seller_phone = getattr(u, "phone", None)
            if not getattr(u, "min", False):
                l.messages = _messages_status(getattr(u, "send_paid_messages_stars", None),
                                              getattr(u, "contact_require_premium", None))
    elif it.owner_type in ("channel", "chat"):
        c = it.chat
        l.seller_username = _username_of(c)
        l.seller_name = getattr(c, "title", None) if c is not None else None
    elif it.owner_type == "address":
        l.seller_name = it.owner_address
    else:
        l.seller_name = it.owner_name
    l.seller_profile_link = username_link(l.seller_username)
    return l


def listing_from_row(r: dict[str, Any]) -> Listing:
    return Listing(
        listing_id=r["listing_id"], gift_unique_id=r["gift_unique_id"], gift_id=r["gift_id"],
        gift_name=r["gift_name"] or "", gift_num=r["gift_num"], gift_slug=r["gift_slug"],
        gift_link=r["gift_link"], model=r["model"], backdrop=r["backdrop"], symbol=r["symbol"],
        seller_key=r["seller_key"] or "n", seller_id=r["seller_id"], seller_type=r["seller_type"],
        seller_username=r["seller_username"], seller_name=r["seller_name"],
        seller_profile_link=r["seller_profile_link"], stars_price=r["stars_price"], ton_nano=r["ton_nano"],
        ton_only=bool(r["ton_only"]), level=r["level"],
        premium=None if r["premium"] is None else bool(r["premium"]), messages=r["messages"],
        seller_filter_result=r["seller_filter_result"], status=r["status"], reason=r["reason"],
        first_seen=r["first_seen"], listing_created_at=r["listing_created_at"], sent=bool(r["sent"]),
        message_id=r["message_id"], session_source=r["session_source"],
        target_chat_id=r["target_chat_id"] if "target_chat_id" in r.keys() else None,
        target_thread_id=r["target_thread_id"] if "target_thread_id" in r.keys() else None,
    )


@dataclass

class ListingEvent:
    listing: Listing
    detected_mono: float
    mode: str


_IN_FLIGHT = ("this seller is being sent right now", "same collection is being sent right now")


class _Stale(Exception):
    """Лот устарел, пока проверялся продавец — не отправляем."""


_LANG_CODES = {"UNKNOWN_BLOCKED", "FOREIGN_TEXT", "PHONE_MISMATCH"}
_HIDDEN_HARD_REJECT = {"BLACKLIST", "ARABIC_SCRIPT", "SHORT_NAME_HIDDEN", "FOREIGN_LETTER_HIDDEN", "FOREIGN_NAME_HIDDEN"}


class ListingPipeline:
    def __init__(self, db: Database, sessions: SessionManager, sender: TelegramSender,
                 formatter: MessageFormatter, price_filter: PriceFilter, seller_filter: SellerFilter):
        self.db = db
        self.sessions = sessions
        self.sender = sender
        self.formatter = formatter
        self.price_filter = price_filter
        self.seller_filter = seller_filter
        # приоритетная очередь: свежие LIVE-лоты первыми, дообработка после перезапуска — после них
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._qseq = 0
        self._workers: list[asyncio.Task] = []
        self._seller_cache: dict[int, tuple[float, SellerFull]] = {}
        self._nft_cache: dict[int, tuple[float, int | None]] = {}
        self._extra_text_cache: dict[int, tuple[float, str | None]] = {}
        self._gift_text_cache: dict[int, tuple[float, str | None]] = {}
        self.stats = {"new": 0, "sent": 0, "filtered_price": 0, "filtered_seller": 0,
                      "filtered_repeat": 0, "duplicates": 0, "failed": 0}
        # защита от повторов NFT / продавца между параллельными обработчиками
        self._repeat_lock = asyncio.Lock()
        self._released = asyncio.Condition(self._repeat_lock)   # «отправка закончилась» для ждущих
        self._seller_locks: dict[int, asyncio.Lock] = {}
        self._retries: set[asyncio.Task] = set()      # отложенные повторные проверки продавца
        self._reserved_gifts: set[int] = set()
        self._reserved_sellers: set[str] = set()
        # «подряд из одной коллекции»
        self._last_sent_gift_id: int | None = None
        self._streak = 0
        self._sending_gift_ids: set[int] = set()
        self.last_new_at: float | None = None      # time.time()
        self.latency: dict[str, float] = {}
        self.totals: deque[tuple[float, bool]] = deque(maxlen=300)   # (секунд до поста, дорогой ли) — для медианы
        self.paused_getter = None                  # задаёт App: lambda: coordinator.paused
        self.floor_getter = None                   # задаёт App: gift_id → флор коллекции
        self.muted_getter = None                   # задаёт App: названия выключенных коллекций (lower)
        self._seen_ids: dict[str, None] = {}       # недавние listing_id (RAM-кэш дедупа, по порядку)
        self.last_sent_at: float | None = None
        self.owner_lookup = None                   # задаёт App: OwnerLookup (автопоиск скрытого владельца)
        self._lookup_tasks: set[asyncio.Task] = set()   # лоты, ждущие ответа бота поиска

    # ------------------------------------------------------------------ admission (global dedup)
    async def admit(self, items: list[ResaleItem], session: str | None, mode: str,
                    floor_stars: int | None = None, t_detect: float | None = None) -> int:
        """items — сверху вниз (новейшие первыми). В очередь кладём от старых к новым.
        Защитимся от падения одного лота: если build_listing или insert_listing_if_new упадёт
        для одного item'а, остальные ВСЁ РАВНО обработаются."""
        admitted = 0
        t_found = time.monotonic()
        for it in reversed(items):
            try:
                listing = build_listing(it, session)
                listing.floor_stars = floor_stars
                listing.t_detect, listing.t_found = t_detect, t_found
                # быстрый отсев дублей в памяти — без обращения к БД
                if listing.listing_id in self._seen_ids:
                    self.stats["duplicates"] += 1
                    continue
                # Глобальная дедупликация: право на обработку получает только тот, чей INSERT прошёл.
                if not await self.db.insert_listing_if_new(listing):
                    self._remember(listing.listing_id)
                    self.stats["duplicates"] += 1
                    llog("DUPLICATE", f"listing={listing.listing_id} already_known=true")
                    continue
                self._remember(listing.listing_id)
                admitted += 1
                self.stats["new"] += 1
                self.last_new_at = time.time()
                price = " / ".join(x for x in (
                    f"{listing.stars_price}⭐" if listing.stars_price is not None else "",
                    f"{listing.ton_price:g}TON" if listing.ton_price is not None else "") if x) or "none"
                # detected = время ПЕРВОГО ОБНАРУЖЕНИЯ трекером, не дата выставления (API её не отдаёт)
                llog("NEW", f'listing={listing.listing_id} gift="{listing.gift_name} #{listing.gift_num}" '
                            f"price={price} seller={listing.seller_key} detected={listing.first_seen} "
                            f"via={session} mode={mode}")
                await self._enqueue(ListingEvent(listing, time.monotonic(), mode))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # один лот сломан (кривое поле, уникс-коллизия и т.п.) — не отменяем остальные
                log.warning("admit: skipped bad item %s: %s", getattr(it, "listing_id", "?"), e)
                continue
        return admitted

    @staticmethod
    def _is_expensive(l) -> bool:
        # дорогой = дорогая коллекция ИЛИ сам лот дорогой (50k⭐ в коллекции с флором 1.5k — тоже дорогой)
        value = max(getattr(l, "floor_stars", None) or 0, getattr(l, "stars_price", None) or 0)
        return bool(settings.priority_floor and value >= settings.priority_floor)

    async def _enqueue(self, ev: ListingEvent) -> None:
        self._qseq += 1
        expensive = self._is_expensive(ev.listing)
        # дорогие LIVE-лоты — самыми первыми, потом остальные LIVE, потом дообработка после перезапуска
        prio = (0 if expensive else 1) if ev.mode == "live" else 2
        # внутри приоритета — самый свежий первым
        await self.queue.put((prio * 1e12 - ev.detected_mono, self._qseq, ev))

    def _remember(self, listing_id: str) -> None:
        self._seen_ids[listing_id] = None
        if len(self._seen_ids) > 200_000:
            for _ in range(20_000):                      # выкидываем самые старые
                self._seen_ids.pop(next(iter(self._seen_ids)))

    async def resume_unfinished(self) -> int:
        last = await self.db.last_sent_listing()
        if last:
            self._last_sent_gift_id, self._streak = last["gift_id"], 1
        rows = await self.db.unfinished_listings()
        if settings.start_fresh and rows:
            # START_FRESH: недообработанные лоты прошлого запуска не дообрабатываем — они уже старые
            for r in rows:
                lr = listing_from_row(r)
                lr.status, lr.reason = ListingStatus.ERROR, "пропущен при перезапуске (START_FRESH)"
                await self.db.update_listing(lr)
            log.info("START_FRESH: %s unfinished listing(s) from previous run skipped", len(rows))
            return 0
        for r in rows:
            lr = listing_from_row(r)
            if self.floor_getter and lr.floor_stars is None:
                lr.floor_stars = self.floor_getter(lr.gift_id)
            await self._enqueue(ListingEvent(lr, time.monotonic(), "resume"))
        if rows:
            log.info("Resuming %s unfinished listing(s) from previous run", len(rows))
        return len(rows)

    # ------------------------------------------------------------------ workers
    def start(self) -> None:
        for i in range(settings.processor_workers):
            from tracker.worker import supervise
            self._workers.append(asyncio.create_task(
                supervise(f"processor-{i}", (lambda i=i: self._worker_loop(i)), delay=1.0), name=f"processor-{i}"))

    async def stop(self) -> None:
        pending = list(self._workers) + list(self._retries) + list(self._lookup_tasks)
        for t in pending:
            t.cancel()
        # Ждём ВСЕ отменённые задачи, не только воркеров. Иначе _lookup_then_process
        # / _later retry может быть в середине update_listing, когда дальше по
        # shutdown-pipeline вызовут db.close() — лот зависнет в SENDING навсегда,
        # а в логи упадёт "Task was destroyed but it is pending".
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=10)
            except asyncio.TimeoutError:
                log.warning("Pipeline stop: %d task(s) didn't finish in 10s",
                            sum(1 for t in pending if not t.done()))
        self._workers.clear()
        self._retries.clear()
        self._lookup_tasks.clear()

    async def _worker_loop(self, idx: int) -> None:
        while True:
            _, _, ev = await self.queue.get()
            try:
                # жёсткий потолок 10мин на один лот: lookup (345с) + enrich (25с) + send (ретраи)
                # помещаются с запасом. Если дольше — что-то залипло (бесконечное ожидание капчи,
                # висящая сессия) и воркер нельзя терять, иначе он не берёт следующие лоты.
                await asyncio.wait_for(self.process(ev), timeout=600)
            except asyncio.TimeoutError:
                log.warning("Processing %s timed out after 10min — abandoning (worker alive)",
                            ev.listing.listing_id)
                ev.listing.status, ev.listing.reason = ListingStatus.ERROR, "process timeout 10min"
                try:
                    await self.db.update_listing(ev.listing)
                except Exception:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Processing failed for %s", ev.listing.listing_id)
                ev.listing.status, ev.listing.reason = ListingStatus.ERROR, f"{type(e).__name__}: {e}"
                try:
                    await self.db.update_listing(ev.listing)
                except Exception:
                    pass
            finally:
                self.queue.task_done()

    # ------------------------------------------------------------------ steps
    async def _enrich_seller(self, l: Listing) -> None:
        if not settings.fetch_full_seller or l.seller_type != "user" or not l.seller_id:
            return
        # Отрицательный seller_id — это наш псевдо-ID (хеш username), настоящего
        # user_id у нас нет. fetch_full_user с ним гарантированно упадёт.
        if l.seller_id < 0:
            return
        # Владельца funstat выдал по доверию имени, резолва не было → access_hash нет
        # ни у одной сессии. Прямой fetch_full_user даёт 'Could not find input entity'.
        # НО: fetch_full_user_failover сначала читает страницу маркета этой коллекции —
        # Telegram отдаёт продавцов вместе с access_hash. Если у владельца на маркете
        # активные лоты в той же коллекции (а это и есть исходный лот), access_hash
        # подтянется и профиль прочитается. Это нужно чтобы для trusted-no-resolve
        # мы всё-таки видели l.messages и могли отсеять продавцов с платным ЛС
        # (Maker: 'чтоб соо не платные в лс были').
        trusted_no_resolve = getattr(l, "owner_trusted_no_resolve", False)
        if trusted_no_resolve:
            # Пробуем именно failover (с чтением resale страницы), прямой fetch_full_user
            # заведомо не работает без access_hash. Если и failover не смог — остаёмся
            # с l.messages=None (как раньше), лот пройдёт как OWNER_TRUSTED.
            try:
                full, other = await self.sessions.run(
                    lambda c: fetch_full_user_failover(c, l.seller_id, l.gift_id),
                    attempts=2, timeout=15)
                l.session_source = other
                l.level = full.level
                if full.premium is not None:
                    l.premium = full.premium
                if full.username and not l.seller_username:
                    l.seller_username = full.username
                    l.seller_profile_link = username_link(full.username)
                if full.phone and not l.seller_phone:
                    l.seller_phone = full.phone
                l.seller_about = full.about
                l.seller_channel = full.channel_title
                l.seller_business = getattr(full, "business_text", None)
                l.seller_channel_id = getattr(full, "channel_id", None)
                l.messages = _messages_status(full.paid_message_stars, full.contact_require_premium)
                # Снимаем флаг — теперь owner у нас реально проверен, не на доверии
                l.owner_trusted_no_resolve = False
                self._seller_cache[l.seller_id] = (time.monotonic(), full)
                self.stats["trusted_enriched"] = self.stats.get("trusted_enriched", 0) + 1
            except Exception as e:
                # failover не прошёл — остаёмся как раньше (OWNER_TRUSTED без messages-проверки)
                log.debug("trusted-no-resolve enrich failed for %s: %s", l.seller_id, e)
            return
        cached = self._seller_cache.get(l.seller_id)
        if cached and time.monotonic() - cached[0] < settings.seller_cache_ttl:
            full = cached[1]
        else:
            uid = l.seller_id
            try:
                # access_hash продавца есть у той session, которая получила лот → сначала на ней
                now_m = time.monotonic()
                nc = self._nft_cache.get(uid)
                tc = self._gift_text_cache.get(uid)
                want_nfts = settings.max_seller_nfts > 0 and not (nc and now_m - nc[0] < settings.seller_cache_ttl)
                # тексты подарков нужны, когда в профиле не окажется русского — для дорогих лотов берём их
                # сразу, в том же заходе (иначе это ещё одна пауза аккаунта после профиля)
                want_texts = (settings.russian_only and settings.unknown_seller_policy == "block"
                              and not (tc and now_m - tc[0] < settings.seller_cache_ttl))
                if self._is_expensive(l) and (want_nfts or want_texts):
                    # дорогой лот: профиль + число NFT + тексты подарков одним заходом
                    (full, nfts, texts), _ = await self.sessions.run(
                        lambda c: fetch_seller_bundle(c, uid, nfts=want_nfts, texts=want_texts),
                        only=l.session_source, priority=True, attempts=2, timeout=15)
                    now_m = time.monotonic()
                    if texts is not None:
                        txt, tn = texts
                        self._gift_text_cache[uid] = (now_m, txt)
                        if tn is not None and nfts is None:
                            nfts = tn
                    if nfts is not None:
                        self._nft_cache[uid] = (now_m, nfts)
                else:
                    full, _ = await self.sessions.run(lambda c: fetch_full_user(c, uid),
                                                      only=l.session_source, priority=True, attempts=2, timeout=15)
            except Exception as e:
                # failover: session умерла / FloodWait / занята — берём любую другую живую
                gid = l.gift_id
                log.info("Seller %s: session %s unavailable (%s) — switching to another session",
                         uid, l.session_source, type(e).__name__)
                # больше попыток для найденных через funstat лотов — не хотим потерять
                attempts = 5 if l.owner_found_via else 3
                try:
                    full, other = await self.sessions.run(lambda c: fetch_full_user_failover(c, uid, gid),
                                                          attempts=attempts, timeout=25)
                    l.session_source = other      # дальнейшие проверки продавца — на рабочей session
                    self.stats["failover"] = self.stats.get("failover", 0) + 1
                except Exception as e2:
                    log.debug("getFullUser %s failover failed: %s", uid, e2)
                    l.seller_lookup_failed = True
                    return
            self._seller_cache[uid] = (time.monotonic(), full)
            # LRU-trim: выкидываем 25% самых старых вместо .clear() — иначе каждые 50k
            # запросов рестартит ВСЕ кеши и каждый повторный продавец снова требует
            # запрос в Telegram. Для 50k dict сортировка ~20мс на запрос trim-а.
            if len(self._seller_cache) > 50_000:
                cutoff = sorted(self._seller_cache.items(), key=lambda kv: kv[1][0])[:12_500]
                for k, _ in cutoff:
                    self._seller_cache.pop(k, None)
        l.level = full.level
        if full.premium is not None:
            l.premium = full.premium
        if full.username and not l.seller_username:
            l.seller_username = full.username
            l.seller_profile_link = username_link(full.username)
        if full.phone and not l.seller_phone:
            l.seller_phone = full.phone
        l.seller_about = full.about
        l.seller_channel = full.channel_title
        l.seller_business = getattr(full, "business_text", None)
        l.seller_channel_id = getattr(full, "channel_id", None)
        l.messages = _messages_status(full.paid_message_stars, full.contact_require_premium)

    async def _nft_count(self, l: Listing) -> int | None:
        uid = l.seller_id
        cached = self._nft_cache.get(uid)
        if cached and time.monotonic() - cached[0] < settings.seller_cache_ttl:
            return cached[1]
        try:
            n, _ = await self.sessions.run(lambda c: fetch_nft_count(c, uid),
                                           only=l.session_source, priority=True, attempts=2, timeout=10)
        except Exception as e:
            log.debug("getSavedStarGifts %s failed: %s", uid, e)
            return None          # сбой не кэшируем — в следующий раз спросим снова
        self._nft_cache[uid] = (time.monotonic(), n)
        if len(self._nft_cache) > 50_000:
            cutoff = sorted(self._nft_cache.items(), key=lambda kv: kv[1][0])[:12_500]
            for k, _ in cutoff:
                self._nft_cache.pop(k, None)
        return n

    async def _gift_texts(self, l: Listing) -> str | None:
        uid = l.seller_id
        cached = self._gift_text_cache.get(uid)
        if cached and time.monotonic() - cached[0] < settings.seller_cache_ttl:
            return cached[1]
        try:
            (txt, nfts), _ = await self.sessions.run(lambda c: fetch_gift_texts(c, uid),
                                                     only=l.session_source, priority=True, attempts=1, timeout=10)
        except Exception as e:
            log.debug("getSavedStarGifts(texts) %s failed: %s", uid, e)
            return None
        now = time.monotonic()
        self._gift_text_cache[uid] = (now, txt)
        if nfts is not None:
            self._nft_cache[uid] = (now, nfts)     # все подарки уже в ответе — число NFT известно
        if len(self._gift_text_cache) > 50_000:
            cutoff = sorted(self._gift_text_cache.items(), key=lambda kv: kv[1][0])[:12_500]
            for k, _ in cutoff:
                self._gift_text_cache.pop(k, None)
        return txt

    async def _extra_texts(self, l: Listing) -> str | None:
        uid = l.seller_id
        cached = self._extra_text_cache.get(uid)
        if cached and time.monotonic() - cached[0] < settings.seller_cache_ttl:
            return cached[1]
        ch = l.seller_channel_id
        try:
            txt, _ = await self.sessions.run(lambda c: fetch_extra_texts(c, uid, ch),
                                             only=l.session_source, priority=True, attempts=1, timeout=10)
        except Exception as e:
            log.debug("stories/channel %s failed: %s", uid, e)
            return None
        if txt is None:
            return None              # частичный сбой — не кэшируем
        self._extra_text_cache[uid] = (time.monotonic(), txt)
        if len(self._extra_text_cache) > 50_000:
            cutoff = sorted(self._extra_text_cache.items(), key=lambda kv: kv[1][0])[:12_500]
            for k, _ in cutoff:
                self._extra_text_cache.pop(k, None)
        return txt

    def _streak_reason(self, l: Listing) -> str | None:
        """Не отправлять одну и ту же коллекцию подряд больше MAX_SAME_COLLECTION_STREAK раз."""
        n = settings.max_same_collection_streak
        if n <= 0:
            return None
        if self._is_expensive(l):
            return None     # дорогие лоты никогда не режем за «та же коллекция подряд»
        if l.gift_id in self._sending_gift_ids:
            return "same collection is being sent right now"
        # «подряд» — только пока прошлый пост свежий: через 5 минут та же коллекция снова можно
        if (self._last_sent_gift_id == l.gift_id and self._streak >= n
                and self.last_sent_at and time.time() - self.last_sent_at < 300):
            return f"same collection {n}x in a row"
        return None

    async def _repeat_reason(self, l: Listing) -> str | None:
        if settings.unique_gifts and (l.gift_unique_id in self._reserved_gifts
                                      or await self.db.gift_already_sent(l.gift_unique_id)):
            return "this gift was already sent"
        # См. _sent_before — слабые ключи (n/n:<имя>) не используем для дедупа продавца.
        if (settings.unique_sellers and l.seller_key
                and not l.seller_key.startswith("n")):
            if l.seller_key in self._reserved_sellers:
                return "this seller is being sent right now"
            since = None
            if settings.seller_repeat_hours > 0:
                since = (datetime.now(timezone.utc) - timedelta(hours=settings.seller_repeat_hours)
                         ).isoformat(timespec="seconds")
            if await self.db.seller_already_sent(l.seller_key, since):
                return "this seller was already sent"
        # тот же @username уже отправлялся (другой лот / другой ключ того же человека).
        # Респектуем SELLER_REPEAT_HOURS как в seller_key-пути выше — иначе один раз
        # отправленный юз никогда не вернётся, даже если admin поставил окно повтора.
        if settings.unique_sellers and l.seller_username:
            u_since = None
            if settings.seller_repeat_hours > 0:
                u_since = (datetime.now(timezone.utc) - timedelta(hours=settings.seller_repeat_hours)
                           ).isoformat(timespec="seconds")
            if await self.db.username_already_sent(l.seller_username, u_since):
                return "this seller was already sent"
        return None

    async def process(self, ev: ListingEvent) -> None:
        l = ev.listing
        while self.paused_getter and self.paused_getter():
            await asyncio.sleep(1)          # /off: не делаем запросов и не шлём, пока парсер выключен
        t_start = time.monotonic()
        # защита от завала: если с момента обнаружения прошло слишком много, лот уже не «свежий» — не шлём
        base = min(ev.detected_mono, l.t_detect) if l.t_detect is not None else ev.detected_mono
        age = t_start - base
        if ev.mode == "resume":
            # лот из прошлого запуска: считаем возраст по реальному времени обнаружения, а не по перезапуску
            try:
                age = max(age, (datetime.now(timezone.utc) - datetime.fromisoformat(l.first_seen)).total_seconds())
            except (TypeError, ValueError):
                pass
        if ev.mode != "lookup" and settings.max_lot_age > 0 and age > settings.max_lot_age:
            l.status, l.reason = ListingStatus.ERROR, f"stale: found {age:.0f}s ago"
            self.stats["stale"] = self.stats.get("stale", 0) + 1
            llog("FILTER", f"#{l.listing_id} skipped: {l.reason}")
            await self.db.update_listing(l)
            return
        price_txt = " / ".join(x for x in (
            f"{l.stars_price} Stars" if l.stars_price is not None else "",
            f"{l.ton_price:g} TON" if l.ton_price is not None else "") if x) or "no price"
        llog("PRICE", f"#{l.listing_id} {price_txt}")

        muted = self.muted_getter() if self.muted_getter else set()
        if muted and (l.gift_name or "").lower() in muted:
            l.status, l.reason = ListingStatus.FILTERED_PRICE, "коллекция выключена (/mute)"
            self.stats["muted"] = self.stats.get("muted", 0) + 1
            llog("FILTER", f"#{l.listing_id} skipped: collection muted")
            await self.db.update_listing(l)
            return

        pr = self.price_filter.check(l)
        if not pr.passed:
            l.status, l.reason = ListingStatus.FILTERED_PRICE, pr.reason
            self.stats["filtered_price"] += 1
            llog("FILTER", f"#{l.listing_id} price rejected: {pr.reason}")
            await self.db.update_listing(l)
            return
        llog("FILTER", f"#{l.listing_id} price passed")

        if settings.require_username and l.seller_type is not None and not l.seller_username:
            # @юза нет — лот не нужен. tg://user?id= не всегда открывает профиль (у многих
            # приватность: кидает в чат с ботом, а не в профиль). user явно сказал:
            # 'он перебрасывает бота а не в проф' — отбрасываем такие даже если funstat
            # нашёл user_id, но без @username.
            await self._reject_no_username(l)
            return
        if l.seller_type is None and not getattr(l, "lookup_done", False):
            # Скрытый владелец. Сначала то, что не стоит ни одного запроса в Telegram:
            #  • имя, которое фильтр и так отсеет (короткое, чужой алфавит, blacklist) — сразу мимо;
            #  • этот NFT / продавец уже отправлялся — сразу мимо.
            # ИСКЛЮЧЕНИЕ: дорогие лоты (>= SOFT_FILTER_STARS) не отсекаем по короткому имени и
            # чужому алфавиту ДО lookup: имя может сложиться нормально после открытия профиля,
            # а если нет — soft-путь в _check_seller всё равно пропустит с пометкой.
            soft = int(getattr(settings, "soft_filter_stars", 0) or 0)
            soft_applies = bool(soft and l.stars_price and l.stars_price >= soft)
            pre = self.seller_filter.check(l)
            if pre.code in _HIDDEN_HARD_REJECT:
                # blacklist режем всегда — это заведомо нежелательный продавец, цена роли не играет.
                if pre.code == "BLACKLIST" or not soft_applies:
                    l.seller_filter_result = pre.code
                    l.status, l.reason = ListingStatus.FILTERED_SELLER, pre.reason
                    self.stats["filtered_seller"] += 1
                    llog("FILTER", f"#{l.listing_id} seller rejected: {pre.code}")
                    await self.db.update_listing(l)
                    return
                llog("FILTER", f"#{l.listing_id} {pre.code}, но цена "
                               f"{l.stars_price}⭐ ≥ SOFT_FILTER_STARS={soft} — не режем до lookup")
            early = await self._sent_before(l)
            if early:
                l.status, l.reason = ListingStatus.FILTERED_DUPLICATE, early
                self.stats["filtered_repeat"] += 1
                llog("FILTER", f"#{l.listing_id} repeat skipped: {early}")
                await self.db.update_listing(l)
                return
            if settings.gift_page_owner:
                await self._resolve_hidden_owner(l)  # страница подарка (почти никогда не раскрывает владельца)
        if self._needs_lookup(l) and not getattr(l, "lookup_done", False):
            # владелец скрыт и через страницу подарка не нашёлся → ищем через бота поиска.
            # Имена, которые фильтр и так отсеет (короткие, чужой алфавит, blacklist), боту не шлём.
            # Поиск идёт ОТДЕЛЬНО от обработчиков: он медленный (ответ бота, капча), а остальные
            # лоты в это время должны идти дальше как обычно.
            # Для дорогих лотов (>= SOFT_FILTER_STARS) шлём в lookup даже с короткими именами /
            # чужим алфавитом — на той стороне профиль может вернуть нормальное имя.
            pre = self.seller_filter.check(l)
            soft = int(getattr(settings, "soft_filter_stars", 0) or 0)
            soft_applies = bool(soft and l.stars_price and l.stars_price >= soft)
            can_lookup = (pre.code not in _HIDDEN_HARD_REJECT
                          or (soft_applies and pre.code != "BLACKLIST"))
            if can_lookup:
                t = asyncio.get_running_loop().create_task(self._lookup_then_process(l))
                self._lookup_tasks.add(t)
                t.add_done_callback(self._lookup_tasks.discard)
                return
        # повтор NFT/продавца, который уже отправлялся, видно по БД — без лишних запросов в Telegram
        early = await self._sent_before(l)
        if early:
            l.status, l.reason = ListingStatus.FILTERED_DUPLICATE, early
            self.stats["filtered_repeat"] += 1
            llog("FILTER", f"#{l.listing_id} repeat skipped: {early}")
            await self.db.update_listing(l)
            return

        # несколько лотов одного продавца проверяются по очереди: второй берёт всё из кэша
        lk = self._seller_locks.setdefault(l.seller_id or 0, asyncio.Lock())
        # ВАЖНО: trim кешей делаем ТОЛЬКО если текущий lock-объект не входит в trim-список.
        # Раньше .clear() стирал словарь ЦЕЛИКОМ, и если второй лот того же продавца делал
        # setdefault в этот момент, он получал НОВЫЙ lock — критическая секция обходилась,
        # и один и тот же продавец проверялся дважды параллельно.
        if len(self._seller_locks) > 20_000:
            # удаляем НЕЗАНЯТЫЕ locks (без ждущих), кроме текущего
            current_key = l.seller_id or 0
            trash = [k for k, v in self._seller_locks.items()
                     if k != current_key and not v.locked()]
            # берём первые 50% unlocked чтобы не чистить слишком часто
            for k in trash[:len(self._seller_locks) // 2]:
                self._seller_locks.pop(k, None)
        async with lk:
            sr = await self._check_seller(l)
        if sr.code == "LOOKUP_FAILED" and not getattr(l, "lookup_retried", False):
            # все аккаунты были заняты/во FloodWait — не выбрасываем лот, а пробуем ещё раз через 15 с
            l.lookup_retried = True
            l.seller_lookup_failed = False
            self.stats["lookup_retry"] = self.stats.get("lookup_retry", 0) + 1
            llog("FILTER", f"#{l.listing_id} seller profile unavailable — retry in 15s")

            async def _later() -> None:
                await asyncio.sleep(15)
                await self._enqueue(ListingEvent(l, ev.detected_mono, ev.mode))
            t = asyncio.get_running_loop().create_task(_later())
            self._retries.add(t)                       # сильная ссылка, иначе задачу может съесть GC
            t.add_done_callback(self._retries.discard)
            return
        l.seller_filter_result = sr.code
        if not sr.passed:
            l.status, l.reason = ListingStatus.FILTERED_SELLER, sr.reason
            self.stats["filtered_seller"] += 1
            llog("FILTER", f"#{l.listing_id} seller rejected: {sr.code}")
            await self.db.update_listing(l)
            return
        llog("FILTER", f"#{l.listing_id} seller passed ({sr.code})")
        # user: 'он перебрасывает бота а не в проф' — без @username пост не нужен,
        # tg://user?id= у большинства уходит в чат бота, а не в профиль продавца.
        if settings.require_username and not l.seller_username:   # REQUIRE_USERNAME=true (по умолчанию)
            # Maker: дорогие лоты (>= SOFT_FILTER_STARS) пропускаем даже без @юза —
            # пост уйдёт с пометкой профиля, а не отфильтруется
            soft = int(getattr(settings, "soft_filter_stars", 0) or 0)
            if soft and l.stars_price and l.stars_price >= soft:
                llog("FILTER", f"#{l.listing_id} no @username, но цена {l.stars_price}⭐ ≥ "
                               f"SOFT_FILTER_STARS={soft} — пропускаем")
            else:
                await self._reject_no_username(l)
                return
        await self._send_stage(ev, l, base, t_start)

    async def _reject_no_username(self, l: Listing) -> None:
        l.seller_filter_result = "NO_USERNAME"
        l.status, l.reason = ListingStatus.FILTERED_SELLER, "у продавца нет @username"
        self.stats["filtered_seller"] += 1
        self.stats["no_username"] = self.stats.get("no_username", 0) + 1
        llog("FILTER", f"#{l.listing_id} seller rejected: NO_USERNAME")
        await self.db.update_listing(l)

    async def _sent_before(self, l: Listing) -> str | None:
        try:
            if settings.unique_gifts and await self.db.gift_already_sent(l.gift_unique_id):
                return "this gift was already sent"
            # Слабые ключи (seller_key='n' или 'n:<имя>') общие для всех одноимённых скрытых —
            # дедуп по имени ЛОЖНО считает их одним продавцом. Пропускаем дедуп для таких;
            # для сильных ключей (u<id>/c<id>/g<id>/a<...>) дедуп работает как обычно.
            if (settings.unique_sellers and l.seller_key
                    and not l.seller_key.startswith("n")):
                since = None
                if settings.seller_repeat_hours > 0:
                    since = (datetime.now(timezone.utc) - timedelta(hours=settings.seller_repeat_hours)
                             ).isoformat(timespec="seconds")
                if await self.db.seller_already_sent(l.seller_key, since):
                    return "this seller was already sent"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # БД может временно сбоить (блокировка, диск) — не ломаем pipeline, не блокируем лот
            log.warning("_sent_before query failed for #%s: %s — assume not sent", l.listing_id, e)
            return None
        return None

    async def _resolve_hidden_owner(self, l: Listing) -> None:
        """Продавец скрыт в выдаче маркета (видно только имя) — ищем его аккаунт через страницу подарка,
        чтобы были @username / ссылка на профиль и обычная проверка продавца."""
        if l.seller_type is not None or not l.gift_slug:
            return
        slug = l.gift_slug
        try:
            user, session = await self.sessions.run(lambda c: fetch_gift_owner(c, slug), priority=True,
                                                    prefer=l.session_source, attempts=2, timeout=10)
        except Exception as e:
            log.debug("hidden owner of %s: %s", slug, e)
            return
        uid = getattr(user, "id", None)
        if not uid:
            self.stats["hidden_owner_unknown"] = self.stats.get("hidden_owner_unknown", 0) + 1
            return
        self.stats["hidden_owner_found"] = self.stats.get("hidden_owner_found", 0) + 1
        l.session_source = session                 # доступ к этому продавцу есть у этой session
        l.seller_type, l.seller_id, l.seller_key = "user", uid, f"u{uid}"
        l.seller_username = _username_of(user) or l.seller_username
        l.seller_profile_link = username_link(l.seller_username)
        name = " ".join(x for x in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if x)
        l.seller_name = name or l.seller_name
        l.premium = getattr(user, "premium", None)
        l.seller_phone = getattr(user, "phone", None)
        if not getattr(user, "min", False):
            l.messages = _messages_status(getattr(user, "send_paid_messages_stars", None),
                                          getattr(user, "contact_require_premium", None))
        llog("FILTER", f"#{l.listing_id} hidden owner resolved → u{uid}"
                       + (f" @{l.seller_username}" if l.seller_username else ""))

    async def _check_seller(self, l: Listing) -> SellerResult:
        if getattr(l, "owner_not_found", False) and l.seller_type is None:
            # Maker: дорогие лоты (>= SOFT_FILTER_STARS) шлём даже с ненайденным владельцем,
            # с пометкой profile hidden. Платные ЛС / фильтр языка не трогаются (они срабатывают
            # только когда профиль прочитан, а здесь профиля нет — значит и этих рисков тоже нет).
            soft = int(getattr(settings, "soft_filter_stars", 0) or 0)
            if soft and l.stars_price and l.stars_price >= soft:
                return SellerResult(True, "OWNER_NOT_FOUND_SENT",
                                    "скрытый владелец — пост с пометкой (soft filter)")
            return SellerResult(False, "OWNER_NOT_FOUND", "владелец не найден — без @username не шлём")
        # Владелец от funstat по доверию имени: пробуем прочитать профиль через failover
        # (resale-страница коллекции отдаёт access_hash → профиль станет доступен). Если
        # получилось — _enrich_seller снимет флаг owner_trusted_no_resolve, и мы ПРОВАЛИМСЯ
        # сквозь if в обычный путь (_check_seller_found + override-логика). Это нужно
        # чтобы для trust-fallback тоже отсеивать платные ЛС
        # (Maker: 'чтоб соо не платные в лс были').
        if getattr(l, "owner_trusted_no_resolve", False):
            await self._enrich_seller(l)
            if getattr(l, "owner_trusted_no_resolve", False):
                # enrich не смог — остаёмся на доверии, но хотя бы blacklist / то что есть
                pre = self.seller_filter.check(l)
                if pre.code in ("BLACKLIST", "PAID_MESSAGES"):
                    return pre
                self.stats["owner_trusted_sent"] = self.stats.get("owner_trusted_sent", 0) + 1
                return SellerResult(True, "OWNER_TRUSTED", "owner trusted via lookup bot (name match)")
            # enrich удался — проваливаемся в обычный путь ниже, где работают override-правила
            # для linguistic-кодов (FOREIGN_TEXT и т.п.) т.к. owner_found_via установлен.
            self.stats["trusted_enriched_fallthrough"] = \
                self.stats.get("trusted_enriched_fallthrough", 0) + 1
        sr = await self._check_seller_found(l)
        # Владелец найден через funstat: мягкая фильтрация, НО не для «жёстких» причин:
        # - BLACKLIST: сам пользователь банил
        # - PAID_MESSAGES: пользователь явно не хочет (user: 'выдает людей с платным соо убери')
        # - TOO_MANY_NFTS / LEVEL_TOO_HIGH: пользовательские лимиты имеют смысл
        # Переопределяем только лингвистические/профильные (UNKNOWN_BLOCKED, FOREIGN_TEXT, PHONE_MISMATCH),
        # и только если LOOKUP_LANG_FILTER=False (при True — язык проверяется даже для found).
        # LOOKUP_FAILED убрано: если enrich упал, но funstat нашёл владельца — доверяем funstat'у
        # (теряем paid-messages защиту для таких лотов, но лучше чем терять лот совсем).
        # ARABIC_SCRIPT: раньше владелец, найденный через funstat, «перебивал» отказ — арабы пролезали
        _HARD_REJECT_EVEN_IF_FOUND = {"BLACKLIST", "ARABIC_SCRIPT", "PAID_MESSAGES", "TOO_MANY_NFTS", "LEVEL_TOO_HIGH"}
        # Если LOOKUP_LANG_FILTER включён, не override'им лингвистические отказы
        skip_override = sr.code in _LANG_CODES and settings.lookup_lang_filter
        if (not sr.passed and l.owner_found_via and sr.code not in _HARD_REJECT_EVEN_IF_FOUND
                and not skip_override):
            # Прежде чем override'нуть — ЕСЛИ seller_nfts ещё не читали и lookup вытащил real_id,
            # проверим NFT (чтобы спамеров-коллекционеров не пропустить). Для pseudo-id пропускаем.
            if (settings.max_seller_nfts > 0 and l.seller_nfts is None
                    and l.seller_id and l.seller_id > 0):
                try:
                    l.seller_nfts = await self._nft_count(l)
                except Exception:
                    pass
            if (settings.max_seller_nfts > 0 and l.seller_nfts is not None
                    and l.seller_nfts > settings.max_seller_nfts):
                return SellerResult(False, "TOO_MANY_NFTS",
                                    f"{l.seller_nfts} NFT > MAX_SELLER_NFTS={settings.max_seller_nfts}")
            llog("FILTER", f"#{l.listing_id} seller rejected by {sr.code} but owner found via lookup bot — overriding")
            self.stats["owner_override"] = self.stats.get("owner_override", 0) + 1
            return SellerResult(True, "OWNER_FOUND_OVERRIDE",
                                f"owner found via lookup bot (ignored {sr.code})")
        return sr

    async def _check_seller_found(self, l: Listing) -> SellerResult:
        # Платные сообщения / только Premium видны уже на странице маркета — тот же фильтр, просто без
        # лишнего запроса профиля (на этом отсеивается заметная часть лотов)
        if l.seller_type == "user" and l.messages is not None and l.messages != "бесплатно":
            pre = self.seller_filter.check(l)
            if pre.code in ("BLACKLIST", "PAID_MESSAGES"):
                return pre

        return await self._check_seller_inner(l)

    async def _check_seller_inner(self, l: Listing) -> SellerResult:
        await self._enrich_seller(l)
        # Владелец от funstat по доверию имени: профиль мы не читаем принципиально,
        # отбрасывать лот по max_seller_level не надо — это ломает полезный fallback.
        if (getattr(l, "seller_lookup_failed", False) and settings.max_seller_level > 0
                and not getattr(l, "owner_trusted_no_resolve", False)
                and not getattr(l, "owner_from_memory", False)
                and not l.owner_found_via):
            # профиль не прочитался ни с одного аккаунта → Level неизвестен; не шлём вслепую.
            # Исключение: trusted/from_memory/owner_found_via — owner уже проверен (funstat или память),
            # доверяем. Иначе теряем лот с реально найденным владельцем из-за временной перегрузки сессий.
            return SellerResult(False, "LOOKUP_FAILED", "seller profile unavailable (all accounts busy)")

        # Сначала проверяем то, что уже известно (сообщения, Level, язык), и только если продавец
        # прошёл — отдельный запрос за количеством его NFT. Отсеянным продавцам лишний запрос не нужен.
        # Отрицательные seller_id — pseudo-ID (из хеша username, настоящего ID у нас нет) — пропускаем
        # все профильные запросы: они гарантированно упадут 'Could not find input entity'.
        has_real_id = l.seller_type == "user" and l.seller_id and l.seller_id > 0
        sr = self.seller_filter.check(l)
        if sr.passed and settings.max_seller_nfts > 0 and has_real_id:
            l.seller_nfts = await self._nft_count(l)
            sr = self.seller_filter.check(l)
        # продавец без телефона/био/канала: последний объективный шанс — язык текстов к его подаркам
        if sr.code == "UNKNOWN_BLOCKED" and has_real_id:
            l.seller_gift_text = await self._gift_texts(l)
            sr = self.seller_filter.check(l)
        # всё ещё непонятно — смотрим истории и посты личного канала
        # (2-3 лишних запроса на продавца — только для дорогих лотов, чтобы не перегружать аккаунты)
        if (sr.code == "UNKNOWN_BLOCKED" and has_real_id and self._is_expensive(l)):
            l.seller_extra_text = await self._extra_texts(l)
            sr = self.seller_filter.check(l)
        # продавец прошёл только по текстам/историям — лимит NFT ещё не проверяли
        if (sr.passed and l.seller_nfts is None and settings.max_seller_nfts > 0 and has_real_id):
            l.seller_nfts = await self._nft_count(l)      # часто уже в кэше из запроса текстов подарков
            sr = self.seller_filter.check(l)
        return sr

    async def _send_stage(self, ev: ListingEvent, l: Listing, base: float, t_start: float) -> None:
        # Последняя проверка перед постом (сюда приходят и «доверенные» владельцы от funstat,
        # у которых профиль не прочитался — раньше они проходили с платными ЛС):
        guard = None
        # Возраст лота с момента обнаружения — для ВСЕХ лотов, включая найденных через funstat
        # (раньше «владелец найден → по возрасту не бросаем», а поиск с капчей идёт до 10 мин).
        limit = settings.max_lot_age
        if ev.mode == "lookup" or getattr(l, "owner_found_via", None):
            # поиск владельца сам ограничен LOOKUP_MAX_AGE; +45 с на проверку профиля и отправку,
            # иначе владелец, найденный на 170-й секунде, выбрасывался как «старый»
            limit = max(limit, settings.lookup_max_age + 45) if settings.lookup_max_age > 0 else limit
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(l.first_seen)).total_seconds()
        except (TypeError, ValueError):
            age = 0.0
        if limit > 0 and age > limit:
            l.status, l.reason = ListingStatus.ERROR, f"stale: {age:.0f}s после обнаружения (> {limit}s)"
            self.stats["stale"] = self.stats.get("stale", 0) + 1
            llog("FILTER", f"#{l.listing_id} skipped before send: {l.reason}")
            await self.db.update_listing(l)
            return
        no_valid_user = ((settings.require_username and not l.seller_username) or l.seller_type != "user"
                         or not l.seller_id or l.seller_id <= 0)
        if no_valid_user:
            # Maker: для лотов >= SOFT_FILTER_STARS отсутствие @юза / неподтверждённого владельца
            # не блокирует отправку — пост уходит с пометкой 'profile hidden'. Платные ЛС и
            # LEVEL_TOO_HIGH всё равно проверяются ниже.
            soft = int(getattr(settings, "soft_filter_stars", 0) or 0)
            if soft and l.stars_price and l.stars_price >= soft:
                l.owner_not_found = True
                llog("FILTER", f"#{l.listing_id} NO_USERNAME на before-send, но цена "
                               f"{l.stars_price}⭐ ≥ SOFT_FILTER_STARS={soft} — пропускаем "
                               "с пометкой profile hidden")
            else:
                guard = ("NO_USERNAME", "у продавца нет @username / владелец не подтверждён")
        if not guard and settings.require_free_messages and l.messages != "бесплатно":
            guard = ("PAID_MESSAGES", f"ЛС: {l.messages or 'не удалось проверить'}")
        elif not guard and settings.max_seller_level > 0 and (l.level or 0) > settings.max_seller_level:
            guard = ("LEVEL_TOO_HIGH", f"level {l.level} > {settings.max_seller_level}")
        if guard:
            l.seller_filter_result = guard[0]
            l.status, l.reason = ListingStatus.FILTERED_SELLER, guard[1]
            self.stats["filtered_seller"] += 1
            llog("FILTER", f"#{l.listing_id} rejected before send: {guard[0]} ({guard[1]})")
            await self.db.update_listing(l)
            return
        # повторы: тот же NFT или тот же продавец уже отправлялись → не отправляем
        async with self._released:
            # тот же продавец / коллекция отправляется прямо сейчас — ждём, чем кончится, а не выбрасываем
            # (если та отправка сорвётся, этот лот всё равно уйдёт)
            deadline = time.monotonic() + 30
            while True:
                repeat = await self._repeat_reason(l)
                if repeat is None:
                    repeat = self._streak_reason(l)
                if repeat not in _IN_FLIGHT or time.monotonic() > deadline:
                    break
                try:
                    await asyncio.wait_for(self._released.wait(), timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    pass
            if repeat is None:
                self._reserved_gifts.add(l.gift_unique_id)
                self._reserved_sellers.add(l.seller_key)
                self._sending_gift_ids.add(l.gift_id)
        if repeat:
            l.status, l.reason = ListingStatus.FILTERED_DUPLICATE, repeat
            self.stats["filtered_repeat"] += 1
            llog("FILTER", f"#{l.listing_id} repeat skipped: {repeat}")
            await self.db.update_listing(l)
            return

        res = None
        try:
            # render ВНУТРИ try: если он бросит исключение, finally всё равно снимет резерв.
            # Иначе краш рендера навсегда блокировал бы этот NFT/продавца/коллекцию (резерв не снят).
            t_checked = time.monotonic()
            if (ev.mode not in ("resume", "lookup") and settings.max_lot_age > 0
                    and t_checked - base > settings.max_lot_age):
                raise _Stale(f"stale: {t_checked - base:.0f}s after detection (slow seller check)")
            if l.t_detect is not None:
                # «Обнаружен за» — от появления лота до того, как трекер его увидел (без проверки продавца)
                l.detect_secs = max(0.0, (l.t_found or t_checked) - l.t_detect)
            text = self.formatter.render(l)
            l.status = ListingStatus.SENDING          # если процесс упадёт во время отправки — не дублируем
            await self.db.update_listing(l)
            # тема по реальной стоимости гифта (флор коллекции), а не по цене, которую поставил продавец
            _value = l.floor_stars if (settings.route_by == "floor" and l.floor_stars) else l.stars_price
            _chat, _thread = settings.route_chat(_value, l.backdrop)
            res = await self.sender.send_listing(text, l.gift_link, listing_keyboard(l),
                                                 listing_keyboard(l, with_seller=False),
                                                 target=_chat, thread=_thread,
                                                 priority=self._is_expensive(l))
            if res.ok:
                l.sent = not res.test_only
                l.message_id = res.message_id
                l.target_chat_id = _chat
                l.target_thread_id = _thread
                l.status = ListingStatus.TEST_ONLY if res.test_only else ListingStatus.SENT
                if not res.test_only:
                    self.stats["sent"] += 1
                    self.last_sent_at = time.time()
                t_sent = time.monotonic()
                latency = t_sent - ev.detected_mono
                dest = "test" if res.test_only else (f"{_chat}/topic{_thread}" if _thread else f"{_chat}")
                llog("SEND", f"listing={l.listing_id} → {dest} latency={latency:.2f}s")
                if l.t_detect is not None and l.t_found is not None:
                    parts = {"request": l.t_found - l.t_detect, "queue": t_start - l.t_found,
                             "check": t_checked - t_start, "send": t_sent - t_checked,
                             "total": t_sent - l.t_detect}
                    self.totals.append((parts["total"], self._is_expensive(l)))
                    for k, v in parts.items():
                        old = self.latency.get(k)
                        self.latency[k] = v if old is None else old * 0.8 + v * 0.2
                    llog("LATENCY", f"listing={l.listing_id} " + " ".join(f"{k}={v:.2f}s" for k, v in parts.items()))
            else:
                l.status, l.reason = ListingStatus.SEND_FAILED, res.error
                self.stats["failed"] += 1
                llog("SEND", f"listing={l.listing_id} FAILED: {res.error}", level=40)
            await self.db.update_listing(l)   # sent=1 записан ДО снятия резерва → нет гонки повторов
        except _Stale as e:
            l.status, l.reason = ListingStatus.ERROR, str(e)
            self.stats["stale"] = self.stats.get("stale", 0) + 1
            llog("FILTER", f"#{l.listing_id} skipped: {e}")
            await self.db.update_listing(l)
        finally:
            async with self._released:
                self._released.notify_all()
                self._reserved_gifts.discard(l.gift_unique_id)
                self._reserved_sellers.discard(l.seller_key)
                self._sending_gift_ids.discard(l.gift_id)
                if res is not None and res.ok and not res.test_only:
                    if self._last_sent_gift_id == l.gift_id:
                        self._streak += 1
                    else:
                        self._last_sent_gift_id, self._streak = l.gift_id, 1


    # ------------------------------------------------------------------ автопоиск скрытого владельца
    def _needs_lookup(self, l: Listing) -> bool:
        lk = self.owner_lookup
        return (lk is not None and lk.enabled() and l.seller_type is None and bool(l.seller_name)
                and bool(l.gift_slug or l.gift_unique_id))

    async def _lookup_then_process(self, l: Listing) -> None:
        # Жёсткий таймаут на весь lookup. Иначе task может висеть на сессии в FloodWait
        # + ожидании капчи + ответа funstat десятки минут (user: '20 мин подряд потом стоп').
        # До 3 капч подряд × (captcha_wait + lookup_timeout) + подстраховка 30с.
        # Для 90с capchta_wait и 15с lookup_timeout → ~345s. Иначе лоты с несколькими капчами
        # тонут в 150с таймауте и теряются до admin fallback'а.
        # ×5 после увеличения captcha_rounds до 5 (было 3), но capped 600с.
        timeout = max(60.0, min(600.0, (settings.lookup_captcha_wait + settings.lookup_timeout) * 5 + 30))
        infra_failure = False       # True = бот жив, но не ответил в срок (timeout/exception) → не наказываем лот
        try:
            found = await asyncio.wait_for(self._lookup_owner(l), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("Owner lookup for %s (%r) timed out after %.0fs — освобождаем pipeline",
                        l.listing_id, l.seller_name, timeout)
            found = False
            infra_failure = True
            self.stats["lookup_timeout"] = self.stats.get("lookup_timeout", 0) + 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Owner lookup for %s (%r) failed: %s", l.listing_id, l.seller_name, e)
            found = False
            infra_failure = True
            self.stats["lookup_exception"] = self.stats.get("lookup_exception", 0) + 1
        l.lookup_done = True
        # пока ждали бота поиска (очередь, капча), лот мог сильно устареть.
        # НО: если владелец найден — не бросаем по возрасту, пусть lookup_check_onsale решает
        # (лот может быть ещё в продаже — он всё равно ценный).
        if settings.lookup_max_age > 0 and not found:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(l.first_seen)).total_seconds()
            except (TypeError, ValueError):
                age = 0.0
            if age > settings.lookup_max_age:
                l.status, l.reason = ListingStatus.ERROR, f"stale: owner lookup took {age:.0f}s"
                self.stats["stale"] = self.stats.get("stale", 0) + 1
                llog("FILTER", f"#{l.listing_id} skipped: lookup finished {age:.0f}s after detection")
                await self.db.update_listing(l)
                return
        if not found:
            # SOFT-FILTER RESCUE: селективное смягчение в зоне "дорогих" лотов.
            # Логика Maker: 400-1000⭐ — строгие фильтры (там скам чаще), 1000+ — мягкие.
            # Порог берём из SOFT_FILTER_STARS (той же настройки, что рулит рescue в
            # SHORT_NAME_HIDDEN и NO_USERNAME ветках выше). Хочешь мягче — /set SOFT_FILTER_STARS 700.
            soft = int(getattr(settings, "soft_filter_stars", 0) or 0)
            if soft and l.stars_price and l.stars_price >= soft:
                l.owner_not_found = True
                self.stats["owner_not_found_soft_rescued"] = \
                    self.stats.get("owner_not_found_soft_rescued", 0) + 1
                llog("FILTER", f"#{l.listing_id} владелец не найден, но цена "
                               f"{l.stars_price}⭐ ≥ SOFT_FILTER_STARS={soft} — шлём с пометкой")
                # НЕ return — проваливаемся дальше в lookup_check_onsale и обычный flow
            else:
                # Без подтверждённого владельца с @username лот НЕ шлём (раньше при молчащем боте
                # уходил пост «⚠️ владелец не найден» без юза).
                all_dead = (self.owner_lookup is not None and hasattr(self.owner_lookup, "all_bots_dead")
                            and self.owner_lookup.all_bots_dead())
                if all_dead:
                    code, why = "OWNER_BOTS_DEAD", "боты поиска не отвечают — владелец не проверен"
                elif infra_failure:
                    code, why = "OWNER_LOOKUP_TIMEOUT", "бот поиска не ответил вовремя — владелец не проверен"
                else:
                    code, why = "OWNER_NOT_FOUND", "владелец не найден ботом поиска"
                l.status, l.reason = ListingStatus.FILTERED_SELLER, why
                l.seller_filter_result = code
                self.stats["filtered_seller"] += 1
                self.stats["owner_not_found"] = self.stats.get("owner_not_found", 0) + 1
                llog("FILTER", f"#{l.listing_id} hidden owner not verified ({code}) — skipped")
                await self.db.update_listing(l)
                return
        # пока ждали бота поиска, лот могли купить или снять — проверяем, что он ещё продаётся
        if settings.lookup_check_onsale and l.gift_slug:
            on_sale = await self._still_on_sale(l)
            if on_sale is False:
                l.status, l.reason = ListingStatus.ERROR, "лот уже продан или снят с продажи (пока искали владельца)"
                self.stats["sold_while_lookup"] = self.stats.get("sold_while_lookup", 0) + 1
                llog("FILTER", f"#{l.listing_id} sold/removed while looking up the owner — skipped")
                await self.db.update_listing(l)
                return
        # дальше — обычная проверка и отправка; время поиска в «устаревание» лота не считаем.
        # Таймаут 10 мин — чтобы одна зависшая сессия/sender не копили lookup-задачи вечно
        # (без него nested process() висел без ограничения, Maker видел «process timeout 10min»
        # через worker, но для lookup-ветки такого щита не было).
        try:
            await asyncio.wait_for(
                self.process(ListingEvent(l, time.monotonic(), "lookup")), timeout=600)
        except asyncio.TimeoutError:
            log.warning("process(%s) после lookup завис > 10 мин — освобождаем", l.listing_id)
            l.status, l.reason = ListingStatus.ERROR, "post-lookup process timeout 10min"
            try:
                await self.db.update_listing(l)
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            # ЛЮБАЯ ошибка в process не должна убивать lookup worker-задачу молча — иначе
            # пайплайн 'внезапно обрывается' (user: 'потом резко все обрывается')
            log.exception("process(%s) после lookup упал — лот теряется", l.listing_id)
            l.status, l.reason = ListingStatus.ERROR, "exception in process after lookup"
            try:
                await self.db.update_listing(l)
            except Exception:
                pass

    async def _still_on_sale(self, l: Listing) -> bool | None:
        """True — лот на маркете, False — продан/снят, None — не удалось проверить (не мешаем отправке)."""
        slug = l.gift_slug

        async def _get(client):
            from telethon.tl.functions.payments import GetUniqueStarGiftRequest
            return await client(GetUniqueStarGiftRequest(slug=slug))
        try:
            res, _ = await self.sessions.run(_get, attempts=2, timeout=15, priority=True)
        except Exception as e:
            log.debug("on-sale check %s failed: %s", slug, e)
            return None
        g = getattr(res, "gift", None)
        if g is None:
            return None
        return bool(getattr(g, "resell_amount", None))

    async def _lookup_owner(self, l: Listing) -> bool:
        """Скрытый владелец: пишем боту поиска «/human <имя>», проверяем кандидатов (открыт ли у них
        этот NFT) и подставляем найденного как обычного продавца. True — найден."""
        # дорогие лоты — первыми в очереди к боту поиска
        value = l.stars_price or l.floor_stars or int((l.ton_price or 0) * 300)
        deadline = None
        if settings.lookup_max_age > 0:
            try:
                deadline = datetime.fromisoformat(l.first_seen).timestamp() + settings.lookup_max_age
            except (TypeError, ValueError):
                deadline = time.time() + settings.lookup_max_age
        owner = await self.owner_lookup.find_owner(l.seller_name, l.gift_slug, l.gift_unique_id,
                                                   prio=-float(value or 0), deadline=deadline)
        if owner is None:
            return False
        uid = owner.user_id
        # Защита: owner.user_id = 0 — поломанная запись (не должно случаться после фильтра в _known
        # и после fallback'а в _verify, но если проскочит — не создаём лот с seller_key='u0')
        if not uid:
            log.warning("Owner lookup returned uid=0 for %s — считаем не найденным", l.seller_name)
            return False
        l.seller_type, l.seller_id, l.seller_key = "user", uid, f"u{uid}"
        l.seller_username = owner.username
        l.seller_profile_link = username_link(owner.username) if owner.username else None
        if owner.premium is not None:
            l.premium = owner.premium
        if owner.session:
            l.session_source = owner.session       # у этого аккаунта есть доступ к профилю владельца
        elif not getattr(owner, "from_memory", False):
            # funstat выдал владельца по доверию имени, никакой session не резолвила access_hash
            # (fallback-trust_name_match). _enrich_seller его пропустит, лот пойдёт как есть.
            l.owner_trusted_no_resolve = True
        else:
            # from_memory: session_source=None, enrich попробует любую сессию. Если упадёт,
            # LOOKUP_FAILED → мы помечаем owner_from_memory чтобы не блокировать.
            l.owner_from_memory = True
        # Если найдено через fallback-бота из LOOKUP_BOTS, бот отличается от primary
        # (settings.lookup_bot_username). Берём реальный бот из FoundOwner.bot — иначе
        # /stats и логи показывали ложь: "найдено через funstat", когда funstat мёртв и
        # ответил infostat. Fallback — primary из настроек, если бот не проставлен.
        l.owner_found_via = (getattr(owner, "bot", None) or settings.lookup_bot_username).lstrip("@")
        self.stats["owner_found"] = self.stats.get("owner_found", 0) + 1
        llog("FILTER", f"#{l.listing_id} hidden owner found via @{l.owner_found_via} → u{uid}"
                       + (f" @{owner.username}" if owner.username else ""))
        return True


async def fetch_seller_bundle(client, user_id: int, nfts: bool = True, texts: bool = False):
    """Профиль продавца, число его NFT и тексты к подаркам ОДНИМ заходом (запросы уходят параллельно
    в одном «окне» аккаунта) — для дорогих лотов: вместо 2-3 пауз между запросами одна.
    → (профиль, число NFT | None, (тексты, NFT) | None); сбой второстепенных запросов → None."""
    async def _none():
        return None
    full, n, t = await asyncio.gather(
        fetch_full_user(client, user_id),
        fetch_nft_count(client, user_id) if nfts else _none(),
        fetch_gift_texts(client, user_id) if texts else _none(),
        return_exceptions=True)
    if isinstance(full, BaseException):
        raise full
    return full, (None if isinstance(n, BaseException) else n), (None if isinstance(t, BaseException) else t)
