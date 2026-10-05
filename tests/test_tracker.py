"""
Офлайн-тесты логики трекера (без Telegram). Маркет имитируется так же, как отдаёт Telegram:
лоты коллекции отсортированы по времени последнего изменения цены, новые сверху.

Запуск:  python -m tests.test_tracker
"""
from __future__ import annotations

import os
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
        from types import SimpleNamespace
        return [SimpleNamespace(name=n) for n in self.workers]

    def effective(self):
        return float(len(self.workers))

    async def run(self, fn, **kw):
        return await fn(None), "session_001"


class FakeSender:
    def __init__(self):
        self.sent: list[str] = []

    async def send_listing(self, text, preview_url, markup=None, fallback_markup=None, target=None, thread=None, priority=False):
        self.sent.append(text)
        return SendResult(True, len(self.sent))


class Env:
    def __init__(self, tmp: Path, market: FakeMarket):
        self.tmp, self.market = tmp, market

    async def boot(self, warmup=True):
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
        if warmup:
            # первый live-опрос после initial sync — тихий (закрепляет базу); в проде это делает
            # живой цикл. Тесты стационарного поведения его пропускают через этот прогрев.
            for st in list(self.coord.states.values()):
                if st.baseline_pending:
                    await self.coord.poll_collection(st, "live")
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
    settings.require_free_messages = False
    settings.max_seller_nfts = 0
    settings.max_seller_level = 0
    settings.collection_min_floor = 0
    settings.max_above_floor = 0
    settings.collection_floor_mode = "skip"
    settings.low_priority_poll_interval, settings.low_priority_hot_min = 60.0, 10.0
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
    settings.sync_mode = "full"
    settings.fast_sync_pages = 2
    settings.old_bot_token = ""          # тесты не ходят в настоящий Telegram
    settings.collections = []


def patch_market(market: FakeMarket):
    async def catalog(c):
        floors = getattr(market, "floors", {})
        return [CollectionInfo(g, f"C{g}", len(v), floors.get(g, 100)) for g, v in market.lots.items()]
    coord_mod.fetch_catalog = catalog
    coord_mod.fetch_resale_page = lambda c, g, o, l: market.page(g, o, l)


async def fresh(n_existing=100, warmup=True) -> tuple[Env, Path]:
    reset_settings()
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(n_existing):
        market.list_new(market.item())
    patch_market(market)
    return await Env(tmp, market).boot(warmup=warmup), tmp


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
    e.market.hidden -= 1                              # старый скрытый лот всплыл: общий count не вырос
    await e.poll()
    assert e.pipe.stats["new"] == 0
    await e.shutdown(); shutil.rmtree(tmp)
    # (в) «ложный якорь»: старый лот перевыставили наверх, а настоящий новый — под ним; count вырос → NEW
    e, tmp = await fresh(100)
    old_top = e.market.lots[GID][50]
    fresh_lot = e.market.item()
    e.market.lots[GID].insert(0, fresh_lot)
    e.market.lots[GID].remove(old_top); e.market.lots[GID].insert(0, old_top)   # старый встал выше нового
    await e.poll()
    assert e.pipe.stats["new"] == 1, e.pipe.stats
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
    # первый опрос после ЛЮБОЙ полной синхронизации (в т.ч. ре-синка) — тихий baseline
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert e.pipe.stats["new"] == 0, "baseline после ре-синка — тихий"
    # после базового опроса по-настоящему новый лот уходит
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert e.pipe.stats["new"] == 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 10 anchor не найден → 0 отправлено, RESYNC + тихий baseline, потом LIVE")


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


class FakeBackupBot:
    """Имитация Bot API для резервной копии: send_document + описание бота + скачивание."""
    def __init__(self):
        self.files, self.desc, self.deleted, self.n = {}, {}, [], 0

    async def send_document(self, chat, f, **kw):
        from types import SimpleNamespace as NS
        self.n += 1
        self.files[f"fid{self.n}"] = f.data
        return NS(message_id=self.n, document=NS(file_id=f"fid{self.n}"))

    async def set_my_description(self, description=None, language_code=None):
        self.desc[language_code] = description

    async def get_my_description(self, language_code=None):
        from types import SimpleNamespace as NS
        return NS(description=self.desc.get(language_code, ""))

    async def download(self, file_id, destination):
        destination.write(self.files[file_id])

    async def delete_message(self, chat, mid):
        self.deleted.append(mid)


