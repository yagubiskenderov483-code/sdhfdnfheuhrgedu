import asyncio, time, types, json, sys
sys.path.insert(0, ".")
from config import settings
settings.admin_ids = {1}
settings.target_chat_id = -100
settings.sub_ton_address = "UQtest"
import bot.subscription as S

class DB:
    def __init__(s): s.kv = {}
    async def kv_get(s, k, d=None): return s.kv.get(k, d)
    async def kv_set(s, k, v): s.kv[k] = v

class Link:
    def __init__(s, l): s.invite_link = l
class Bot:
    def __init__(s): s.sent=[]; s.bans=[]; s.unbans=[]; s.revoked=[]; s.n=0; s.members=set()
    async def create_chat_invite_link(s, chat, **kw):
        s.n+=1; return Link(f"https://t.me/+L{s.n}")
    async def revoke_chat_invite_link(s, chat, l): s.revoked.append(l)
    async def send_message(s, uid, text, **kw): s.sent.append((uid, text))
    async def ban_chat_member(s, chat, uid, **kw): s.bans.append(uid)
    async def unban_chat_member(s, chat, uid, **kw): s.unbans.append(uid)
    async def get_chat_member(s, chat, uid):
        return types.SimpleNamespace(status="member" if uid in s.members else "left")

app = types.SimpleNamespace(db=DB(), bot=Bot())
subs = S.Subscriptions(app)
S.asyncio.sleep = (lambda orig: (lambda t: orig(0)))(asyncio.sleep)

def ev(uid, old, new, link):
    u = types.SimpleNamespace(id=uid)
    return types.SimpleNamespace(chat=types.SimpleNamespace(id=-100),
        old_chat_member=types.SimpleNamespace(status=old, user=u),
        new_chat_member=types.SimpleNamespace(status=new, user=u),
        invite_link=Link(link) if link else None)

async def main():
    await subs.load()
    # trial
    subs.user(5)["trial"] = True
    t = await subs.grant(5, S.TRIAL_SECONDS, "trial")
    assert "https://t.me/+L1" in t
    await subs.on_join(ev(5, "left", "member", "https://t.me/+L1"))
    u = subs.user(5)
    assert u["in_chat"] and 3590 < u["until"] - time.time() <= 3600, u
    assert app.bot.revoked == ["https://t.me/+L1"]
    # someone else uses link
    await subs.grant(6, 12*3600, "paid")
    await subs.on_join(ev(7, "left", "member", "https://t.me/+L2"))
    assert 7 in app.bot.bans and 7 in app.bot.unbans
    assert any(uid == 6 and "+L3" in t for uid, t in app.bot.sent), app.bot.sent
    await subs.on_join(ev(6, "left", "member", "https://t.me/+L3"))
    assert 12*3600-10 < subs.user(6)["until"] - time.time() <= 12*3600
    # expiry
    subs.user(5)["until"] = time.time() - 1
    await subs._expire()
    assert app.bot.bans.count(5) == 1 and not subs.user(5)["in_chat"]
    # payments
    code = await subs.new_invoice(9, "forever")
    assert code == await subs.new_invoice(9, "forever")
    assert "amount=15000000000" in subs.invoice_url(code) and code in subs.invoice_url(code)
    async def fake(): return [
        {"transaction_id": {"hash": "h1"}, "in_msg": {"source": "EQx", "value": "1000", "message": code}},
        {"transaction_id": {"hash": "h2"}, "in_msg": {"source": "EQx", "value": "15000000000", "message": " "+code.lower()+" "}}]
    subs._fetch_txs = fake
    assert await subs.check_payments() == 1
    assert await subs.check_payments() == 0
    assert subs.user(9)["pending"] == S.FOREVER
    link = [l for l, r in subs.state["links"].items() if r["uid"] == 9][0]
    await subs.on_join(ev(9, "left", "member", link))
    assert subs.user(9)["until"] == S.FOREVER
    subs.user(9)
    await subs._expire()
    assert 9 not in app.bot.bans
    # extend while in chat
    app.bot.members.add(6)
    before = subs.user(6)["until"]
    t = await subs.grant(6, 3600, "ext")
    assert "продлена" in t and abs(subs.user(6)["until"] - before - 3600) < 2
    # admin never kicked
    subs.user(1).update(in_chat=True, until=time.time()-5)
    await subs._expire(); assert 1 not in app.bot.bans
    # persistence roundtrip
    d = json.loads(app.db.kv[S.KV_KEY]); s2 = S.Subscriptions(types.SimpleNamespace(db=DB(), bot=Bot()))
    await s2.import_(d); assert s2.user(9)["until"] == S.FOREVER
    print("SUB TESTS OK")
asyncio.run(main())

def test_overpriced_filter():
    from filters.price_filter import PriceFilter
    from database.models import Listing
    import dataclasses
    settings.collection_min_floor = 400; settings.max_above_floor = 50
    kw = {x.name: None for x in dataclasses.fields(Listing)
          if x.default is dataclasses.MISSING and x.default_factory is dataclasses.MISSING}
    l = Listing(**kw); l.stars_price = 8000; l.floor_stars = 500
    assert not PriceFilter().check(l).passed
    l.stars_price = 700
    assert PriceFilter().check(l).passed
    print("OVERPRICED FILTER OK")

test_overpriced_filter()

def test_route_by_floor():
    settings.price_routes = [(400, 1000, -1, 4), (1000, 2500, -1, 6), (5000, None, -1, 10)]
    settings.backdrop_routes = []
    # гифт коллекции с флором 500, выставлен за 8000 → тема 400-1000 (по реальной цене)
    assert settings.route_chat(500, None) == (-1, 4)
    assert settings.route_chat(8000, None) == (-1, 10)
    print("ROUTE OK")

