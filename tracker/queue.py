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
from utils.links import gift_link, username_link
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
        gift_slug=it.slug,
        gift_link=gift_link(it.slug),
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
    )


@dataclass

class ListingEvent:
    listing: Listing
    detected_mono: float
    mode: str


_IN_FLIGHT = ("this seller is being sent right now", "same collection is being sent right now")


class _Stale(Exception):
    """Лот устарел, пока проверялся продавец — не отправляем."""


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

    # ------------------------------------------------------------------ admission (global dedup)
    async def admit(self, items: list[ResaleItem], session: str | None, mode: str,
                    floor_stars: int | None = None, t_detect: float | None = None) -> int:
        """items — сверху вниз (новейшие первыми). В очередь кладём от старых к новым."""
        admitted = 0
        t_found = time.monotonic()
        for it in reversed(items):
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
            self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"processor-{i}"))

    async def stop(self) -> None:
        for t in list(self._workers) + list(self._retries):
            t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _worker_loop(self, idx: int) -> None:
        while True:
            _, _, ev = await self.queue.get()
            try:
                await self.process(ev)
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
        cached = self._seller_cache.get(l.seller_id)
        if cached and time.monotonic() - cached[0] < settings.seller_cache_ttl:
            full = cached[1]
        else:
            uid = l.seller_id
            try:
                # access_hash продавца есть у той session, которая получила лот → сначала на ней
                nc = self._nft_cache.get(uid)
                if (self._is_expensive(l) and settings.max_seller_nfts > 0
                        and not (nc and time.monotonic() - nc[0] < settings.seller_cache_ttl)):
                    # дорогой лот: профиль и число NFT одним заходом — минус одна пауза аккаунта
                    (full, nfts), _ = await self.sessions.run(lambda c: fetch_seller_bundle(c, uid),
                                                              only=l.session_source, priority=True, attempts=2,
                                                              timeout=15)
                    if nfts is not None:
                        self._nft_cache[uid] = (time.monotonic(), nfts)
                else:
                    full, _ = await self.sessions.run(lambda c: fetch_full_user(c, uid),
                                                      only=l.session_source, priority=True, attempts=2, timeout=15)
            except Exception as e:
                # failover: session умерла / FloodWait / занята — берём любую другую живую
                gid = l.gift_id
                log.info("Seller %s: session %s unavailable (%s) — switching to another session",
                         uid, l.session_source, type(e).__name__)
                try:
                    full, other = await self.sessions.run(lambda c: fetch_full_user_failover(c, uid, gid),
                                                          attempts=3, timeout=20)
                    l.session_source = other      # дальнейшие проверки продавца — на рабочей session
                    self.stats["failover"] = self.stats.get("failover", 0) + 1
                except Exception as e2:
                    log.debug("getFullUser %s failover failed: %s", uid, e2)
                    l.seller_lookup_failed = True
                    return
            self._seller_cache[uid] = (time.monotonic(), full)
            if len(self._seller_cache) > 50_000:
                self._seller_cache.clear()
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
            self._nft_cache.clear()
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
            self._gift_text_cache.clear()
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
            self._extra_text_cache.clear()
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
        if settings.unique_sellers and l.seller_key not in ("n", ""):
            if l.seller_key in self._reserved_sellers:
                return "this seller is being sent right now"
            since = None
            if settings.seller_repeat_hours > 0:
                since = (datetime.now(timezone.utc) - timedelta(hours=settings.seller_repeat_hours)
                         ).isoformat(timespec="seconds")
            if await self.db.seller_already_sent(l.seller_key, since):
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
        if settings.max_lot_age > 0 and age > settings.max_lot_age:
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

        await self._resolve_hidden_owner(l)          # скрытый в выдаче продавец → ищем его аккаунт
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
        if len(self._seller_locks) > 20_000:
            self._seller_locks.clear()
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
        await self._send_stage(ev, l, base, t_start)

    async def _sent_before(self, l: Listing) -> str | None:
        if settings.unique_gifts and await self.db.gift_already_sent(l.gift_unique_id):
            return "this gift was already sent"
        if settings.unique_sellers and l.seller_key not in ("n", ""):
            since = None
            if settings.seller_repeat_hours > 0:
                since = (datetime.now(timezone.utc) - timedelta(hours=settings.seller_repeat_hours)
                         ).isoformat(timespec="seconds")
            if await self.db.seller_already_sent(l.seller_key, since):
                return "this seller was already sent"
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
        # Платные сообщения / только Premium видны уже на странице маркета — тот же фильтр, просто без
        # лишнего запроса профиля (на этом отсеивается заметная часть лотов)
        if l.seller_type == "user" and l.messages is not None and l.messages != "бесплатно":
            pre = self.seller_filter.check(l)
            if pre.code in ("BLACKLIST", "PAID_MESSAGES"):
                return pre

        return await self._check_seller_inner(l)

    async def _check_seller_inner(self, l: Listing) -> SellerResult:
        await self._enrich_seller(l)
        if getattr(l, "seller_lookup_failed", False) and settings.max_seller_level > 0:
            # профиль не прочитался ни с одного аккаунта → Level неизвестен; не шлём вслепую
            return SellerResult(False, "LOOKUP_FAILED", "seller profile unavailable (all accounts busy)")

        # Сначала проверяем то, что уже известно (сообщения, Level, язык), и только если продавец
        # прошёл — отдельный запрос за количеством его NFT. Отсеянным продавцам лишний запрос не нужен.
        sr = self.seller_filter.check(l)
        if sr.passed and settings.max_seller_nfts > 0 and l.seller_type == "user" and l.seller_id:
            l.seller_nfts = await self._nft_count(l)
            sr = self.seller_filter.check(l)
        # продавец без телефона/био/канала: последний объективный шанс — язык текстов к его подаркам
        if sr.code == "UNKNOWN_BLOCKED" and l.seller_type == "user" and l.seller_id:
            l.seller_gift_text = await self._gift_texts(l)
            sr = self.seller_filter.check(l)
        # всё ещё непонятно — смотрим истории и посты личного канала
        # (2-3 лишних запроса на продавца — только для дорогих лотов, чтобы не перегружать аккаунты)
        if (sr.code == "UNKNOWN_BLOCKED" and l.seller_type == "user" and l.seller_id
                and self._is_expensive(l)):
            l.seller_extra_text = await self._extra_texts(l)
            sr = self.seller_filter.check(l)
        # продавец прошёл только по текстам/историям — лимит NFT ещё не проверяли
        if (sr.passed and l.seller_nfts is None and settings.max_seller_nfts > 0
                and l.seller_type == "user" and l.seller_id):
            l.seller_nfts = await self._nft_count(l)      # часто уже в кэше из запроса текстов подарков
            sr = self.seller_filter.check(l)
        return sr

    async def _send_stage(self, ev: ListingEvent, l: Listing, base: float, t_start: float) -> None:
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
            if ev.mode != "resume" and settings.max_lot_age > 0 and t_checked - base > settings.max_lot_age:
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


async def fetch_seller_bundle(client, user_id: int):
    """Профиль продавца и число его NFT ОДНИМ заходом (оба запроса уходят параллельно в одном «окне»
    аккаунта) — для дорогих лотов: вместо двух пауз между запросами одна. Сбой счётчика NFT → None."""
    full, nfts = await asyncio.gather(fetch_full_user(client, user_id), fetch_nft_count(client, user_id),
                                      return_exceptions=True)
    if isinstance(full, BaseException):
        raise full
    return full, (None if isinstance(nfts, BaseException) else nfts)