async def test_13_backup_survives_deploy():
    import os
    from telethon.crypto import AuthKey
    from telethon.sessions import SQLiteSession
    from sessions.backup import SessionBackup
    from sessions.storage import SessionStorage
    reset_settings()
    settings.bot_token, settings.session_backup_key, settings.target_chat_id = "123:abc", "", -100
    bot = FakeBackupBot()
    d1 = Path(tempfile.mkdtemp())
    st1 = SessionStorage(d1, 1, "h" * 32)
    for n in ("session_001", "session_002"):
        x = SQLiteSession(str(d1 / n)); x.set_dc(2, "149.154.167.51", 443)
        x.auth_key = AuthKey(os.urandom(256)); x.save(); x.close()
        st1.save_meta(n, 1, "h" * 32, owner_id=777)
    admins = {777}
    sent = [["1:u9", 1, 5, "u9", "2026-01-01T00:00:00+00:00"]]
    async def exp():
        return sent
    import config as cfg
    cfg.RUNTIME_SETTINGS_PATH = Path(tempfile.mkdtemp()) / "rs.json"
    cfg.set_runtime_value("COLLECTION_MIN_FLOOR", "3000")
    b1 = SessionBackup(bot, st1, lambda: admins, history_export=exp)
    assert not await b1.save(chat_hint=777), "до восстановления копию не перезаписываем"
    assert await b1.restore() == 0 and b1.restore_done
    assert await b1.save(chat_hint=777)
    assert await b1.save(chat_hint=777)          # повторное сохранение удаляет прошлую копию
    assert bot.deleted == [1]
    # «деплой»: пустая папка, пустая база
    d2 = Path(tempfile.mkdtemp())
    st2 = SessionStorage(d2, 1, "h" * 32)
    cfg.RUNTIME_SETTINGS_PATH.unlink()                       # «деплой» стёр настройки
    settings.collection_min_floor = 4500
    got_admins, got_hist = set(), []
    async def imp(rows):
        got_hist.extend(rows); return len(rows)
    b2 = SessionBackup(bot, st2, lambda: set(), got_admins.update, history_import=imp)
    assert await b2.restore() == 2
    recs = st2.list_sessions()
    assert [r.name for r in recs] == ["session_001", "session_002"] and recs[0].owner_id == 777
    assert got_admins == {777} and got_hist == sent
    from config import RUNTIME_SETTINGS_PATH
    assert "COLLECTION_MIN_FLOOR" in RUNTIME_SETTINGS_PATH.read_text() and settings.collection_min_floor == 3000
    # защита: если аккаунтов стало меньше (файл не готов / не восстановился) — старая копия не затирается
    (d2 / "session_002.session").unlink()
    assert await b2.save() and b2.backup_names == {"session_001", "session_002"}, \
        "недоступный аккаунт берётся из прошлой копии, сохранение не блокируется"
    assert await b2.save(allow_shrink=True), "удаление аккаунта — разрешено"
    assert b2.backup_names == {"session_001"}
    # указатель не читается (сбой Telegram) — ничего не затираем
    class Broken(FakeBackupBot):
        async def get_my_description(self, language_code=None):
            raise RuntimeError("telegram down")
    b3 = SessionBackup(Broken(), st2, lambda: set())
    b3.restore_done = True
    assert not await b3.save()
    print("✔ TEST 13 копия переживает деплой; пустая/меньшая копия и сбой Telegram не затирают старую")


async def test_14_ton_rate_changes():
    # у лотов за Stars Telegram дописывает TON-эквивалент, который плавает с курсом
    reset_settings()
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(60):
        it = market.item(9000)
        market.list_new(ResaleItem(**{**it.__dict__, "ton_nano": 95_000_000_000}))
    patch_market(market)
    e = await Env(tmp, market).boot()
    before = e.coord.stats["scanned"]
    e.market.lots[GID] = [ResaleItem(**{**x.__dict__, "ton_nano": 97_300_000_000}) for x in e.market.lots[GID]]
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert e.pipe.stats["new"] == 1 and e.coord.stats["price_moves"] == 0, (e.pipe.stats, e.coord.stats)
    assert e.coord.stats["scanned"] - before <= settings.page_limit, "должен хватить один короткий запрос"
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 14 курс TON изменился → это не смена цены, anchor находится сразу, 1 NEW")


def test_15_seller_rules():
    from database.models import Listing
    from filters.seller_filter import SellerFilter
    reset_settings()
    settings.require_free_messages, settings.max_seller_nfts = True, 12
    settings.russian_only, settings.unknown_seller_policy = True, "block"
    f = SellerFilter(Path(tempfile.mkdtemp()))
    base = dict(listing_id="1:u1", gift_unique_id=1, gift_id=1, gift_name="X", seller_type="user", seller_id=1)
    ok = Listing(**base, messages="бесплатно", seller_nfts=3, seller_about="Продаю подарки, пишите в лс")
    assert f.check(ok).passed
    assert f.check(Listing(**{**base}, messages="платно (50 ⭐)", seller_nfts=3,
                           seller_about="Продаю подарки")).code == "PAID_MESSAGES"
    assert f.check(Listing(**base, messages="только Premium", seller_about="Продаю подарки")).code == "PAID_MESSAGES"
    assert f.check(Listing(**base, messages="бесплатно", seller_nfts=40,
                           seller_about="Продаю подарки")).code == "TOO_MANY_NFTS"
    assert f.check(Listing(**base, messages="бесплатно", seller_nfts=2, seller_about=None,
                           seller_channel="Мой канал про подарки")).code == "CHANNEL_RU"
    assert f.check(Listing(**base, messages="бесплатно", seller_nfts=2)).code == "UNKNOWN_BLOCKED"
    # нет телефона/био/канала, но к подаркам приложены русские тексты → пропустить
    assert f.check(Listing(**base, messages="бесплатно", seller_nfts=2,
                           seller_gift_text="С днём рождения! Люблю тебя")).code == "GIFT_MSG_RU"
    settings.max_seller_level = 5
    assert f.check(Listing(**base, messages="бесплатно", level=5, seller_about="Продаю подарки")).passed
    assert f.check(Listing(**base, messages="бесплатно", level=6,
                           seller_about="Продаю подарки")).code == "LEVEL_TOO_HIGH"
    print("✔ TEST 15 продавец: платные сообщения / >12 NFT отсеяны, русский bio или канал проходят")


