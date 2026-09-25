"""
Офлайн-тесты логики трекера (без Telegram). Маркет имитируется так же, как отдаёт Telegram:
лоты коллекции отсортированы по времени последнего изменения цены, новые сверху.

Запуск:  python -m tests.test_tracker
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

import tracker.coordinator as coord_mod
from bot.formatter import MessageFormatter
from bot.sender import SendResult
from config import settings
from database.db import Database
from filters.price_filter import PriceFilter
from filters.seller_filter import SellerFilter
from tracker.coordinator import MarketplaceCoordinator
from tracker.marketplace import CollectionInfo, ResaleItem, ResalePage
from tracker.queue import ListingPipeline

GID = 1
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "listing.html"


class FakeMarket:
    def __init__(self):
        self.lots: dict[int, list[ResaleItem]] = {GID: []}
        self.uid = 1000
        self.seller = 5000
        self.hidden = 0          # сколько лотов API учитывает в count, но не отдаёт в выдаче

    def item(self, stars=9000, seller=None, gid=GID) -> ResaleItem:
        self.uid += 1
        if seller is None:
            self.seller += 1
            seller = self.seller
        return ResaleItem(unique_id=self.uid, gift_id=gid, title="Ice Cream", slug=f"IceCream-{self.uid}",
                          num=self.uid, model="Gold Ingot", backdrop="Black", symbol="Star",
                          seller_key=f"u{seller}", owner_type="user", owner_id=seller, owner_name=None,
                          owner_address=None, stars=stars, ton_nano=None, ton_only=False)

    def list_new(self, it, gid=GID):
        self.lots[gid].insert(0, it)

    def sell(self, uid, gid=GID):
        self.lots[gid] = [x for x in self.lots[gid] if x.unique_id != uid]

    def change_price(self, uid, stars, gid=GID):
        it = next(x for x in self.lots[gid] if x.unique_id == uid)
        self.sell(uid, gid)
        it = ResaleItem(**{**it.__dict__, "stars": stars})
        self.lots[gid].insert(0, it)
        return it

    async def page(self, gift_id, offset, limit):
        start = int(offset or 0)
        items = self.lots[gift_id][start:start + limit]
        nxt = str(start + limit) if start + limit < len(self.lots[gift_id]) else None
        return ResalePage(items=list(items), next_offset=nxt, count=len(self.lots[gift_id]) + self.hidden)


class FakeSessions:
    def __init__(self):
        self.workers = {"session_001": object(), "session_002": object()}

    def usable(self):
        return [1, 2]

    async def run(self, fn, **kw):
        return await fn(None), "session_001"


class FakeSender:
    def __init__(self):
        self.sent: list[str] = []

    async def send_listing(self, text, preview_url, markup=None, fallback_markup=None):
        self.sent.append(text)
        return SendResult(True, len(self.sent))


class Env:
    def __init__(self, tmp: Path, market: FakeMarket):
        self.tmp, self.market = tmp, market

    async def boot(self):
        self.db = Database(self.tmp / "t.db")
        await self.db.connect()
        self.sender = FakeSender()
        self.pipe = ListingPipeline(self.db, FakeSessions(), self.sender, MessageFormatter(TEMPLATE),
                                    PriceFilter(), SellerFilter(self.tmp))
        self.coord = MarketplaceCoordinator(self.db, FakeSessions(), self.pipe)
        self.pipe.start()
        await self.coord.start()
        for t in self.coord._tasks:           # фоновые петли не нужны — управляем вручную
            t.cancel()
        await self.coord.sync_pending()       # тихая полная синхронизация (в проде — в фоне)
        await self.pipe.queue.join()
        return self

    async def poll(self, gid=GID):
        n = await self.coord.poll_collection(self.coord.states[gid], "live")
        await self.pipe.queue.join()
        return n

    async def shutdown(self):
        await self.coord.stop()
        await self.pipe.stop()
        await self.db.close()


def reset_settings():
    settings.min_stars = 5000
    settings.min_ton = 0
    settings.allow_ton_only = False
    settings.russian_only = False
    settings.fetch_full_seller = False
    settings.test_mode = False
    settings.unique_gifts = False
    settings.unique_sellers = False
    settings.max_same_collection_streak = 0
    settings.page_limit = 30
    settings.live_max_pages = 5
    settings.recovery_max_pages = 20
    settings.initial_sync_pages = 0
    settings.sync_passes = 3
    settings.collections = []


def patch_market(market: FakeMarket):
    async def catalog(c):
        return [CollectionInfo(g, f"C{g}", len(v), 100) for g, v in market.lots.items()]
    coord_mod.fetch_catalog = catalog
    coord_mod.fetch_resale_page = lambda c, g, o, l: market.page(g, o, l)


async def fresh(n_existing=100) -> tuple[Env, Path]:
    reset_settings()
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(n_existing):
        market.list_new(market.item())
    patch_market(market)
    return await Env(tmp, market).boot(), tmp


# ---------------------------------------------------------------------------- tests
async def test_1_initial_sync():
    e, tmp = await fresh(100)
    assert e.pipe.stats["new"] == 0 and e.sender.sent == []
    assert await e.db.count_market_items() == 100
    assert e.coord.states[GID].complete
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 1  initial sync: 100 существующих → 0 NEW, 0 sent, complete=True")


async def test_2_one_new():
    e, tmp = await fresh(100)
    e.market.list_new(e.market.item(7000))
    await e.poll()
    assert e.pipe.stats["new"] == 1 and len(e.sender.sent) == 1
    await e.poll()
    assert e.pipe.stats["new"] == 1, "повторный опрос не должен давать NEW"
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 2  1 новый листинг → 1 NEW, 1 sent; повторный опрос → 0")


async def test_3_price_change():
    e, tmp = await fresh(100)
    old = e.market.lots[GID][50]
    e.market.change_price(old.unique_id, 20000)
    await e.poll()
    assert e.pipe.stats["new"] == 0 and e.sender.sent == []
    assert e.coord.stats["price_moves"] == 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 3  смена цены старого листинга → 0 NEW, 0 sent, PRICE_MOVED=1")


async def test_4_moved_down():
    e, tmp = await fresh(100)
    for idx in (80, 70, 60):                          # 3 смены цены сдвигают остальных вниз
        e.market.change_price(e.market.lots[GID][idx].unique_id, 12345)
    await e.poll()
    assert e.pipe.stats["new"] == 0 and e.coord.stats["price_moves"] == 3
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 4  старые листинги сместились вниз → 0 NEW")


async def test_5_unknown_old_enters_window():
    # (а) коллекция прочитана не полностью (INITIAL_SYNC_PAGES=1 из 2) — именно так раньше
    #     старые лоты «глубже окна» после смены цены принимались за новые
    reset_settings()
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(150):
        market.list_new(market.item())
    patch_market(market)
    settings.initial_sync_pages = 1
    e = await Env(tmp, market).boot()
    assert not e.coord.states[GID].complete
    deep_old = market.lots[GID][140]                  # не попал в синхронизацию
    market.change_price(deep_old.unique_id, 50000)    # поднялся наверх
    await e.poll()
    assert e.pipe.stats["new"] == 0 and e.sender.sent == []
    await e.shutdown(); shutil.rmtree(tmp)
    # (б) неизвестный лот ниже anchor
    e, tmp = await fresh(100)
    ghost = e.market.item()
    e.market.lots[GID].insert(20, ghost)              # «впервые попал в окно» ниже известных
    await e.poll()
    assert e.pipe.stats["new"] == 0
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 5  неизвестный старый листинг впервые попал в окно → 0 NEW (SILENT_OLD)")


async def test_6_recovery():
    e, tmp = await fresh(3)                           # A B C
    await e.shutdown()
    x, y, z = e.market.item(8000), e.market.item(9000), e.market.item(10000)
    for it in (x, y, z):
        e.market.list_new(it)                         # Z Y X A B C
    e.market.change_price(e.market.lots[GID][4].unique_id, 7777)   # и старый поменял цену
    e2 = await Env(tmp, e.market).boot()
    assert e2.pipe.stats["new"] == 3 and len(e2.sender.sent) == 3, e2.pipe.stats
    assert all(f"{it.num}" in "".join(e2.sender.sent) for it in (x, y, z))
    await e2.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 6  recovery: A B C → выкл → X Y Z → только X Y Z (смена цены старого не отправлена)")


async def test_7_two_sessions_same_listing():
    e, tmp = await fresh(100)
    it = e.market.item(9000)
    e.market.list_new(it)
    r = await asyncio.gather(e.pipe.admit([it], "session_001", "live"),
                             e.pipe.admit([it], "session_002", "live"),
                             e.coord.poll_collection(e.coord.states[GID], "live"),
                             e.coord.poll_collection(e.coord.states[GID], "live"))
    await e.pipe.queue.join()
    assert sum(r) == 1 and len(e.sender.sent) == 1, (r, e.sender.sent)
    assert e.pipe.stats["duplicates"] >= 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 7  две sessions увидели один листинг → INSERT один раз, отправка одна")


async def test_8_seller_change():
    e, tmp = await fresh(100)
    old = e.market.lots[GID][10]
    e.market.sell(old.unique_id)
    relist = ResaleItem(**{**old.__dict__, "seller_key": "u999", "owner_id": 999, "stars": 6000})
    e.market.list_new(relist)                         # тот же NFT, другой продавец → новый листинг
    await e.poll()
    assert e.pipe.stats["new"] == 1
    other = e.market.lots[GID][30]                    # тот же NFT, владелец не пришёл в ответе
    e.market.sell(other.unique_id)
    hidden = ResaleItem(**{**other.__dict__, "seller_key": "n", "owner_type": None, "owner_id": None,
                           "stars": 6500})
    e.market.list_new(hidden)
    await e.poll()
    assert e.pipe.stats["new"] == 1, "скрытый владелец ≠ новый листинг"
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 8  другой продавец → новый listing_id; скрытый владелец → тот же листинг (PRICE_MOVED)")


async def test_9_burst_500():
    e, tmp = await fresh(100)
    for _ in range(500):
        e.market.list_new(e.market.item(9000))
    e.market.change_price(e.market.lots[GID][550].unique_id, 11111)
    await e.poll()
    assert e.pipe.stats["new"] == 500 and len(e.sender.sent) == 500, e.pipe.stats
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 9  500 новых между опросами → дочитаны страницы, 500 NEW, старые не отправлены")


async def test_10_no_anchor():
    e, tmp = await fresh(100)
    settings.recovery_max_pages = 2                   # окно API меньше всплеска
    for _ in range(400):
        e.market.list_new(e.market.item(9000))
    logging.disable(logging.WARNING)
    await e.poll()
    logging.disable(logging.NOTSET)
    assert e.pipe.stats["new"] == 0 and e.sender.sent == []
    assert e.coord.stats["resyncs"] == 1 and e.coord.states[GID].needs_full_sync
    await e.poll()                                    # тихая полная ре-синхронизация
    st = e.coord.states[GID]
    assert st.complete and not st.needs_full_sync and e.pipe.stats["new"] == 0
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert e.pipe.stats["new"] == 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 10 anchor не найден → 0 отправлено, RESYNC + WARNING, потом снова LIVE")


async def test_11_filters_and_repeats():
    e, tmp = await fresh(20)
    for stars in (4999, 5000):
        e.market.list_new(e.market.item(stars))
    await e.poll()
    assert e.pipe.stats["filtered_price"] == 1 and len(e.sender.sent) == 1
    settings.max_same_collection_streak = 1
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert len(e.sender.sent) == 1, "та же коллекция подряд не отправляется"
    settings.max_same_collection_streak = 0
    settings.unique_sellers = True
    a = e.market.item(9000, seller=424242)
    b = e.market.item(9000, seller=424242)
    e.market.list_new(a); e.market.list_new(b)
    await e.poll()
    assert len(e.sender.sent) == 2, (e.pipe.stats, len(e.sender.sent))
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 11 MIN_STARS 4999→FILTERED/5000→SENT, одна коллекция подряд и повтор продавца отсеяны")


async def test_12_count_mismatch_and_backup_fallback():
    # count у Telegram больше, чем реально отдаётся → коллекция всё равно становится LIVE
    reset_settings()
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(250):
        market.list_new(market.item())
    market.hidden = 40
    patch_market(market)
    e = await Env(tmp, market).boot()
    assert e.coord.states[GID].complete, "стабильный повторный проход = коллекция прочитана"
    market.list_new(market.item(9000))
    await e.poll()
    assert e.pipe.stats["new"] == 1
    await e.shutdown(); shutil.rmtree(tmp)
    # копия аккаунтов без ADMIN_IDS уходит в канал
    from sessions.backup import SessionBackup
    from sessions.storage import SessionStorage
    settings.sessions_backup_chat_id = 0
    settings.target_chat_id = -100123
    b = SessionBackup(None, SessionStorage(Path(tempfile.mkdtemp()), 1, "h" * 32), lambda: set())
    assert b.candidate_chats() == [-100123]
    print("✔ TEST 12 count API > выдачи → коллекция всё равно LIVE; копия аккаунтов без ADMIN_IDS → канал")


async def run() -> None:
    for t in (test_1_initial_sync, test_2_one_new, test_3_price_change, test_4_moved_down,
              test_5_unknown_old_enters_window, test_6_recovery, test_7_two_sessions_same_listing,
              test_8_seller_change, test_9_burst_500, test_10_no_anchor, test_11_filters_and_repeats,
              test_12_count_mismatch_and_backup_fallback):
        await t()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run())