test_route_by_floor()

def test_seller_signals():
    import dataclasses, tempfile
    from pathlib import Path
    from filters.seller_filter import SellerFilter
    from database.models import Listing
    settings.russian_only = True; settings.unknown_seller_policy = "block"; settings.require_free_messages = False
    settings.max_seller_level = 0; settings.max_seller_nfts = 0; settings.seller_phone_prefixes = ["7", "375", "380"]
    f = SellerFilter(Path(tempfile.mkdtemp()))
    def chk(**kw):
        base = {x.name: None for x in dataclasses.fields(Listing)
                if x.default is dataclasses.MISSING and x.default_factory is dataclasses.MISSING}
        l = Listing(**base); l.seller_type = "user"
        for k, v in kw.items(): setattr(l, k, v)
        return f.check(l).code
    assert chk(seller_about="nft | vk.com/x") == "RU_MARKER"
    assert chk(seller_about="nft seller") == "UNKNOWN_BLOCKED"
    assert chk(seller_extra_text="Всем привет") == "STORIES_RU"
    assert chk(seller_business="Москва, Тверская 1") == "BUSINESS_RU"
    assert chk(seller_about="من ایران") == "FOREIGN_TEXT"
    assert chk(seller_name="Ivan 🇷🇺") == "FLAG_RU"
    print("SELLER SIGNALS OK")

test_seller_signals()

def test_failover():
    import asyncio, dataclasses, types
    from tracker.queue import ListingPipeline
    from tracker.marketplace import SellerFull
    from tracker.worker import WorkerUnavailable
    from database.models import Listing
    settings.fetch_full_seller = True
    calls = []
    class S:
        async def run(self, fn, only=None, **kw):
            calls.append(only)
            if only == "dead":
                raise WorkerUnavailable("dead")
            return SellerFull(level=2, premium=False, username="x", paid_message_stars=None,
                              contact_require_premium=None, phone=None, about="Привет"), "alive"
    p = ListingPipeline.__new__(ListingPipeline)
    p.sessions = S(); p._seller_cache = {}; p._nft_cache = {}; p._gift_text_cache = {}; p.stats = {}
    base = {x.name: None for x in dataclasses.fields(Listing)
            if x.default is dataclasses.MISSING and x.default_factory is dataclasses.MISSING}
    l = Listing(**base); l.seller_type = "user"; l.seller_id = 5; l.gift_id = 1; l.session_source = "dead"
    asyncio.run(p._enrich_seller(l))
    assert calls == ["dead", None] and l.session_source == "alive" and l.level == 2 and p.stats["failover"] == 1
    print("FAILOVER OK")

test_failover()

def test_session_health():
    from pathlib import Path
    from tracker.worker import SessionWorker
    from tracker.ratelimit import TokenBucket
    w = SessionWorker("s1", Path("/tmp/x.session"), 1, "h", TokenBucket(0), lambda: 1.0)
    assert w.risk() == "ok"
    w._slow_down(10)
    assert w.speed == 0.5 and w.risk() == "warn"
    w._slow_down(120); w._slow_down(120)
    assert w.speed < 0.3 and w.risk() == "high"
    print("SESSION HEALTH OK")

test_session_health()

def test_priority_queue():
    import asyncio, types
    from tracker.queue import ListingPipeline, ListingEvent
    async def run():
        p = ListingPipeline.__new__(ListingPipeline)
        p.queue = asyncio.PriorityQueue(); p._qseq = 0
        mk = lambda name, t, mode: ListingEvent(types.SimpleNamespace(listing_id=name), t, mode)
        await p._enqueue(mk("resume", 1.0, "resume"))
        await p._enqueue(mk("old", 2.0, "live"))
        await p._enqueue(mk("fresh", 3.0, "live"))
        order = [(await p.queue.get())[2].listing.listing_id for _ in range(3)]
        assert order == ["fresh", "old", "resume"], order
    asyncio.run(run())
    print("PRIORITY QUEUE OK")

test_priority_queue()

def test_payment_delivery():
    import asyncio, types
    import bot.subscription as S
    class DB:
        def __init__(s): s.kv = {}
        async def kv_get(s, k, d=None): return s.kv.get(k, d)
        async def kv_set(s, k, v): s.kv[k] = v
    class Bot:
        fail = True; n = 0
        async def create_chat_invite_link(s, chat, **kw):
            if s.fail: raise RuntimeError("no rights")
            s.n += 1; return types.SimpleNamespace(invite_link=f"L{s.n}")
        async def revoke_chat_invite_link(s, *a): pass
        async def send_message(s, *a, **k): pass
        async def get_chat_member(s, chat, uid): return types.SimpleNamespace(status="left")
    settings.target_chat_id = -100
    subs = S.Subscriptions(types.SimpleNamespace(db=DB(), bot=Bot()))
    async def run():
        await subs.load()
        inv = {"uid": 9, "plan": "24h", "nano": 1, "ts": 0, "paid": True, "granted": "inflight"}
        subs.state["invoices"]["X"] = inv
        assert not await subs._deliver(inv) and inv["granted"] is False
        assert not subs.user(9).get("pending")            # неудача не накручивает время
        await subs._retry_undelivered(); await subs._retry_undelivered()
        assert not subs.user(9).get("pending")
        subs.bot.fail = False
        await subs._retry_undelivered()
        assert inv["granted"] is True and subs.user(9)["pending"] == 24 * 3600
        await subs._retry_undelivered()                   # повторно не выдаёт
        assert subs.user(9)["pending"] == 24 * 3600
        subs.state["invoices"]["OLD"] = {"uid": 5, "plan": "24h", "nano": 1, "ts": 0, "paid": True}
        await subs._retry_undelivered()                   # старые счета без флага не трогаем
        assert not subs.user(5).get("pending")
    asyncio.run(run())
    print("PAYMENT DELIVERY OK")

