"""
Нагрузочная симуляция трекера (без Telegram): настоящие координатор, очередь, фильтры и менеджер
аккаунтов, а вместо Telegram — модель рынка и аккаунтов с задержкой ответа и FloodWait.

Меряет: сколько новых лотов дошло до канала, за сколько секунд (дорогие / дешёвые отдельно),
сколько было запросов и FloodWait.

Запуск:  PYTHONPATH=. python tests/sim_load.py [секунд] [аккаунтов]
"""
from __future__ import annotations

import asyncio
import logging
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from telethon import errors

import tracker.coordinator as coord_mod
import tracker.queue as queue_mod
from bot.formatter import MessageFormatter
from bot.sender import SendResult
from config import settings
from database.db import Database
from filters.price_filter import PriceFilter
from filters.seller_filter import SellerFilter
from sessions.manager import SessionManager
from tracker.coordinator import MarketplaceCoordinator
from tracker.marketplace import CollectionInfo, ResaleItem, ResalePage, SellerFull
from tracker.queue import ListingPipeline
from tracker.worker import SessionWorker, WorkerStatus

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "listing.html"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 120
ACCOUNTS = int(sys.argv[2]) if len(sys.argv) > 2 else 13
N_COLL = 120
WARMUP = 25.0
EVENTS = float(__import__("os").getenv("SIM_EVENTS", "3"))   # событий рынка в секунду (в проде ~0.5)
random.seed(7)

# ---------------------------------------------------------------- модель Telegram
FLOOD_WINDOW, FLOOD_LIMIT = 10.0, int(__import__("os").getenv("SIM_FLOOD_LIMIT", "11"))  # больше N запросов за 10 с → FloodWait
stats = {"requests": 0, "floods": 0}
CHAOS = __import__("os").getenv("SIM_CHAOS", "0") == "1"   # случайные обрывы/сбои Telegram + 1 аккаунт умирает


class FakeClient:
    def __init__(self, name: str):
        self.name = name
        self.calls: list[float] = []

    dead = False

    def is_connected(self):
        return True

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def __call__(self, req):                 # проверка авторизации при переподключении
        if self.dead:
            raise errors.AuthKeyUnregisteredError(None)
        from types import SimpleNamespace
        return [SimpleNamespace(id=hash(self.name) & 0xffff)]

    async def hit(self) -> None:
        now = time.monotonic()
        self.calls = [t for t in self.calls if now - t < FLOOD_WINDOW] + [now]
        stats["requests"] += 1
        await asyncio.sleep(random.uniform(0.15, 0.6))          # пинг Telegram
        if self.dead:
            raise errors.AuthKeyUnregisteredError(None)
        if CHAOS:
            r = random.random()
            if r < 0.01:
                stats["chaos"] = stats.get("chaos", 0) + 1
                raise ConnectionError("chaos: connection reset")
            if r < 0.02:
                stats["chaos"] = stats.get("chaos", 0) + 1
                raise errors.ServerError(None, "chaos: INTERNAL")
        if len(self.calls) > FLOOD_LIMIT:
            stats["floods"] += 1
            e = errors.FloodWaitError(None, capture=random.randint(5, 25))
            raise e


class Market:
    def __init__(self):
        self.uid = 10_000
        self.seller = 50_000
        self.lots: dict[int, list[ResaleItem]] = {}
        self.floor: dict[int, int] = {}
        self.born: dict[int, float] = {}          # unique_id → когда выставлен (новые лоты)
        for g in range(1, N_COLL + 1):
            self.floor[g] = random.choice([500, 800, 1200, 1800, 2600, 3500, 6000, 12000])
            self.lots[g] = [self.item(g, self.floor[g] + random.randint(0, 3000)) for _ in range(150)]

    def item(self, g: int, stars: int) -> ResaleItem:
        self.uid += 1
        self.seller += 1
        return ResaleItem(unique_id=self.uid, gift_id=g, title=f"C{g}", slug=f"C{g}-{self.uid}", num=self.uid,
                          model="M", backdrop="Blue", symbol="S", seller_key=f"u{self.seller}", owner_type="user",
                          owner_id=self.seller, owner_name=None, owner_address=None, stars=stars,
                          ton_nano=None, ton_only=False)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(random.expovariate(EVENTS))      # событий рынка в секунду
            g = random.randint(1, N_COLL)
            r = random.random()
            lots = self.lots[g]
            if r < 0.25:                                        # новый лот
                it = self.item(g, max(400, int(self.floor[g] * random.uniform(0.9, 1.6))))
                lots.insert(0, it)
                self.born[it.unique_id] = time.monotonic()
            elif r < 0.5 and lots:                              # продажа
                lots.pop(random.randrange(len(lots)))
            elif lots:                                          # смена цены старого лота
                i = random.randrange(len(lots))
                old = lots.pop(i)
                lots.insert(0, ResaleItem(**{**old.__dict__, "stars": max(400, old.stars + random.randint(-200, 200))}))

    def page(self, g: int, offset: str, limit: int) -> ResalePage:
        start = int(offset or 0)
        items = self.lots[g][start:start + limit]
        nxt = str(start + limit) if start + limit < len(self.lots[g]) else None
        return ResalePage(items=list(items), next_offset=nxt, count=len(self.lots[g]))