async def test_16_market_snapshot_redeploy():
    """Снимок рынка: после «деплоя» (пустая база) трекер сразу в LIVE без полной синхронизации,
    старые лоты не отправляются, новые — отправляются."""
    from sessions.backup import SessionBackup
    from sessions.storage import SessionStorage
    e, tmp = await fresh(300)
    settings.bot_token, settings.session_backup_key, settings.target_chat_id = "123:abc", "", -100
    bot = FakeBackupBot()
    b = SessionBackup(bot, SessionStorage(tmp / "s", 1, "h" * 32), lambda: {1})
    b.market_export = e.db.export_market_snapshot
    assert await b.save_market()
    await e.shutdown()
    # новая пустая база, та же «Telegram»-копия
    tmp2 = Path(tempfile.mkdtemp())
    db2 = Database(tmp2 / "t.db"); await db2.connect()
    b2 = SessionBackup(bot, SessionStorage(tmp2 / "s", 1, "h" * 32), lambda: set())
    b2.market_import = db2.import_market_snapshot
    assert await b2.restore_market() == 300
    await db2.close()
    x = e.market.item(9000)
    e.market.list_new(x)
    e.market.change_price(e.market.lots[GID][100].unique_id, 7777)
    e2 = Env(tmp2, e.market)
    e2.db = Database(tmp2 / "t.db"); await e2.db.connect()
    e2.sender = FakeSender()
    e2.pipe = ListingPipeline(e2.db, FakeSessions(), e2.sender, MessageFormatter(TEMPLATE), PriceFilter(),
                              SellerFilter(tmp2))
    e2.coord = MarketplaceCoordinator(e2.db, FakeSessions(), e2.pipe)
    e2.pipe.start()
    await e2.coord.start()
    for t in e2.coord._tasks:
        t.cancel()
    await e2.pipe.queue.join()
    assert e2.coord.sync_progress() == (1, 1), "коллекция сразу LIVE — без полной синхронизации"
    assert e2.pipe.stats["new"] == 1 and len(e2.sender.sent) == 1
    await e2.shutdown(); shutil.rmtree(tmp); shutil.rmtree(tmp2)
    print("✔ TEST 16 снимок рынка: после деплоя сразу LIVE, найден только новый лот (смена цены — нет)")


async def test_17_hot_cold_schedule():
    e, tmp = await fresh(50)
    settings.cold_poll_interval, settings.tracker_interval = 20.0, 1.0
    import time as _t
    st = e.coord.states[GID]
    e.coord._running.add(GID)
    await e.coord._live_poll(st)
    assert e.coord._due[GID] - _t.monotonic() > 15, "без изменений — страховочный опрос нескоро"
    e.market.list_new(e.market.item(9000))
    await e.coord.refresh_catalog()
    assert e.coord._due[GID] == 0.0, "каталог показал изменение → опрос сразу"
    e.coord._running.add(GID)
    await e.coord._live_poll(st)
    await e.pipe.queue.join()
    assert e.pipe.stats["new"] == 1 and e.coord._due[GID] - _t.monotonic() < 2
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 17 без изменений — опрос раз в 20с; изменился счётчик в каталоге — опрос сразу")


async def test_18_pause():
    e, tmp = await fresh(20)
    await e.coord.set_paused(True)
    before = e.coord.stats["scanned"]
    e.market.list_new(e.market.item(9000))
    task = asyncio.create_task(e.poll())
    await asyncio.sleep(1.5)
    assert not task.done() and e.coord.stats["scanned"] == before, "на паузе запросов нет"
    await e.coord.set_paused(False)
    await asyncio.wait_for(task, 5)
    assert e.pipe.stats["new"] == 1
    assert await e.db.kv_get("paused") == "0"
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 18 /pause — ни одного запроса к Telegram; /resume — продолжает с того же места")