test_payment_delivery()

def test_remind_and_stats():
    import asyncio, types, time as _t
    import bot.subscription as S
    class DB:
        def __init__(s): s.kv = {}
        async def kv_get(s, k, d=None): return s.kv.get(k, d)
        async def kv_set(s, k, v): s.kv[k] = v
    sent = []
    class Bot:
        async def send_message(s, uid, text, **k): sent.append(uid)
    subs = S.Subscriptions(types.SimpleNamespace(db=DB(), bot=Bot()))
    async def run():
        await subs.load()
        now = _t.time()
        subs.user(1).update(in_chat=True, until=now + 1800, started=now - 86400)   # сутки, осталось 30 мин
        subs.user(2).update(in_chat=True, until=now + 1800, started=now - 1800)    # пробный час — не напоминаем
        await subs._remind(); await subs._remind()
        assert sent == [1], sent
        subs.state["revenue_nano"], subs.state["paid_count"] = 3_000_000_000, 2
        st = subs.stats()
        assert st["paid_total"] == 3 and st["paid_count"] == 2 and st["active"] == 2
    asyncio.run(run())
    print("REMIND + STATS OK")

test_remind_and_stats()

def test_cheaters():
    import asyncio, types, time as _t
    import bot.subscription as S
    settings.admin_ids = {1}; settings.target_chat_id = -100; settings.sub_chat_id = 0
    class DB:
        def __init__(s): s.kv = {}
        async def kv_get(s, k, d=None): return s.kv.get(k, d)
        async def kv_set(s, k, v): s.kv[k] = v
    kicked, members = [], {77, 88}
    class Bot:
        async def ban_chat_member(s, chat, uid, **k): kicked.append(uid)
        async def unban_chat_member(s, *a, **k): pass
        async def send_message(s, *a, **k): pass
        async def revoke_chat_invite_link(s, *a): pass
        async def get_chat_member(s, chat, uid):
            return types.SimpleNamespace(status="member" if uid in members else "left")
    subs = S.Subscriptions(types.SimpleNamespace(db=DB(), bot=Bot()))
    S.asyncio.sleep = (lambda orig: (lambda t: orig(0)))(asyncio.sleep)
    def ev(uid, link=None, adder=None, uname="cheat"):
        u = types.SimpleNamespace(id=uid, username=uname, first_name="X", last_name=None, is_bot=False)
        return types.SimpleNamespace(chat=types.SimpleNamespace(id=-100), from_user=types.SimpleNamespace(id=adder or uid),
            old_chat_member=types.SimpleNamespace(status="left", user=u),
            new_chat_member=types.SimpleNamespace(status="member", user=u),
            invite_link=types.SimpleNamespace(invite_link=link) if link else None)
    async def run():
        await subs.load()
        await subs.on_join(ev(50))                       # зашёл по общей ссылке без подписки → кик
        assert kicked == [50]
        await subs.on_join(ev(51, adder=1))              # админ добавил вручную → не трогаем
        assert kicked == [50]
        subs.user(52).update(until=_t.time() + 3600)     # есть оплаченное время → пускаем
        await subs.on_join(ev(52))
        assert kicked == [50] and subs.user(52)["in_chat"] and subs.user(52)["username"] == "cheat"
        # событие выхода/кика потерялось: срок кончился, а человек всё ещё в канале → выкидываем
        subs.user(77).update(until=_t.time() - 60, in_chat=False)
        subs.user(88).update(until=S.FOREVER, in_chat=True)
        await subs._reconcile()
        assert 77 in kicked and 88 not in kicked
    asyncio.run(run())
    print("CHEATERS OK")

test_cheaters()


def test_expensive_first():
    import asyncio, types
    from tracker.queue import ListingPipeline, ListingEvent
    settings.priority_floor = 2500
    async def run():
        p = ListingPipeline.__new__(ListingPipeline)
        p.queue = asyncio.PriorityQueue(); p._qseq = 0
        mk = lambda n, fl, t: ListingEvent(types.SimpleNamespace(listing_id=n, floor_stars=fl, stars_price=fl), t, "live")
        await p._enqueue(mk("cheap_new", 500, 5.0))
        await p._enqueue(mk("rich_old", 3000, 1.0))
        order = [(await p.queue.get())[2].listing.listing_id for _ in range(2)]
        assert order == ["rich_old", "cheap_new"], order
    asyncio.run(run())
    print("EXPENSIVE FIRST OK")

test_expensive_first()


def test_connect_does_not_kill_flooded_account():
    """FloodWait / сбой Telegram при подключении НЕ должны помечать аккаунт «снесённым» (раньше
    client.is_user_authorized() превращал любую ошибку в «не авторизован» → аккаунт удалялся)."""
    import asyncio
    from pathlib import Path
    from telethon import errors
    from tracker.worker import SessionWorker, WorkerStatus
    from tracker.ratelimit import TokenBucket

    class FakeClient:
        def __init__(self, exc):
            self.exc = exc
        def is_connected(self): return True
        async def connect(self): pass
        async def disconnect(self): pass
        async def __call__(self, req):
            if self.exc:
                raise self.exc
            from types import SimpleNamespace
            return [SimpleNamespace(id=42)]

    async def run():
        res = {}
        for name, exc in (("flood", errors.FloodWaitError(None, capture=600)),
                          ("server", errors.ServerError(None, "INTERNAL")),
                          ("dead", errors.AuthKeyUnregisteredError(None)),
                          ("ok", None)):
            w = SessionWorker(name, Path("/tmp/x.session"), 1, "h", TokenBucket(0), lambda: 1.0)
            killed = []
            async def cb(w, killed=killed): killed.append(w.name)
            w.on_auth_error = cb
            w.client = FakeClient(exc)
            await w.connect()
            await asyncio.sleep(0)
            res[name] = (w.status, killed)
        assert res["flood"][0] == WorkerStatus.ERROR and not res["flood"][1], res["flood"]
        assert res["server"][0] == WorkerStatus.ERROR and not res["server"][1]
        assert res["dead"][0] == WorkerStatus.AUTH_ERROR and res["dead"][1] == ["dead"]
        assert res["ok"][0] == WorkerStatus.ACTIVE
    asyncio.run(run())
    print("CONNECT SAFE OK")