market = Market()
sent_at: dict[int, float] = {}


async def fake_catalog(client):
    stats["catalog"] = stats.get("catalog", 0) + 1
    await client.hit()
    return [CollectionInfo(g, f"C{g}", len(v), min((x.stars for x in v), default=market.floor[g]))
            for g, v in market.lots.items()]


async def fake_page(client, g, offset, limit):
    await client.hit()
    return market.page(g, offset, limit)


async def fake_full_user(client, uid):
    await client.hit()
    return SellerFull(level=1, premium=False, username=None, paid_message_stars=None,
                      contact_require_premium=None, phone=None, about="Привет, продаю подарки")


async def fake_nft_count(client, uid):
    await client.hit()
    return 2


class Sender:
    async def send_listing(self, text, preview_url, markup=None, fallback_markup=None, target=None, thread=None, priority=False):
        await asyncio.sleep(0.3)
        return SendResult(True, 1)


async def main() -> None:
    logging.disable(logging.WARNING)
    tmp = Path(tempfile.mkdtemp())
    coord_mod.fetch_catalog = fake_catalog
    coord_mod.fetch_resale_page = fake_page
    queue_mod.fetch_full_user = fake_full_user
    queue_mod.fetch_nft_count = fake_nft_count

    async def fake_failover(client, uid, gid):
        await client.hit()                       # чтение рынка, чтобы получить доступ к продавцу
        return await fake_full_user(client, uid)
    queue_mod.fetch_full_user_failover = fake_failover
    settings.sync_mode = "fast"
    settings.collection_min_floor = 400
    settings.unique_sellers = settings.unique_gifts = True
    settings.max_same_collection_streak = 0
    settings.test_mode = False
    settings.old_bot_token = ""

    db = Database(tmp / "sim.db")
    await db.connect()
    storage = type("S", (), {"list_sessions": lambda self: []})()
    sessions = SessionManager(storage)
    for i in range(ACCOUNTS):
        w = SessionWorker(f"s{i:02d}", tmp / f"s{i}.session", 1, "h", sessions.limiter, lambda: settings.session_rps)
        w.client = FakeClient(w.name)
        w.status = WorkerStatus.ACTIVE
        w.start_health_loop()
        sessions.workers[w.name] = w
    pipe = ListingPipeline(db, sessions, Sender(), MessageFormatter(TEMPLATE), PriceFilter(), SellerFilter(tmp))

    orig_update = db.update_listing

    async def track(l):
        if str(getattr(l, "status", "")) == "SENT" and l.gift_unique_id not in sent_at:
            sent_at[l.gift_unique_id] = time.monotonic()
        return await orig_update(l)
    db.update_listing = track

    coord = MarketplaceCoordinator(db, sessions, pipe)
    pipe.start()
    stop = asyncio.Event()
    t0 = time.monotonic()
    mk = asyncio.create_task(market.run(stop))
    await coord.start()

    async def progress():
        while not stop.is_set():
            live, total = coord.sync_progress()
            if "--verbose" in sys.argv:
                print(f"  t={time.monotonic() - t0:5.1f}s live {live}/{total} req={stats['requests']}")
            if live >= total and "--verbose" not in sys.argv:
                print(f"  все коллекции в работе через {time.monotonic() - t0:.0f} с после запуска")
                return
            await asyncio.sleep(2)
    asyncio.create_task(progress())
    MEM = __import__("os").getenv("SIM_MEM", "0") == "1"
    if MEM:
        import tracemalloc
        tracemalloc.start(15)

        async def snap_later():
            await asyncio.sleep(90)
            snaps["base"] = tracemalloc.take_snapshot()
        snaps: dict = {}
        asyncio.create_task(snap_later())
    if CHAOS:
        async def kill():
            await asyncio.sleep(DURATION / 2)
            victim = next(iter(sessions.workers.values()))
            victim.client.dead = True
            print(f"  t={time.monotonic() - t0:.0f}s аккаунт {victim.name} снесён")
        asyncio.create_task(kill())
    await asyncio.sleep(max(0, DURATION - (time.monotonic() - t0)))
    stop.set()
    await asyncio.sleep(15)
    dead_tasks = [t.get_name() for t in coord._tasks + pipe._workers if t.done()]                    # дать дойти тому, что уже в работе
    await coord.stop()
    await pipe.stop()
    mk.cancel()

    born = {u: t for u, t in market.born.items() if t - t0 > WARMUP and t < t0 + DURATION}
    exp, cheap = [], []
    lost_exp = lost_cheap = 0
    lost_ids: list[int] = []
    g_of = {it.unique_id: it.gift_id for v in market.lots.values() for it in v}
    for u, t in born.items():
        g = g_of.get(u)
        is_exp = g is not None and market.floor[g] >= settings.priority_floor
        if u in sent_at:
            (exp if is_exp else cheap).append(sent_at[u] - t)
        elif g is not None:                      # ещё на рынке, но не отправлен (проданные не считаем)
            lost_ids.append(u)
            if is_exp:
                lost_exp += 1
            else:
                lost_cheap += 1

    def q(xs, p):
        return sorted(xs)[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")
    secs = DURATION
    print(f"\n=== {ACCOUNTS} аккаунтов, {N_COLL} коллекций, {secs:.0f} с ===")
    print(f"запросов: {stats['requests']} ({stats['requests'] / (time.monotonic() - t0):.1f}/с), "
          f"FloodWait: {stats['floods']}, каталог: {stats.get('catalog', 0)}, сбоев: {stats.get('chaos', 0)}")
    print("аккаунты в конце: " + ", ".join(f"{k}={v}" for k, v in sessions.counts().items()))
    if dead_tasks:
        print("‼ УПАВШИЕ ЗАДАЧИ:", dead_tasks)
    if MEM and "base" in snaps:
        import tracemalloc
        cur = tracemalloc.take_snapshot()
        print("рост памяти после 90 с (топ-8):")
        for st in cur.compare_to(snaps["base"], "lineno")[:8]:
            print("  ", st)
        sizes = {k: len(v) for k, v in vars(coord).items() if isinstance(v, (dict, set, list))}
        sizes.update({f"pipe.{k}": len(v) for k, v in vars(pipe).items() if isinstance(v, (dict, set, list))})
        print("размеры структур:", {k: v for k, v in sorted(sizes.items(), key=lambda x: -x[1])[:12]})
    for name, xs, lost in (("дорогие (флор ≥ 2500)", exp, lost_exp), ("дешёвые", cheap, lost_cheap)):
        print(f"{name}: отправлено {len(xs)}, не дошло {lost}, задержка p50 {q(xs, .5):.1f} с, "
              f"p90 {q(xs, .9):.1f} с, макс {max(xs) if xs else float('nan'):.1f} с")
    for u in lost_ids[:15]:
        cur = await db.conn.execute("SELECT status, reason FROM listings WHERE gift_unique_id=?", (u,))
        row = await cur.fetchone()
        cur = await db.conn.execute("SELECT state FROM market_items WHERE unique_id=?", (u,))
        mi = await cur.fetchone()
        print(f"  не дошёл {u}: родился {born[u] - t0:.0f}с, listing={tuple(row) if row else None}, "
              f"market={tuple(mi) if mi else None}")
    if pipe.latency:
        print("этапы (среднее): " + " · ".join(f"{k} {v:.2f}s" for k, v in pipe.latency.items()))
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