async def test_19_floor_filter():
    reset_settings()
    settings.collection_min_floor = 4500
    settings.collection_floor_mode = "skip"
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    market.lots[2] = []
    market.floors = {GID: 4698, 2: 500}          # Sharp Tongue ~4700⭐ vs коллекция с флором 500⭐
    for _ in range(30):
        market.list_new(market.item())
        market.list_new(market.item(gid=2), gid=2)
    patch_market(market)
    e = await Env(tmp, market).boot()
    assert e.coord.skipped == {2} and e.coord.sync_progress() == (1, 1)
    assert await e.db.count_market_items(2) == 0, "дешёвую коллекцию даже не читаем"
    market.floors[GID] = 4300                    # флор чуть просел (<10%) — продолжаем
    await e.coord.refresh_catalog()
    assert GID not in e.coord.skipped
    market.floors[GID] = 3000                    # сильно упал — выключаем
    await e.coord.refresh_catalog()
    assert GID in e.coord.skipped
    market.floors[2] = 6000                      # дешёвая подорожала — начинаем отслеживать (с тихой синхр.)
    await e.coord.refresh_catalog()
    assert 2 not in e.coord.skipped
    await e.coord.sync_pending()
    assert await e.db.count_market_items(2) == 30 and e.pipe.stats["new"] == 0
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 19 коллекции с флором < 4500⭐ пропускаются; подорожала — тихо подключается")


def test_20_proxies():
    from sessions.storage import SessionStorage
    from utils.proxy import parse_proxy, split_lines
    d = Path(tempfile.mkdtemp())
    os.environ["USE_PROXIES"] = "1"
    st = SessionStorage(d, 1, "h" * 32)
    for n in ("session_001", "session_002", "session_003", "session_004"):
        (d / f"{n}.session").write_bytes(b"")
    pool = split_lines("socks5://u:p@1.1.1.1:1080\n2.2.2.2:1080:user:pw\nмусор")
    assert pool == ["socks5://u:p@1.1.1.1:1080", "socks5://user:pw@2.2.2.2:1080"]
    st.save_pool(pool)
    for n in ("session_001", "session_002", "session_003", "session_004"):
        st.set_proxy(n, st.pick_proxy(2))
    used = [r.proxy for r in st.list_sessions()]
    assert used.count(pool[0]) == 2 and used.count(pool[1]) == 2, used
    assert st.pick_proxy(2) is None, "все прокси заняты по лимиту"
    st.save_meta("session_001", 1, "h" * 32, owner_id=5)       # метаданные не затирают прокси
    assert st.list_sessions()[0].proxy == pool[0] and st.list_sessions()[0].owner_id == 5
    assert parse_proxy(pool[1])["username"] == "user"
    os.environ["USE_PROXIES"] = "0"                            # по умолчанию прокси выключены
    assert st.pick_proxy(2) is None and all(r.proxy is None for r in st.list_sessions())
    assert st.wipe_proxies() == 4 and not st.pool_path.exists()
    os.environ["USE_PROXIES"] = "1"
    assert all(r.proxy is None for r in st.list_sessions())
    print("✔ TEST 20 прокси: разбор форматов, раздача по 2 на прокси, метаданные сохраняются")


async def test_21_ton_priced_lots():
    """Лоты с ценой в TON: Stars-эквивалент плавает с курсом — это не смена цены."""
    reset_settings()
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(40):
        it = market.item(9476)
        market.list_new(ResaleItem(**{**it.__dict__, "ton_nano": 100_000_000_000}))
    patch_market(market)
    e = await Env(tmp, market).boot()
    market.lots[GID] = [ResaleItem(**{**x.__dict__, "stars": x.stars + 3}) for x in market.lots[GID]]
    real = market.lots[GID][10]
    market.change_price(real.unique_id, 12000)
    market.lots[GID][0] = ResaleItem(**{**market.lots[GID][0].__dict__, "ton_nano": 126_000_000_000})
    market.list_new(market.item(9000))
    await e.poll()
    assert e.coord.stats["price_moves"] == 1, e.coord.stats
    assert e.pipe.stats["new"] == 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 21 лот за TON: Stars плавают по курсу — не смена цены; настоящая смена — 1 PRICE_MOVE")


async def test_22_log_regressions():
    """Ошибки из логов прода: shard_owner у пропущенной коллекции, сбои Telegram, дрожание цены."""
    from telethon import errors
    from tracker.state import same_price
    from tracker.worker import SessionWorker, WorkerUnavailable
    from tracker.ratelimit import TokenBucket
    e, tmp = await fresh(10)
    e.coord.skipped.add(GID)
    assert e.coord.shard_owner(GID) in ("session_001", "session_002")   # раньше: ValueError
    e.coord.skipped.discard(GID)
    await e.shutdown(); shutil.rmtree(tmp)
    w = SessionWorker("s", Path("/tmp/x.session"), 1, "h", TokenBucket(0), lambda: 100)
    w.client = object()
    for exc in (errors.RpcMcgetFailError(request=None), ValueError("Request was unsuccessful 3 time(s)")):
        async def boom(c, exc=exc):
            raise exc
        try:
            await w.call(boom)
            raise AssertionError("должно было упасть")
        except WorkerUnavailable:
            pass
    assert same_price(57193, 605_870_000_000, 57187, 605_750_000_000)
    assert not same_price(29238, 310_000_000_000, 28367, 300_000_000_000)
    print("✔ TEST 22 ошибки из логов: пропущенная коллекция, сбой Telegram → повтор другим аккаунтом, дрожание цены")