test_connect_does_not_kill_flooded_account()


def test_graveyard():
    import tempfile, sqlite3, time as _t
    from pathlib import Path
    from sessions.storage import SessionStorage
    from sessions.backup import SessionBackup
    from telethon.sessions import SQLiteSession
    from telethon.crypto import AuthKey
    d = Path(tempfile.mkdtemp())
    st = SessionStorage(d, 1, "h")
    ss = SQLiteSession(str(d / "session_001"))
    ss.set_dc(2, "149.154.167.51", 443); ss.auth_key = AuthKey(b"\x01" * 256); ss.save(); ss.close()
    st.save_meta("session_001", 123, "hash", owner_id=7)
    b = SessionBackup.__new__(SessionBackup)
    b.storage = st; b.graveyard = {}; b._prev_items = {}
    assert b.bury("session_001", "test")
    st.remove("session_001")
    assert not st.session_path("session_001").exists()
    b.graveyard["old"] = {"name": "old", "string": "x", "ts": _t.time() - 30 * 86400}
    b._trim_graveyard()
    assert "old" not in b.graveyard and "session_001" in b.graveyard
    name = b.unbury("session_001")
    assert name == "session_001" and st.session_path(name).exists()
    rec = st.list_sessions()[0]
    assert rec.api_id == 123 and rec.owner_id == 7 and not b.graveyard
    print("GRAVEYARD OK")

test_graveyard()


def test_split_payment_and_inflight_restore():
    import asyncio, types, time as _t
    import bot.subscription as S
    class DB:
        def __init__(s): s.kv = {}
        async def kv_get(s, k, d=None): return s.kv.get(k, d)
        async def kv_set(s, k, v): s.kv[k] = v
    class Bot:
        n = 0; msgs = []
        async def create_chat_invite_link(s, chat, **kw):
            s.n += 1; return types.SimpleNamespace(invite_link=f"L{s.n}")
        async def revoke_chat_invite_link(s, *a): pass
        async def send_message(s, uid, text, **k): s.msgs.append((uid, text))
        async def get_chat_member(s, chat, uid): return types.SimpleNamespace(status="left")
    settings.target_chat_id = -100
    settings.sub_ton_address = "UQx"
    subs = S.Subscriptions(types.SimpleNamespace(db=DB(), bot=Bot()))
    async def run():
        await subs.load()
        subs.state["invoices"]["AB"] = {"uid": 3, "plan": "24h", "nano": 2_600_000_000, "ts": _t.time()}
        txs = [{"transaction_id": {"hash": "p1"}, "in_msg": {"source": "EQ", "value": "1600000000", "message": "AB"}}]
        async def fake(): return txs
        subs._fetch_txs = fake
        assert await subs.check_payments() == 0
        assert any(u == 3 and "Доплатите" in t for u, t in subs.bot.msgs), "человеку сказали про доплату"
        txs.append({"transaction_id": {"hash": "p2"}, "in_msg": {"source": "EQ", "value": "1000000000", "message": "AB"}})
        assert await subs.check_payments() == 1
        assert subs.state["invoices"]["AB"]["granted"] is True and subs.user(3)["pending"] == 24 * 3600
        # выдача оборвалась перезапуском («inflight» в копии) → после восстановления повторяется
        subs2 = S.Subscriptions(types.SimpleNamespace(db=DB(), bot=Bot()))
        await subs2.import_({"invoices": {"Z": {"uid": 4, "plan": "24h", "nano": 1, "ts": 0,
                                                "paid": True, "granted": "inflight"}}})
        assert subs2.state["invoices"]["Z"]["granted"] is False
        await subs2._retry_undelivered()
        assert subs2.state["invoices"]["Z"]["granted"] is True
    asyncio.run(run())
    print("SPLIT PAYMENT + INFLIGHT OK")

test_split_payment_and_inflight_restore()


def test_ton_only_filter():
    import dataclasses
    from filters.price_filter import PriceFilter
    from database.models import Listing
    from bot.formatter import price_line
    base = {x.name: None for x in dataclasses.fields(Listing)
            if x.default is dataclasses.MISSING and x.default_factory is dataclasses.MISSING}
    l = Listing(**base); l.stars_price = 9000; l.ton_nano = 100 * 10**9; l.ton_only = True; l.floor_stars = 5000
    settings.collection_min_floor = 400; settings.max_above_floor = 0; settings.min_ton = 0
    settings.allow_ton_only = False
    assert not PriceFilter().check(l).passed
    settings.allow_ton_only = True
    assert PriceFilter().check(l).passed
    assert "только TON" in price_line(l) and "⭐" not in price_line(l)
    settings.max_above_floor = 50
    assert not PriceFilter().check(l).passed, "TON-only тоже под лимитом «дороже флора»"
    settings.max_above_floor = 0
    print("TON-ONLY OK")

test_ton_only_filter()


