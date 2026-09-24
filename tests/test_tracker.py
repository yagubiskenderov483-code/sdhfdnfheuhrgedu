"""
Офлайн-тесты логики трекера (без Telegram): имитация маркетплейса, отсортированного
как у Telegram — по времени последнего изменения цены, новые сверху.

Запуск:  python -m tests.test_tracker
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import tracker.coordinator as coord_mod
from config import settings
from database.db import Database
from filters.price_filter import PriceFilter
from filters.seller_filter import SellerFilter
from tracker.coordinator import MarketplaceCoordinator
from tracker.marketplace import CollectionInfo, ResaleItem, ResalePage
from tracker.queue import ListingPipeline
from bot.formatter import MessageFormatter


class FakeMarket:
    def __init__(self):
        self.lots: dict[int, list[ResaleItem]] = {1: []}   # gift_id → список, новые в начале
        self.uid = 1000

    def item(self, stars, seller=7181202828, ton=None) -> ResaleItem:
        self.uid += 1
        return ResaleItem(unique_id=self.uid, gift_id=1, title="Ice Cream", slug=f"IceCream-{self.uid}",
                          num=self.uid, model="Gold Ingot", backdrop="Black", symbol="Star",
                          seller_key=f"u{seller}", owner_type="user", owner_id=seller, owner_name=None,
                          owner_address=None, stars=stars, ton_nano=ton, ton_only=False)

    def list_new(self, it):          # новый листинг → наверх
        self.lots[1].insert(0, it)

    def sell(self, uid):
        self.lots[1] = [x for x in self.lots[1] if x.unique_id != uid]

    def change_price(self, uid, stars):
        it = next(x for x in self.lots[1] if x.unique_id == uid)
        self.sell(uid)
        it.stars = stars
        self.lots[1].insert(0, it)

    async def page(self, gift_id, offset, limit):
        start = int(offset or 0)
        items = self.lots[gift_id][start:start + limit]
        nxt = str(start + limit) if start + limit < len(self.lots[gift_id]) else None
        return ResalePage(items=list(items), next_offset=nxt, count=len(self.lots[gift_id]))


class FakeSessions:
    def __init__(self):
        self.workers = {"session_001": object()}
        self.limiter = None

    def usable(self):
        return [1]

    async def run(self, fn, **kw):
        return await fn(None), "session_001"


class FakeSender:
    def __init__(self):
        self.sent: list[str] = []

    async def send_listing(self, text, preview_url, markup=None, fallback_markup=None):
        from bot.sender import SendResult
        self.sent.append(text)
        return SendResult(True, len(self.sent))


async def run() -> None:
    tmp = Path(tempfile.mkdtemp())
    try:
        settings.min_stars = 5000
        settings.russian_only = True
        settings.unknown_seller_policy = "allow"
        settings.unique_gifts = False
        settings.unique_sellers = False
        settings.fetch_full_seller = False
        settings.test_mode = False
        settings.page_limit = 3            # маленькая страница, чтобы проверить пагинацию всплеска
        settings.live_max_pages = 5
        settings.initial_sync = True
        settings.collections = []

        market = FakeMarket()
        for _ in range(10):
            market.list_new(market.item(9000))

        coord_mod.fetch_catalog = lambda c: _catalog()
        coord_mod.fetch_resale_page = lambda c, g, o, l: market.page(g, o, l)

        async def _catalog():
            return [CollectionInfo(1, "Ice Cream", len(market.lots[1]), 100)]

        async def boot():
            db = Database(tmp / "t.db")
            await db.connect()
            sender = FakeSender()
            fmt = MessageFormatter(Path(__file__).resolve().parent.parent / "templates" / "listing.html")
            pipe = ListingPipeline(db, FakeSessions(), sender, fmt, PriceFilter(), SellerFilter(tmp))
            coord = MarketplaceCoordinator(db, FakeSessions(), pipe)
            pipe.start()
            await coord.start()
            return db, sender, pipe, coord

        async def poll(coord, pipe):
            await coord.poll_collection(coord.states[1], "live")
            await pipe.queue.join()

        db, sender, pipe, coord = await boot()
        # стоп LIVE-петель — будем опрашивать вручную
        for t in coord._tasks:
            t.cancel()
        await pipe.queue.join()
        assert sender.sent == [], "INITIAL SYNC не должен ничего отправлять"
        print("✔ initial sync: 10 существующих лотов сохранены, 0 отправлено")

        # 4 новых сразу (больше чем PAGE_LIMIT=3 → нужна пагинация)
        a, b, c, d = market.item(1000), market.item(5000), market.item(7000, seller=1), market.item(10000)
        for it in (a, b, c, d):
            market.list_new(it)
        await poll(coord, pipe)
        assert pipe.stats["new"] == 4, pipe.stats
        assert len(sender.sent) == 3, len(sender.sent)          # 1000 Stars отсеян
        assert pipe.stats["filtered_price"] == 1
        print("✔ всплеск 4 лотов > page limit: все 4 найдены, 3 отправлено, 1 FILTERED по цене")

        # повторный опрос — ничего нового
        await poll(coord, pipe)
        assert pipe.stats["new"] == 4
        print("✔ повторный опрос: дубликатов нет")

        # продажи + изменение цены старого лота + новый лот ниже по цене
        market.sell(market.lots[1][0].unique_id)
        old = market.lots[1][-1]
        market.change_price(old.unique_id, 20000)
        e = market.item(6000)
        market.list_new(e)
        await poll(coord, pipe)
        assert pipe.stats["new"] == 5, pipe.stats
        print("✔ смена цены старого лота не считается новым листингом; новый найден")

        # тот же подарок перевыставлен ДРУГИМ продавцом → новый листинг
        market.sell(e.unique_id)
        e2 = ResaleItem(**{**e.__dict__, "seller_key": "u555", "owner_id": 555})
        market.list_new(e2)
        await poll(coord, pipe)
        assert pipe.stats["new"] == 6
        print("✔ перепродажа другим продавцом = новый листинг")

        # глобальная дедупликация: повторный admit того же лота
        n = await pipe.admit([e2], "session_002", "live")
        assert n == 0 and pipe.stats["duplicates"] == 1
        print("✔ global dedup: тот же listing_id со второй session проигнорирован")

        # ----- рестарт: программа выключена, за это время появились 3 лота -----
        await coord.stop()
        await pipe.stop()
        await db.close()
        f1, f2, f3 = market.item(8000), market.item(9000), market.item(100)
        for it in (f1, f2, f3):
            market.list_new(it)
        db, sender2, pipe, coord = await boot()
        for t in coord._tasks:
            t.cancel()
        await pipe.queue.join()
        assert pipe.stats["new"] == 3, pipe.stats
        assert len(sender2.sent) == 2
        print("✔ восстановление после рестарта: 3 пропущенных лота найдены, 2 отправлено, старые не повторены")

        # повторы: тот же продавец / тот же NFT больше не отправляются
        settings.unique_gifts = True
        settings.unique_sellers = True
        before = len(sender2.sent)
        g1 = market.item(9000, seller=424242)
        g2 = market.item(9000, seller=424242)       # тот же продавец → повтор
        market.list_new(g1); market.list_new(g2)
        await poll(coord, pipe)
        assert len(sender2.sent) == before + 1, (before, len(sender2.sent))
        market.sell(g1.unique_id)
        g1b = ResaleItem(**{**g1.__dict__, "seller_key": "u777", "owner_id": 777})  # тот же NFT, другой продавец
        market.list_new(g1b)
        await poll(coord, pipe)
        assert len(sender2.sent) == before + 1
        assert pipe.stats["filtered_repeat"] == 2, pipe.stats
        print("✔ повторы: тот же продавец и тот же NFT второй раз не отправляются")

        print("\n--- пример сообщения ---\n" + sender.sent[0])
        await coord.stop()
        await pipe.stop()
        await db.close()
        print("\nALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run())