async def test_23_cheap_collections_slow():
    """Режим slow: дешёвая коллекция тоже отслеживается — дорогой лот в ней находится."""
    import time as _t
    reset_settings()
    settings.collection_min_floor = 4500
    settings.collection_floor_mode = "slow"
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    market.lots[2] = []
    market.floors = {GID: 6000, 2: 500}
    for _ in range(20):
        market.list_new(market.item())
        market.list_new(market.item(gid=2), gid=2)
    patch_market(market)
    e = await Env(tmp, market).boot()
    assert e.coord.skipped == set() and e.coord.low_prio == {2} and e.coord.sync_progress() == (2, 2)
    e.coord._last_poll_mono[2] = _t.monotonic()
    e.coord._due[2] = _t.monotonic() + 60                      # как после страховочного опроса
    market.list_new(market.item(20000, gid=2), gid=2)          # дорогой лот в дешёвой коллекции
    await e.coord.refresh_catalog()
    assert _t.monotonic() + 5 < e.coord._due[2] < _t.monotonic() + 15, "дешёвая: по изменению, но не чаще раза в 10с"
    await e.poll(2)
    assert e.pipe.stats["new"] == 1 and len(e.sender.sent) == 1
    e.coord._running.add(2)
    await e.coord._live_poll(e.coord.states[2])
    e.coord._running.add(2)
    await e.coord._live_poll(e.coord.states[2])
    assert e.coord._due[2] - _t.monotonic() > 50, "страховочный обход дешёвой — раз в 60с"
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 23 дешёвые коллекции (slow): тоже отслеживаются, дорогой лот в них найден, опрос реже")


async def test_24_partial_collection():
    """Telegram отдаёт только часть большой коллекции (400 из 12384). Старый невиданный лот,
    у которого сменили цену, всплывает наверх — count не растёт → НЕ новый. Новый листинг растит count."""
    reset_settings()
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(60):
        market.list_new(market.item())
    market.hidden = 500
    patch_market(market)
    e = await Env(tmp, market).boot()
    st = e.coord.states[GID]
    assert st.complete and st.partial and st.last_count == 560, (st.complete, st.partial, st.last_count)
    market.hidden -= 1                                   # скрытый старый лот сменил цену и всплыл
    market.list_new(market.item(15000))
    await e.poll()
    assert e.pipe.stats["new"] == 0 and not e.sender.sent, "старый лот из невидимой части не должен уйти"
    market.list_new(market.item(12000))                  # настоящий новый листинг: count +1
    await e.poll()
    assert e.pipe.stats["new"] == 1 and len(e.sender.sent) == 1
    market.sell(market.lots[GID][-1].unique_id)          # продажа и новый листинг за один опрос
    market.hidden -= 1
    market.list_new(market.item(13000)); market.list_new(market.item(14000))
    await e.poll()
    assert e.pipe.stats["new"] == 1, "count не объясняет двух незнакомых — ничего не отправляем"
    await e.shutdown()
    e = await Env(tmp, market).boot()                    # partial и last_count переживают рестарт
    assert e.coord.states[GID].partial and e.coord.states[GID].last_count is not None
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 24 неполная коллекция: всплывший старый лот не уходит, новый (count +1) уходит")


async def test_25_post_deploy_baseline():
    """Первый live-опрос сразу после initial sync — тихий: churn рынка за время синхронизации
    не уходит в канал как «новый». Только следующие опросы шлют по-настоящему новые лоты."""
    e, tmp = await fresh(30, warmup=False)
    assert e.coord.states[GID].baseline_pending
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert e.pipe.stats["new"] == 0 and not e.sender.sent, "базовый опрос не шлёт new"
    assert not e.coord.states[GID].baseline_pending
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert e.pipe.stats["new"] == 1 and len(e.sender.sent) == 1, "после базы новый лот уходит"
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 25 после деплоя: первый опрос тихий, затем по-настоящему новые лоты уходят")


async def test_26_floor_is_single_threshold():
    """Один порог: COLLECTION_MIN_FLOOR задаёт и какие коллекции читать, и от какой цены слать лоты."""
    e, tmp = await fresh(30)
    settings.collection_min_floor = 3000
    settings.min_stars = 5000                       # прежний отдельный порог игнорируется
    e.market.list_new(e.market.item(4740))          # был бы отсеян по старому MIN_STARS=5000
    await e.poll()
    assert e.pipe.stats["new"] == 1 and len(e.sender.sent) == 1, (e.pipe.stats, e.sender.sent)
    e.market.list_new(e.market.item(2500))          # ниже флора — отсеивается
    await e.poll()
    assert e.pipe.stats["filtered_price"] == 1, e.pipe.stats
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 26 один порог: флор 3000 → лоты от 3000 идут, ниже — отсеиваются")