def test_old_inflight_invoice():
    import asyncio, types, time as _t
    import bot.subscription as S
    class DB:
        def __init__(s): s.kv = {}
        async def kv_get(s, k, d=None): return s.kv.get(k, d)
        async def kv_set(s, k, v): s.kv[k] = v
    subs = S.Subscriptions(types.SimpleNamespace(db=DB(), bot=None))
    async def run():
        now = _t.time()
        await subs.import_({
            "users": {"1": {"until": now + 86400, "in_chat": True, "pending": 0},
                      "2": {"until": 0, "in_chat": False, "pending": 0}},
            "invoices": {"A": {"uid": 1, "plan": "24h", "nano": 1, "ts": now - 60, "paid": True, "granted": "inflight"},
                         "B": {"uid": 2, "plan": "24h", "nano": 1, "ts": now - 60, "paid": True, "granted": "inflight"}}})
        assert subs.state["invoices"]["A"]["granted"] is True, "старая версия уже выдала время — второй раз не выдаём"
        assert subs.state["invoices"]["B"]["granted"] is False, "время не выдано — выдадим"
    asyncio.run(run())
    print("OLD INFLIGHT OK")

test_old_inflight_invoice()


def test_sender_fallbacks():
    import asyncio, types
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage
    from bot.sender import TelegramSender
    calls = []
    class Bot:
        async def send_message(s, chat, text, **kw):
            calls.append(("send", kw.get("message_thread_id"), getattr(kw.get("link_preview_options"), "is_disabled", None)))
            lp = kw.get("link_preview_options")
            if lp is not None and lp.is_disabled is not True:
                raise TelegramBadRequest(method=SendMessage(chat_id=chat, text=text), message="WEBPAGE_CURL_FAILED")
            if kw.get("message_thread_id") == 67:
                raise TelegramBadRequest(method=SendMessage(chat_id=chat, text=text), message="TOPIC_CLOSED")
            return types.SimpleNamespace(message_id=5)
        async def reopen_forum_topic(s, chat, thread):
            calls.append(("reopen", thread)); raise RuntimeError("no rights")
        async def close_forum_topic(s, chat, thread):
            calls.append(("close", thread))
    settings.test_mode = False; settings.send_interval = 0
    snd = TelegramSender(Bot())
    async def run():
        r = await snd.send_listing("x", "https://t.me/nft/A-1", target=-100, thread=67)
        assert r.ok, r
        assert ("close", 67) not in calls, "тему, которую не открывали, не закрываем"
    asyncio.run(run())
    print("SENDER FALLBACKS OK")

test_sender_fallbacks()


def test_sender_priority_and_rate():
    import asyncio, types, time as _t
    from bot.sender import TelegramSender, _PriorityLock
    async def run():
        lk = _PriorityLock(); order = []
        await lk.acquire(1)
        async def w(name, prio):
            await lk.acquire(prio); order.append(name); await asyncio.sleep(0); lk.release()
        ts = [asyncio.create_task(w("cheap1", 1)), asyncio.create_task(w("cheap2", 1)),
              asyncio.create_task(w("rich", 0))]
        await asyncio.sleep(0.01); lk.release(); await asyncio.gather(*ts)
        assert order == ["rich", "cheap1", "cheap2"], order
        # не больше PER_CHAT_MINUTE сообщений в минуту в одну группу
        sent = []
        class Bot:
            async def send_message(s, chat, text, **kw):
                sent.append(_t.monotonic()); return types.SimpleNamespace(message_id=1)
        settings.test_mode = False; settings.send_interval = 0
        snd = TelegramSender(Bot()); snd.PER_CHAT_MINUTE = 3
        t0 = _t.monotonic()
        snd._recent[-1] = __import__("collections").deque([t0 - 59.8, t0 - 59.8, t0 - 59.8])
        waits = []
        orig_sleep = asyncio.sleep
        async def rec(t, *a):
            waits.append(t); await orig_sleep(0)
        asyncio.sleep = rec
        try:
            await snd.send(-1, "x")
        finally:
            asyncio.sleep = orig_sleep
        assert waits and 0.1 < waits[0] <= 0.2, ("ждёт, пока освободится окно минуты", waits)
    asyncio.run(run())
    print("SENDER PRIORITY + RATE OK")

test_sender_priority_and_rate()


def test_hidden_owner_resolved():
    import asyncio, dataclasses, types
    from tracker.queue import ListingPipeline
    from database.models import Listing
    class S:
        async def run(self, fn, **kw):
            return types.SimpleNamespace(id=4242, username=None, usernames=[types.SimpleNamespace(active=True, username="coll")],
                                         first_name="Петя", last_name=None, premium=True, phone=None, min=False,
                                         send_paid_messages_stars=None, contact_require_premium=False), "s07"
    p = ListingPipeline.__new__(ListingPipeline); p.sessions = S(); p.stats = {}
    base = {x.name: None for x in dataclasses.fields(Listing)
            if x.default is dataclasses.MISSING and x.default_factory is dataclasses.MISSING}
    l = Listing(**base); l.listing_id = "1:n:Петя"; l.seller_key = "n:Петя"; l.seller_name = "Петя"; l.gift_slug = "X-1"
    asyncio.run(p._resolve_hidden_owner(l))
    assert (l.seller_type, l.seller_id, l.seller_key, l.seller_username, l.session_source, l.messages) == \
        ("user", 4242, "u4242", "coll", "s07", "бесплатно"), l
    print("HIDDEN OWNER OK")

test_hidden_owner_resolved()


