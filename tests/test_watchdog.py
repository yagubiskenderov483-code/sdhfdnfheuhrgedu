import asyncio, types, time, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import main
from config import settings
async def run():
    app = main.App.__new__(main.App)
    app._alerts, app._bad_since, app._started_at = {}, {}, time.time() - 4000
    sent = []
    class B:
        async def send_message(s, uid, text): sent.append(text)
    app.bot = B()
    app.booting = False
    app.coordinator = types.SimpleNamespace(paused=False, mode="LIVE")
    ws = {f"s{i}": object() for i in range(10)}
    app.sessions = types.SimpleNamespace(workers=ws, usable=lambda: [])
    app.pipeline = types.SimpleNamespace(last_new_at=None, latency={"total": 3.2})
    settings.admin_ids = {1}
    await app._watchdog()
    assert not sent
    app._bad_since["no_accounts"] -= 200
    await app._watchdog()
    assert any("Нет рабочих" in t for t in sent), sent
    await app._watchdog(); n = len(sent)
    await app._watchdog(); assert len(sent) == n, "не спамит"
    # daily report
    class DB:
        kv = {}
        async def kv_get(s, k, d=None): return s.kv.get(k, d)
        async def kv_set(s, k, v): s.kv[k] = v
        async def counts_since(s, x): return {"SENT": 5, "FILTERED_SELLER": 7}
    app.db = DB()
    app.subs = types.SimpleNamespace(stats=lambda: {"paid_day_count": 2, "paid_day": 3.1, "active": 4})
    app.backup = types.SimpleNamespace(graveyard={})
    import datetime as d
    class FakeDT(d.datetime):
        @classmethod
        def now(cls, tz=None): return d.datetime(2026, 10, 4, 10, 5, tzinfo=tz)
    d.datetime = FakeDT
    await app._daily_report(); await app._daily_report()
    rep = [t for t in sent if "Итоги" in t]
    assert len(rep) == 1, rep
    print(rep[0]); print("WATCHDOG OK")
asyncio.run(run())


async def speeds():
    app = main.App.__new__(main.App)
    app.booting = False
    class DB:
        kv = {}
        async def kv_get(s, k, d=None): return s.kv.get(k, d)
        async def kv_set(s, k, v): s.kv[k] = v
    app.db = DB()
    app.backup = types.SimpleNamespace(flags={})
    w1 = types.SimpleNamespace(name="a", speed=0.3)
    w2 = types.SimpleNamespace(name="b", speed=1.0)
    app.sessions = types.SimpleNamespace(workers={"a": w1, "b": w2})
    await app._save_speeds()
    assert "a" in app.backup.flags["speeds"] and "b" not in app.backup.flags["speeds"]
    w1.speed = 1.0
    await app._restore_speeds()
    assert w1.speed == 0.3 and w2.speed == 1.0
    print("SPEEDS OK")
asyncio.run(speeds())


async def startup():
    app = main.App.__new__(main.App)
    sent = []
    class B:
        async def send_message(s, uid, text): sent.append(text)
    app.bot = B()
    from collections import Counter
    app.sessions = types.SimpleNamespace(workers={"a": 1, "b": 2}, counts=lambda: Counter({"ACTIVE": 2}))
    app.coordinator = types.SimpleNamespace(sync_progress=lambda: (120, 120), paused=False)
    settings.admin_ids = {1}
    orig = asyncio.sleep
    async def fast(t): await orig(0)
    asyncio.sleep = fast
    try:
        await app._startup_report()
    finally:
        asyncio.sleep = orig
    assert sent and "Бот запущен" in sent[0] and "120/120" in sent[0], sent
    print("STARTUP REPORT OK")
asyncio.run(startup())
