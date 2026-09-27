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
    p.sessions = S(); p._seller_cache = {}; p.stats = {}
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