def test_backup_migrates_from_old_bot_once():
    import asyncio, json, tempfile
    from pathlib import Path
    from types import SimpleNamespace as NS
    import sessions.backup as B
    from sessions.storage import SessionStorage
    f = B._fernet()
    fresh = f.encrypt(json.dumps({"v": 2, "sessions": [], "admins": [], "sent": [], "flags": {"who": "old-fresh"}}).encode())
    stale = f.encrypt(json.dumps({"v": 2, "sessions": [], "admins": [], "sent": [], "flags": {"who": "new-stale"}}).encode())
    class FB:
        def __init__(s, ptr, blob): s.ptr, s.blob, s.desc = ptr, blob, None
        async def get_my_description(s, language_code=None):
            return NS(description=s.desc or (B.POINTER_PREFIX2 + json.dumps(s.ptr)))
        async def set_my_description(s, description=None, language_code=None): s.desc = description
        async def download(s, fid, destination): destination.write(s.blob)
        @property
        def session(s):
            async def close(): pass
            return NS(close=close)
    old = FB({"s": ["oldfid", 1, 2]}, fresh)
    new = FB({"s": ["newfid", 1, 3]}, stale)
    orig = B.Bot
    B.Bot = lambda token: old
    settings.old_bot_token, settings.bot_token = "111:aaa", "222:bbb"
    try:
        st = SessionStorage(Path(tempfile.mkdtemp()), 1, "h")
        b = B.SessionBackup(new, st, lambda: set())
        asyncio.run(b.restore())
        assert b.flags.get("who") == "old-fresh", b.flags
        assert b._ptr.get("mig") == "111" and "s" not in b._ptr
        # после переноса указатель нового бота с отметкой mig → следующий запуск читает СВОЮ копию
        new.ptr = {"s": ["newfid2", 1, 4], "mig": "111"}
        new.blob = f.encrypt(json.dumps({"v": 2, "sessions": [], "admins": [], "sent": [], "flags": {"who": "new-after"}}).encode())
        b2 = B.SessionBackup(new, SessionStorage(Path(tempfile.mkdtemp()), 1, "h"), lambda: set())
        asyncio.run(b2.restore())
        assert b2.flags.get("who") == "new-after", b2.flags
    finally:
        B.Bot = orig
        settings.old_bot_token = ""
    print("BOT MIGRATION OK")

test_backup_migrates_from_old_bot_once()


def test_backup_migration_chain():
    import asyncio, json, tempfile
    from pathlib import Path
    from types import SimpleNamespace as NS
    import sessions.backup as B
    from sessions.storage import SessionStorage
    f = B._fernet()
    blob = lambda who: f.encrypt(json.dumps({"v": 2, "sessions": [], "admins": [], "sent": [], "flags": {"who": who}}).encode())
    class FB:
        def __init__(s, ptr, b): s.ptr, s.blob, s.desc = ptr, b, None
        async def get_my_description(s, language_code=None):
            return NS(description=s.desc or (B.POINTER_PREFIX2 + json.dumps(s.ptr)))
        async def set_my_description(s, description=None, language_code=None): s.desc = description
        async def download(s, fid, destination): destination.write(s.blob)
        @property
        def session(s):
            async def close(): pass
            return NS(close=close)
    orig = B.Bot
    settings.bot_token = "333:ccc"
    settings.old_bot_token = "222:bbb,111:aaa"
    try:
        # 1) средний бот уже забрал копию у старшего (mig=111) → берём у среднего
        bots = {"222:bbb": FB({"s": ["m", 1, 2], "mig": "111"}, blob("mid")), "111:aaa": FB({"s": ["o", 1, 2]}, blob("oldest"))}
        B.Bot = lambda t: bots[t]
        b = B.SessionBackup(FB({}, b""), SessionStorage(Path(tempfile.mkdtemp()), 1, "h"), lambda: set())
        asyncio.run(b.restore())
        assert b.flags.get("who") == "mid" and b._ptr.get("mig") == "222", (b.flags, b._ptr)
        # 2) средний бот так и не задеплоили (своя старая копия без mig) → берём у самого старого
        bots = {"222:bbb": FB({"s": ["m", 1, 2]}, blob("mid-stale")), "111:aaa": FB({"s": ["o", 1, 2]}, blob("oldest"))}
        b = B.SessionBackup(FB({}, b""), SessionStorage(Path(tempfile.mkdtemp()), 1, "h"), lambda: set())
        asyncio.run(b.restore())
        assert b.flags.get("who") == "oldest" and b._ptr.get("mig") == "111", (b.flags, b._ptr)
        # 3) уже перенесли (mig = любой из списка) → свою копию, к прошлым ботам не ходим
        def boom(t): raise AssertionError("must not touch old bots")
        B.Bot = boom
        b = B.SessionBackup(FB({"s": ["own", 1, 2], "mig": "111"}, blob("own")), SessionStorage(Path(tempfile.mkdtemp()), 1, "h"), lambda: set())
        asyncio.run(b.restore())
        assert b.flags.get("who") == "own", b.flags
    finally:
        B.Bot = orig
        settings.old_bot_token = ""
    print("BOT MIGRATION CHAIN OK")

test_backup_migration_chain()