async def test_27_reinclude_after_skip_no_burst():
    """Коллекцию, которую какое-то время не читали (была пропущена по порогу), при повторном
    включении нельзя сразу вываливать: сперва тихая ре-синхронизация, только потом новые лоты."""
    import time as _t
    reset_settings()
    settings.collection_min_floor, settings.collection_floor_mode = 3000, "skip"
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    market.floors = {GID: 6000}
    for _ in range(20):
        market.list_new(market.item(9000))
    patch_market(market)
    e = await Env(tmp, market).boot()
    st = e.coord.states[GID]
    assert st.synced and st.complete and GID not in e.coord.skipped
    # флор упал ниже порога → коллекция пропущена
    market.floors[GID] = 500
    await e.coord.refresh_catalog()
    assert GID in e.coord.skipped
    # пока пропущена — на рынок пришли лоты
    for _ in range(5):
        market.list_new(market.item(9000))
    # флор вернулся → коллекция снова включается
    market.floors[GID] = 6000
    await e.coord.refresh_catalog()
    assert GID not in e.coord.skipped and st.needs_full_sync, "повторное включение должно требовать ре-синк"
    assert not st.complete, "повторное включение сбрасывает complete (переживёт рестарт)"
    await e.poll()                                   # тихая полная ре-синхронизация, НИЧЕГО не слать
    assert e.pipe.stats["new"] == 0 and not e.sender.sent, "накопившееся за простой — не новое"
    market.list_new(market.item(9000))               # первый опрос после ре-синка — тихий baseline
    await e.poll()
    assert e.pipe.stats["new"] == 0, "baseline после ре-синка — тихий"
    market.list_new(market.item(9000))               # вот теперь по-настоящему новый
    await e.poll()
    assert e.pipe.stats["new"] == 1 and len(e.sender.sent) == 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 27 повторное включение после пропуска: complete сброшен, тихий ре-синк+baseline, старое не улетает")


def test_29_price_routes():
    """PRICE_ROUTES: лот уходит в канал по диапазону цены; вне диапазонов — в общий канал."""
    import config
    from config import settings as st
    reset_settings()
    st.target_chat_id = -100999
    # темы форума: один и тот же ID группы, разные номера тем после /
    st.price_routes = config._price_routes("250-1000:-100/61,1000-3000:-100/62,3000-5000:-100/63,5000-:-100/64")
    assert st.route_chat(500) == (-100, 61)
    assert st.route_chat(1000) == (-100, 62) and st.route_chat(4999) == (-100, 63)
    assert st.route_chat(5000) == (-100, 64) and st.route_chat(99999) == (-100, 64)
    assert st.route_chat(100) == (-100999, None)    # ниже всех диапазонов → общий
    assert st.route_chat(None) == (-100999, None)
    st.price_routes = []
    assert st.route_chat(7000) == (-100999, None)   # маршрутов нет → общий
    # маршрут по фону (в тему 67) — в приоритете над ценой
    st.price_routes = config._price_routes("5000-:-100/64")
    st.backdrop_routes = config._backdrop_routes("Black:-100/67")
    assert st.route_chat(9000, "Black Velvet") == (-100, 67)   # чёрный фон → своя тема, не 5000+
    assert st.route_chat(9000, "Blue") == (-100, 64)           # не чёрный → по цене
    print("✔ TEST 29 каналы/темы по цене и фону: фон в приоритете, иначе по цене, иначе общий")


def test_28_russian_detection_softened():
    """Смягчённое распознавание русского: латиница/короткий текст не режут русских,
    но украинский/казахский/чистая латиница по-прежнему отсеиваются."""
    from filters.seller_filter import is_russian_text as r
    assert r("NFT gifts на продажу, пишите")        # латиница рядом с кириллицей — ок
    assert r("продам") and r("Мой канал")            # короткие
    assert r("Продаю подарунки, пишіть")             # украинский теперь тоже проходит
    assert not r("for sale, dm me")                  # только латиница
    assert not r("Сатылады, жазыңыз")                # казахский — нет
    assert not r("") and not r(None)
    print("✔ TEST 28 язык: рус/укр/бел проходят, казахский и латиница — нет")


