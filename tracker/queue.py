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
from dataclasses import dataclass
from typing import Any


from datetime import datetime, timedelta, timezone

from bot.formatter import MessageFormatter, listing_keyboard
from bot.sender import TelegramSender
from config import settings
from database.db import Database
from database.models import Listing, ListingStatus
from filters.price_filter import PriceFilter
from filters.seller_filter import SellerFilter
from sessions.manager import SessionManager
from tracker.marketplace import ResaleItem, SellerFull, fetch_full_user, fetch_gift_texts, fetch_nft_count
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


class ListingPipeline:
    def __init__(self, db: Database, sessions: SessionManager, sender: TelegramSender,
                 formatter: MessageFormatter, price_filter: PriceFilter, seller_filter: SellerFilter):
        self.db = db
        self.sessions = sessions
        self.sender = sender
        self.formatter = formatter
        self.price_filter = price_filter
        self.seller_filter = seller_filter
        self.queue: asyncio.Queue[ListingEvent] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._seller_cache: dict[int, tuple[float, SellerFull]] = {}
        self._nft_cache: dict[int, tuple[float, int | None]] = {}
        self._gift_text_cache: dict[int, tuple[float, str | None]] = {}
        self.stats = {"new": 0, "sent": 0, "filtered_price": 0, "filtered_seller": 0,
                      "filtered_repeat": 0, "duplicates": 0, "failed": 0}
        # защита от повторов NFT / продавца между параллельными обработчиками
        self._repeat_lock = asyncio.Lock()
        self._reserved_gifts: set[int] = set()
        self._reserved_sellers: set[str] = set()
        # «подряд из одной коллекции»
        self._last_sent_gift_id: int | None = None
        self._streak = 0
        self._sending_gift_ids: set[int] = set()
        self.last_new_at: float | None = None      # time.time()
        self.last_sent_at: float | None = None

    # ------------------------------------------------------------------ admission (global dedup)
    async def admit(self, items: list[ResaleItem], session: str | None, mode: str,
                    floor_stars: int | None = None) -> int:
        """items — сверху вниз (новейшие первыми). В очередь кладём от старых к новым."""
        admitted = 0
        for it in reversed(items):
            listing = build_listing(it, session)
            listing.floor_stars = floor_stars
            # Глобальная дедупликация: право на обработку получает только тот, чей INSERT прошёл.
            if not await self.db.insert_listing_if_new(listing):
                self.stats["duplicates"] += 1
                llog("DUPLICATE", f"listing={listing.listing_id} already_known=true")
                continue
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
            await self.queue.put(ListingEvent(listing, time.monotonic(), mode))
        return admitted

    async def resume_unfinished(self) -> int:
        last = await self.db.last_sent_listing()
        if last:
            self._last_sent_gift_id, self._streak = last["gift_id"], 1
        rows = await self.db.unfinished_listings()
        for r in rows:
            await self.queue.put(ListingEvent(listing_from_row(r), time.monotonic(), "resume"))
        if rows:
            log.info("Resuming %s unfinished listing(s) from previous run", len(rows))
        return len(rows)

    # ------------------------------------------------------------------ workers
    def start(self) -> None:
        for i in range(settings.processor_workers):
            self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"processor-{i}"))

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _worker_loop(self, idx: int) -> None:
        while True:
            ev = await self.queue.get()
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
                # access_hash продавца есть только у той session, которая получила лот → строго на ней
                full, _ = await self.sessions.run(lambda c: fetch_full_user(c, uid),
                                                  only=l.session_source, attempts=2, timeout=45)
            except Exception as e:
                log.debug("getFullUser %s failed: %s", uid, e)
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
        l.messages = _messages_status(full.paid_message_stars, full.contact_require_premium)

    async def _nft_count(self, l: Listing) -> int | None:
        uid = l.seller_id
        cached = self._nft_cache.get(uid)
        if cached and time.monotonic() - cached[0] < settings.seller_cache_ttl:
            return cached[1]
        try:
            n, _ = await self.sessions.run(lambda c: fetch_nft_count(c, uid),
                                           only=l.session_source, attempts=2, timeout=45)
        except Exception as e:
            log.debug("getSavedStarGifts %s failed: %s", uid, e)
            n = None
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
            txt, _ = await self.sessions.run(lambda c: fetch_gift_texts(c, uid),
                                             only=l.session_source, attempts=1, timeout=20)
        except Exception as e:
            log.debug("getSavedStarGifts(texts) %s failed: %s", uid, e)
            txt = None
        self._gift_text_cache[uid] = (time.monotonic(), txt)
        if len(self._gift_text_cache) > 50_000:
            self._gift_text_cache.clear()
        return txt

    def _streak_reason(self, l: Listing) -> str | None:
        """Не отправлять одну и ту же коллекцию подряд больше MAX_SAME_COLLECTION_STREAK раз."""
        n = settings.max_same_collection_streak
        if n <= 0:
            return None
        if l.gift_id in self._sending_gift_ids:
            return "same collection is being sent right now"
        if self._last_sent_gift_id == l.gift_id and self._streak >= n:
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
        price_txt = " / ".join(x for x in (
            f"{l.stars_price} Stars" if l.stars_price is not None else "",
            f"{l.ton_price:g} TON" if l.ton_price is not None else "") if x) or "no price"
        llog("PRICE", f"#{l.listing_id} {price_txt}")

        pr = self.price_filter.check(l)
        if not pr.passed:
            l.status, l.reason = ListingStatus.FILTERED_PRICE, pr.reason
            self.stats["filtered_price"] += 1
            llog("FILTER", f"#{l.listing_id} price rejected: {pr.reason}")
            await self.db.update_listing(l)
            return
        llog("FILTER", f"#{l.listing_id} price passed")

        await self._enrich_seller(l)

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
        l.seller_filter_result = sr.code
        if not sr.passed:
            l.status, l.reason = ListingStatus.FILTERED_SELLER, sr.reason
            self.stats["filtered_seller"] += 1
            llog("FILTER", f"#{l.listing_id} seller rejected: {sr.code}")
            await self.db.update_listing(l)
            return
        llog("FILTER", f"#{l.listing_id} seller passed ({sr.code})")

        # повторы: тот же NFT или тот же продавец уже отправлялись → не отправляем
        async with self._repeat_lock:
            repeat = await self._repeat_reason(l)
            if repeat is None:
                repeat = self._streak_reason(l)
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

        text = self.formatter.render(l)
        res = None
        try:
            res = await self.sender.send_listing(text, l.gift_link, listing_keyboard(l),
                                                 listing_keyboard(l, with_seller=False),
                                                 target=settings.route_chat(l.stars_price, l.backdrop))
            if res.ok:
                l.sent = not res.test_only
                l.message_id = res.message_id
                l.status = ListingStatus.TEST_ONLY if res.test_only else ListingStatus.SENT
                if not res.test_only:
                    self.stats["sent"] += 1
                    self.last_sent_at = time.time()
                latency = time.monotonic() - ev.detected_mono
                llog("SEND", f"listing={l.listing_id} target={'test' if res.test_only else 'channel'} "
                             f"latency={latency:.2f}s")
            else:
                l.status, l.reason = ListingStatus.SEND_FAILED, res.error
                self.stats["failed"] += 1
                llog("SEND", f"listing={l.listing_id} FAILED: {res.error}", level=40)
            await self.db.update_listing(l)   # sent=1 записан ДО снятия резерва → нет гонки повторов
        finally:
            async with self._repeat_lock:
                self._reserved_gifts.discard(l.gift_unique_id)
                self._reserved_sellers.discard(l.seller_key)
                self._sending_gift_ids.discard(l.gift_id)
                if res is not None and res.ok and not res.test_only:
                    if self._last_sent_gift_id == l.gift_id:
                        self._streak += 1
                    else:
                        self._last_sent_gift_id, self._streak = l.gift_id, 1