def test_sub_loopholes():
    import asyncio, types, time as _t
    import bot.subscription as S
    class DB:
        def __init__(s): s.kv = {}
        async def kv_get(s, k, d=None): return s.kv.get(k, d)
        async def kv_set(s, k, v): s.kv[k] = v
    class Bot:
        def __init__(s): s.bans, s.links, s.st = [], 0, {}
        async def create_chat_invite_link(s, chat, **kw):
            s.links += 1; return types.SimpleNamespace(invite_link=f"L{s.links}")
        async def revoke_chat_invite_link(s, *a): pass
        async def send_message(s, *a, **k): pass
        async def ban_chat_member(s, chat, uid, **kw): s.bans.append(uid)
        async def unban_chat_member(s, *a, **k): pass
        async def get_chat_member(s, chat, uid):
            st, mem = s.st.get(uid, ("left", False))
            return types.SimpleNamespace(status=st, is_member=mem)
    settings.target_chat_id = -100
    bot = Bot()
    subs = S.Subscriptions(types.SimpleNamespace(db=DB(), bot=bot))
    def ev(uid, old, new, mem=True):
        u = types.SimpleNamespace(id=uid, username=None, first_name="x")
        return types.SimpleNamespace(chat=types.SimpleNamespace(id=-100), invite_link=None, from_user=u,
            old_chat_member=types.SimpleNamespace(status=old, user=u, is_member=False),
            new_chat_member=types.SimpleNamespace(status=new, user=u, is_member=mem))
    async def run():
        await subs.load()
        subs.ready.set()
        # 1) отскочивший перевод не считается оплатой
        code = await subs.new_invoice(50, "24h")
        nano = subs.state["invoices"][code]["nano"]
        async def txs():
            return [{"transaction_id": {"hash": "B1"}, "in_msg": {"source": "EQpay", "value": str(nano), "message": code},
                     "out_msgs": [{"destination": "EQpay", "value": str(nano - 5)}]}]
        subs._fetch_txs = txs
        await subs.check_payments()
        assert not subs.state["invoices"][code].get("paid") and not subs.state["invoices"][code].get("got")
        # 2) ограниченный, но вышедший — не «в канале»: оплата даёт ссылку
        bot.st[51] = ("restricted", False)
        t = await subs.grant(51, 3600, "paid")
        assert "L" in t and not subs.user(51)["in_chat"], t
        # 3) зашёл без оплаты, пока копия не прочитана → после чтения копии его выкидывают
        subs.degraded = True
        await subs.on_join(ev(52, "left", "member"))
        assert 52 not in bot.bans and subs.user(52)["in_chat"]
        await subs._expire(); await subs._reconcile()
        assert 52 not in bot.bans                       # пока копии нет — не трогаем
        await subs.import_({"users": {}})
        subs.degraded = False
        bot.st[52] = ("member", True)
        await subs._expire()
        assert 52 in bot.bans, bot.bans
        # 3b) а если в копии оплачено — остаётся
        subs.degraded = True
        await subs.on_join(ev(53, "left", "member"))
        await subs.import_({"users": {"53": {"until": _t.time() + 86400, "in_chat": True}}})
        subs.degraded = False
        bot.st[53] = ("member", True)
        await subs._expire()
        assert 53 not in bot.bans and subs.user(53)["until"] > _t.time() + 86000
    asyncio.run(run())
    print("SUB LOOPHOLES OK")

test_sub_loopholes()


def test_sender_retry_after_releases_queue():
    import asyncio, types
    from aiogram.exceptions import TelegramRetryAfter
    from bot.sender import TelegramSender
    async def run():
        order, calls = [], {"A": 0}
        class Bot:
            async def send_message(s, chat, text, **kw):
                if chat == -1 and calls["A"] < 6:          # группа A под лимитом 6 раз подряд
                    calls["A"] += 1
                    raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
                order.append(chat); return types.SimpleNamespace(message_id=1)
        settings.test_mode = False; settings.send_interval = 0
        snd = TelegramSender(Bot())
        a = asyncio.create_task(snd.send(-1, "a"))
        await asyncio.sleep(0)
        b = asyncio.create_task(snd.send(-2, "b"))
        ra, rb = await asyncio.gather(a, b)
        assert ra.ok and rb.ok, (ra.error, rb.error)       # 6 ожиданий лимита ≠ 5 «неудачных попыток»
        assert order.index(-2) < order.index(-1), order     # B не ждал, пока A отлежит лимит
        assert not snd._lock._busy
    asyncio.run(run())
    print("SENDER RETRY-AFTER OK")

test_sender_retry_after_releases_queue()


def test_backup_migration_timestamps_and_old_key():
    import asyncio, json, tempfile
    from pathlib import Path
    from types import SimpleNamespace as NS
    import sessions.backup as B
    from sessions.storage import SessionStorage
    class FB:
        def __init__(s, ptr, b): s.ptr, s.blob, s.desc = ptr, b, None
        async def get_my_description(s, language_code=None):
            return NS(description=s.desc or (B.POINTER_PREFIX2 + json.dumps(s.ptr)))
        async def set_my_description(s, description=None, language_code=None): s.desc = description
        async def download(s, fid, destination): destination.write(s.blob)
        @property
        def session(s):
            async def close(): pass
            return NS(close=close)
    body = lambda who: json.dumps({"v": 2, "sessions": [{"name": "../evil", "string": "x"}, 5],
                                   "admins": ["bad", 7], "sent": [], "flags": {"who": who}}).encode()
    orig, key = B.Bot, settings.session_backup_key
    try:
        # без SESSION_BACKUP_KEY: копия старого бота зашифрована ЕГО токеном — всё равно читается
        settings.session_backup_key = ""
        settings.bot_token, settings.old_bot_token = "333:ccc", "222:bbb,111:aaa"
        enc_old = B._key("111:aaa").encrypt(body("oldest"))
        enc_mid = B._key("222:bbb").encrypt(body("mid"))
        # у среднего копия СТАРЕЕ по времени, хотя он идёт первым — берём самую свежую
        bots = {"222:bbb": FB({"s": ["m", 1, 2, 100]}, enc_mid), "111:aaa": FB({"s": ["o", 1, 2, 200]}, enc_old)}
        B.Bot = lambda t: bots[t]
        b = B.SessionBackup(FB({}, b""), SessionStorage(Path(tempfile.mkdtemp()), 1, "h"), lambda: set())
        asyncio.run(b.restore())
        assert b.flags.get("who") == "oldest" and b._ptr.get("mig") == "111", (b.flags, b._ptr)
        assert b.restore_done and b.restored_count == 0       # битые записи пропущены, не уронили восстановление
        # своя копия свежее всех старых — не переносим
        own = FB({"s": ["own", 1, 2, 300]}, B._fernet().encrypt(body("own")))
        b = B.SessionBackup(own, SessionStorage(Path(tempfile.mkdtemp()), 1, "h"), lambda: set())
        asyncio.run(b.restore())
        assert b.flags.get("who") == "own", b.flags
    finally:
        B.Bot = orig
        settings.old_bot_token, settings.session_backup_key = "", key
    print("BACKUP TIMESTAMPS + OLD KEY OK")