def test_30_hardening():
    """Регрессии из аудита: валидация /set, парсинг прокси, отсев не-славянской кириллицы."""
    import config
    from filters.seller_filter import is_russian_text as r
    from utils.proxy import parse_proxy, split_lines
    # /set-валидация: мусор → ValueError (ловится в боте), а не тихая порча
    for key, bad in [("RUSSIAN_ONLY", "ture"), ("UNKNOWN_SELLER_POLICY", "alow"),
                     ("COLLECTION_FLOOR_MODE", "xxx")]:
        try:
            config.RUNTIME_KEYS[key][1](bad); raise AssertionError(f"{key} принял мусор")
        except ValueError:
            pass
    assert config.RUNTIME_KEYS["MIN_STARS"][1]("-5") == 0          # клампится, не отрицательный
    assert config.RUNTIME_KEYS["RUSSIAN_ONLY"][1]("false") is False
    # backdrop без двоеточия — ошибка, а не молча пусто
    try:
        config._backdrop_routes("Black-100/67"); raise AssertionError("backdrop принял кривой ввод")
    except ValueError:
        pass
    # прокси с нечисловым портом → None и не роняет разбор списка
    assert parse_proxy("http://1.2.3.4:port") is None
    assert len(split_lines("http://1.2.3.4:port\nsocks5://1.2.3.4:1080")) == 1
    # язык: не-славянская кириллица (казах/таджик/серб) не считается русской
    assert not r("Сатылады жазыңыз") and not r("ҳарорати ҷумҳурӣ") and not r("продају")
    assert r("Продаю") and r("Продаю подарунки")                  # рус/укр — да
    print("✔ TEST 30 аудит-регрессии: /set-валидация, прокси-порт, отсев чужой кириллицы")


async def test_31_baseline_survives_restart():
    """Флаг тихого baseline переживает рестарт: если процесс упал после initial sync, но до
    первого тихого опроса, RECOVERY после старта НЕ должен выбросить накопленное как новое."""
    reset_settings()
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(30):
        market.list_new(market.item())
    patch_market(market)
    e = await Env(tmp, market).boot(warmup=False)     # initial sync прошёл, baseline НЕ израсходован
    assert e.coord.states[GID].baseline_pending, "после initial sync baseline_pending=True"
    market.list_new(market.item(9000))                 # лоты появились «за время простоя»
    market.list_new(market.item(9000))
    await e.shutdown()                                 # «падение» до тихого опроса (baseline=1 в БД)
    # рестарт на той же БД: start() запускает RECOVERY по live-коллекциям
    e = await Env(tmp, market).boot(warmup=False)
    await e.pipe.queue.join()
    assert e.pipe.stats["new"] == 0 and not e.sender.sent, \
        "RECOVERY после рестарта НЕ выбрасывает накопленное — baseline пережил рестарт"
    assert not e.coord.states[GID].baseline_pending, "baseline израсходован тихим recovery"
    market.list_new(market.item(9000))                 # теперь по-настоящему новый
    await e.poll()
    assert e.pipe.stats["new"] == 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 31 baseline переживает рестарт: RECOVERY не выбрасывает накопленное как новое")


async def test_32_fast_sync():
    """SYNC_MODE=fast: большая коллекция (1000 лотов, из них 300 Telegram «не отдаёт»)
    становится LIVE за 2 запроса и сразу шлёт новые лоты."""
    reset_settings()
    settings.sync_mode = "fast"
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(1000):
        market.list_new(market.item())
    market.hidden = 300
    patch_market(market)
    calls = {"n": 0}
    orig = coord_mod.fetch_resale_page

    def counting(c, g, o, l):
        calls["n"] += 1
        return orig(c, g, o, l)
    coord_mod.fetch_resale_page = counting
    e = await Env(tmp, market).boot()
    st = e.coord.states[GID]
    assert st.complete and calls["n"] <= 3, f"быстрая база: {calls['n']} запросов"
    assert e.pipe.stats["new"] == 0 and not e.sender.sent, "база тихая"
    market.list_new(market.item(9000))
    market.list_new(market.item(9100))
    await e.poll()
    assert e.pipe.stats["new"] == 2 and len(e.sender.sent) == 2, e.pipe.stats
    # продажа одновременно с новым лотом (count не растёт) — новый всё равно уходит
    market.lots[GID].pop(500)
    market.list_new(market.item(9200))
    await e.poll()
    assert e.pipe.stats["new"] == 3, "продажа не глушит новый лот"
    # смена цены известного лота — не новый
    market.change_price(market.lots[GID][10].unique_id, 15000)
    await e.poll()
    assert e.pipe.stats["new"] == 3
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 32 SYNC_MODE=fast: коллекция LIVE за 2 запроса, новые лоты идут сразу, продажи не глушат")