test_backup_migration_timestamps_and_old_key()


def test_sender_same_group_waits_flood():
    import asyncio, types
    from aiogram.exceptions import TelegramRetryAfter
    from bot.sender import TelegramSender
    async def run():
        calls = []
        class Bot:
            first = True
            async def send_message(s, chat, text, **kw):
                calls.append(text)
                if s.first:
                    s.first = False
                    raise TelegramRetryAfter(method=None, message="flood", retry_after=0.3)
                return types.SimpleNamespace(message_id=1)
        settings.test_mode = False; settings.send_interval = 0
        snd = TelegramSender(Bot())
        a = asyncio.create_task(snd.send(-1, "a", thread_id=3))
        await asyncio.sleep(0.05)
        b = asyncio.create_task(snd.send(-1, "b", thread_id=5))     # другая тема той же группы
        ra, rb = await asyncio.gather(a, b)
        assert ra.ok and rb.ok
        assert calls.count("b") == 1, calls     # пока группа под лимитом, B не стучится зря
    asyncio.run(run())
    print("SENDER GROUP FLOOD OK")

test_sender_same_group_waits_flood()


def test_new_collection_and_bad_item():
    import types as T
    from tracker.coordinator import MarketplaceCoordinator as Coordinator
    from tracker.state import CollectionState
    import tracker.marketplace as MP
    c = Coordinator.__new__(Coordinator)
    c.skipped, c.low_prio, c._dirty, c._due = set(), set(), set(), {}
    settings.collection_min_floor, settings.collection_floor_mode = 300, "skip"
    st = CollectionState(gift_id=77, title="New Gift")
    assert c._apply_floor_rule(st, None, first=True) is False and 77 not in c.skipped   # без флора — отслеживаем
    assert c._apply_floor_rule(CollectionState(gift_id=78, title="x"), 100, first=True) is True
    # кривой лот не ломает страницу
    good = MP.types.StarGiftUnique(id=1, gift_id=5, title="A", slug="A-1", num=1, owner_id=MP.types.PeerUser(9),
                                   attributes=[], availability_issued=1, availability_total=1)
    class Bad:
        slug = "B-2"
    MP.types.StarGiftUnique  # noqa
    orig = MP.parse_unique_gift
    def p(g, u, ch):
        if isinstance(g, Bad):
            raise AttributeError("new field")
        return orig(g, u, ch)
    MP.parse_unique_gift = p
    try:
        import asyncio
        class Cl:
            async def __call__(s, req):
                return T.SimpleNamespace(users=[], chats=[], gifts=[Bad(), good], next_offset=None, count=2)
        page = asyncio.run(MP.fetch_resale_page(Cl(), 5, "", 30))
        assert [i.slug for i in page.items] == ["A-1"], page.items
    finally:
        MP.parse_unique_gift = orig
    print("NEW COLLECTION + BAD ITEM OK")

test_new_collection_and_bad_item()


def test_supervisor_restarts_loops():
    import asyncio
    import tracker.worker as W
    async def run():
        runs = []
        stop = asyncio.Event()
        async def loop():
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("boom")              # упала с ошибкой → перезапуск
            if len(runs) == 2:
                f = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(f.cancel)
                await f                                 # «чужая» отмена (как от Telethon) → перезапуск
            await asyncio.get_running_loop().create_future()   # дальше просто работает
        t = asyncio.create_task(W.supervise("t", loop, stop, delay=0))
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(runs) == 3, runs
        stop.set(); t.cancel()                          # своя отмена → выходит
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert t.done()
    asyncio.run(run())
    print("SUPERVISOR OK")

test_supervisor_restarts_loops()


def test_backup_file_import():
    import asyncio, json, tempfile, zlib
    from pathlib import Path
    from types import SimpleNamespace as NS
    from telethon.sessions import StringSession
    from telethon.crypto import AuthKey
    import sessions.backup as B
    from sessions.storage import SessionStorage
    ss = StringSession(); ss.set_dc(2, "149.154.167.51", 443); ss.auth_key = AuthKey(b"\x07" * 256)
    acc = ss.save()
    body = {"v": 2, "sessions": [{"name": "session_001", "string": acc, "api_id": 1, "api_hash": "h" * 32}],
            "admins": [999], "sent": [], "flags": {}, "settings": {"MIN_STARS": "99999"}}
    blob = B._fernet().encrypt(b"Z1" + zlib.compress(json.dumps(body).encode()))
    class FB:
        async def get_my_description(s, language_code=None): return NS(description="")
    st = SessionStorage(Path(tempfile.mkdtemp()), 1, "h")
    b = B.SessionBackup(FB(), st, lambda: set())
    async def run():
        await b.restore()
        n = await b.import_file(blob)
        assert n == 1 and [r.name for r in st.list_sessions()] == ["session_001"], n
        assert settings.min_stars != 99999                 # /set из чужого файла не применяются
        assert await b.import_file(blob) == 0               # тот же аккаунт второй раз не дублируется
        for bad in (b"junk", B._key("other:key").encrypt(b"x")):
            try:
                await b.import_file(bad); raise AssertionError("must fail")
            except ValueError:
                pass
    asyncio.run(run())
    print("BACKUP FILE IMPORT OK")

test_backup_file_import()