async def test_33_same_seller_failed_send_not_lost():
    """Два лота одного продавца одновременно: первая отправка сорвалась → второй лот всё равно уходит
    (раньше второй выбрасывался как «продавец отправляется прямо сейчас»)."""
    e, tmp = await fresh(20)
    settings.unique_sellers = True
    fails = {"n": 1}
    orig = e.sender.send_listing

    async def flaky(*a, **kw):
        await asyncio.sleep(0.05)
        if fails["n"]:
            fails["n"] -= 1
            return SendResult(False, error="boom")
        return await orig(*a, **kw)
    e.sender.send_listing = flaky
    e.market.list_new(e.market.item(9000, seller=777))
    e.market.list_new(e.market.item(9000, seller=777))
    await e.poll()
    assert len(e.sender.sent) == 1, (e.pipe.stats, e.sender.sent)
    # третий лот того же продавца — уже повтор (проверка по БД, без запросов в Telegram)
    e.market.list_new(e.market.item(9000, seller=777))
    await e.poll()
    assert len(e.sender.sent) == 1 and e.pipe.stats["filtered_repeat"] >= 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 33 сорвалась отправка — второй лот того же продавца не теряется; повтор режется по БД")


async def test_34_fast_burst_and_false_anchor():
    """Быстрый режим: всплеск больше окна сканирования не теряется; перевыставление по той же цене
    (ложный якорь) при одновременной продаже не глушит новый лот под ним."""
    reset_settings()
    settings.sync_mode, settings.fast_sync_pages = "fast", 1
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(250):
        market.list_new(market.item())
    patch_market(market)
    e = await Env(tmp, market).boot()
    for _ in range(350):
        market.list_new(market.item(9000))
    await e.poll()
    st = e.coord.states[GID]
    assert len(e.sender.sent) == 300 and st.complete and not st.needs_full_sync, (len(e.sender.sent), st.complete)
    market.list_new(market.item(9000))
    await e.poll()
    assert len(e.sender.sent) == 301, "после всплеска коллекция в работе без пересинхронизации"
    # ложный якорь: новый лот, потом старый лот сняли и выставили по той же цене (он сверху), и продажа
    new = market.item(9100)
    market.list_new(new)
    old = market.lots[GID][5]
    market.sell(old.unique_id); market.list_new(old)
    market.sell(market.lots[GID][50].unique_id)
    await e.poll()
    assert len(e.sender.sent) == 302, "новый лот под ложным якорем не потерян"
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 34 быстрый режим: всплеск 350 лотов не теряется, ложный якорь не глушит новый лот")


async def test_35_resold_top_and_false_anchor():
    """Верхний лот перепродан другому продавцу + старый лот перевыставлен по той же цене (ложный якорь)
    → перепроданный лот всё равно уходит (раньше граница «прошлый верх» терялась вместе с ним)."""
    reset_settings()
    settings.sync_mode, settings.fast_sync_pages = "fast", 1
    tmp = Path(tempfile.mkdtemp())
    market = FakeMarket()
    for _ in range(5):
        market.list_new(market.item())
    patch_market(market)
    e = await Env(tmp, market).boot()
    top = market.lots[GID][0]
    market.sell(top.unique_id)
    market.list_new(ResaleItem(**{**top.__dict__, "seller_key": "u900001", "owner_id": 900001, "stars": 54650}))
    third = market.lots[GID][3]
    market.sell(third.unique_id); market.list_new(third)
    await e.poll()
    assert len(e.sender.sent) == 1, len(e.sender.sent)
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 35 перепродажа верхнего лота + ложный якорь: лот не теряется")


async def test_36_muted_collection():
    e, tmp = await fresh(10)
    e.pipe.muted_getter = lambda: {"ice cream"}
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert not e.sender.sent and e.pipe.stats.get("muted") == 1
    e.pipe.muted_getter = lambda: set()
    e.market.list_new(e.market.item(9000))
    await e.poll()
    assert len(e.sender.sent) == 1
    await e.shutdown(); shutil.rmtree(tmp)
    print("✔ TEST 36 /mute: лоты выключенной коллекции не приходят, после /unmute — приходят")


async def run() -> None:
    for t in (test_1_initial_sync, test_2_one_new, test_3_price_change, test_4_moved_down,
              test_5_unknown_old_enters_window, test_6_recovery, test_7_two_sessions_same_listing,
              test_8_seller_change, test_9_burst_500, test_10_no_anchor, test_11_filters_and_repeats,
              test_12_count_mismatch_and_backup_fallback, test_13_backup_survives_deploy,
              test_14_ton_rate_changes, test_16_market_snapshot_redeploy, test_17_hot_cold_schedule,
              test_18_pause, test_19_floor_filter, test_21_ton_priced_lots,
              test_22_log_regressions, test_23_cheap_collections_slow, test_24_partial_collection,
              test_25_post_deploy_baseline, test_26_floor_is_single_threshold,
              test_27_reinclude_after_skip_no_burst,
              test_31_baseline_survives_restart, test_32_fast_sync,
              test_33_same_seller_failed_send_not_lost, test_34_fast_burst_and_false_anchor,
              test_35_resold_top_and_false_anchor, test_36_muted_collection):
        await t()
    test_15_seller_rules()
    test_20_proxies()
    test_28_russian_detection_softened()
    test_29_price_routes()
    test_30_hardening()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run())
