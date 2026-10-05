"""Случайные сценарии подписок (оплаты, входы, перезапуски, сбои Telegram) — проверка, что оплаченное
время не теряется, двойного начисления нет, без доступа в канале не остаются.
Запуск: python tests/fuzz_subscriptions.py 3000   (NOLOSE=1 — без «потерянных» апдейтов Telegram)
usage: python fuzz.py SEEDS [START] [--shrink]"""
import asyncio, json, random, sys, types, traceback, math, os, logging, time as _rt
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT); os.chdir(_ROOT)
from config import settings
settings.admin_ids = {1}; settings.target_chat_id = -100; settings.sub_chat_id = 0
settings.sub_ton_address = "UQtest"
if os.environ.get("PATCHED"):
    import importlib.util
    _sp = importlib.util.spec_from_file_location("bot.subscription", os.environ["PATCHED"])
    S = importlib.util.module_from_spec(_sp); sys.modules["bot.subscription"] = S; _sp.loader.exec_module(S)
else:
    import bot.subscription as S
logging.disable(logging.CRITICAL)
NS = types.SimpleNamespace
INF = math.inf
FOREVER = S.FOREVER
T0 = 1_700_000_000.0


class Clock: t = T0
CLK = Clock()
S.time = NS(time=lambda: CLK.t)
async def _nosleep(*a, **k): return None
S.asyncio = NS(Lock=asyncio.Lock, Event=asyncio.Event, sleep=_nosleep)
_tok = [0]
def _token_hex(n): _tok[0] += 1; return f"{_tok[0]:08x}"
S.secrets = NS(token_hex=_token_hex)


class Crash(BaseException): pass
class TgErr(Exception): pass


class World:
    def __init__(s):
        s.members = set(); s.links = {}; s.nlink = 0; s.chain = []; s.nh = 0
        s.fail = set(); s.crash_at = None; s.calls = 0; s.updates = []; s.sent = []; s.h = None

    def call(s, name):
        s.calls += 1
        if s.crash_at is not None and s.calls == s.crash_at:
            raise Crash()
        if name in s.fail:
            raise TgErr(f"{name}: Bad Gateway")


class FakeBot:
    def __init__(s, w): s.w = w
    async def get_chat_member(s, chat, uid):
        s.w.call("gcm"); return NS(status="member" if uid in s.w.members else "left")
    async def create_chat_invite_link(s, chat, name=None, member_limit=None, expire_date=None, **kw):
        s.w.call("create"); s.w.nlink += 1; l = f"https://t.me/+L{s.w.nlink}"
        s.w.links[l] = dict(owner=int(name.split()[1]), revoked=False, used=0, exp=expire_date)
        return NS(invite_link=l)
    async def revoke_chat_invite_link(s, chat, l):
        s.w.call("revoke")
        if l in s.w.links: s.w.links[l]["revoked"] = True
    async def send_message(s, uid, text, **kw):
        s.w.call("send"); s.w.sent.append((uid, text))
    async def ban_chat_member(s, chat, uid, until_date=None, **kw):
        s.w.call("ban"); s.w.h.on_ban(uid)
        if uid in s.w.members:
            s.w.members.discard(uid); s.w.h.admin_added.discard(uid)
            s.w.updates.append(dict(uid=uid, old="member", new="kicked", link=None, adder=None, lose=False))
    async def unban_chat_member(s, chat, uid, **kw):
        s.w.call("unban")


class DB:
    def __init__(s): s.kv = {}
    async def kv_get(s, k, d=None): return s.kv.get(k, d)
    async def kv_set(s, k, v): s.kv[k] = v


def mkev(d):
    u = NS(id=d["uid"], username=f"u{d['uid']}", first_name="U", last_name=None, is_bot=False)
    return NS(chat=NS(id=-100), from_user=NS(id=d["adder"] or d["uid"]),
              old_chat_member=NS(status=d["old"], user=u), new_chat_member=NS(status=d["new"], user=u),
              invite_link=NS(invite_link=d["link"]) if d["link"] else None)


class Q:
    def __init__(s, uid, data):
        s.from_user = NS(id=uid, username=f"u{uid}", first_name="U", last_name=None, is_bot=False)
        s.data = data; s.answers = []; s.message = s
    async def answer(s, text=None, **kw): s.answers.append(text)
    async def edit_text(s, text, **kw): s.answers.append(text)


class Harness:
    def __init__(s, plose):
        s.w = World(); s.w.h = s; s.db = DB(); s.backup = None
        s.inv = {}; s.opaid = {}; s.ent = {}; s.cons = {}; s.admin_added = set()
        s.revoking = None; s.viol = {}; s.plose = plose; s.late_ignored = 0; s.cur = None

    # ---------- oracle
    def add_ent(s, uid, secs, src, code=None):
        s.ent.setdefault(uid, []).append(dict(secs=secs, src=src, code=code))

    def tot(s, uid, known_only=False):
        es = s.ent.get(uid, [])
        if known_only:
            st = s.subs.state["invoices"]
            es = [e for e in es if e["src"] != "inv" or e["code"] not in st or st[e["code"]].get("paid")]
        if any(e["secs"] == FOREVER for e in es): return INF
        return sum(e["secs"] for e in es)

    def V(s, uid):
        u = s.subs.state["users"].get(str(uid)) or {}
        un, p = u.get("until") or 0, u.get("pending") or 0
        if un == FOREVER or p == FOREVER: return INF
        return max(0.0, float(un) - CLK.t) + float(p)

    def oracle_pay(s, code):
        if code in s.opaid: return
        i = s.inv[code]; got = 0; seen = set()
        for t in s.w.chain:
            if t["h"] in seen or code not in t["c"].strip().upper(): continue
            seen.add(t["h"])
            if t["ut"] - i["ts"] < 7 * 86400:
                got += t["v"]
            else:
                s.late_ignored += 1
        if got >= i["nano"]:
            s.opaid[code] = True
            s.add_ent(i["uid"], S.PLANS[i["plan"]][2], "inv", code)

    def V_(s, kind, uid, msg):
        key = kind
        if key not in s.viol: s.viol[key] = f"{kind} uid={uid} step={s.cur}: {msg}"

    def on_ban(s, uid):
        if uid == s.revoking or uid not in s.w.members: return
        rem = s.tot(uid, known_only=True) - s.cons.get(uid, 0.0)
        if rem > 60:
            import inspect
            fn = next((f.function for f in inspect.stack() if "subscription.py" in f.filename and f.function != "kick"), "?")
            s.V_(f"inv4-kicked-with-access via {fn}", uid, f"remaining paid {rem:.0f}s, bot user={s.subs.state['users'].get(str(uid))}")

    def check_upper(s):
        for uid in range(2, 6):
            t = s.tot(uid); v = s.V(uid)
            if t == INF: continue
            if v == INF: s.V_("inv2-overcredit-forever", uid, "bot gives forever, paid finite")
            elif v + s.cons.get(uid, 0) > t + 1:
                s.V_("inv2-overcredit", uid, f"granted {v + s.cons.get(uid,0):.0f} > entitled {t:.0f}")
        for c, i in s.subs.state["invoices"].items():
            if i.get("paid") and c in s.inv and c not in s.opaid:
                s.V_("inv2-bot-paid-not-paid", i["uid"], c)

    def check_lower(s):
        for uid in range(2, 6):
            t = s.tot(uid); v = s.V(uid)
            if t == INF and v != INF:
                s.V_("inv1-lost-forever", uid, f"bot user={s.subs.state['users'].get(str(uid))}")
            elif t != INF and v + s.cons.get(uid, 0) < t - 1:
                s.V_("inv1-lost-time", uid, f"granted {v + s.cons.get(uid,0):.0f} < entitled {t:.0f}; "
                     f"bot user={s.subs.state['users'].get(str(uid))}")
        for uid in s.w.members:
            if uid == 1 or uid in s.admin_added: continue
            if not s.subs._has_access(s.subs.state["users"].get(str(uid))):
                u = s.subs.state["users"].get(str(uid))
                tag = "unknown-user" if u is None else ("pending" if u.get("pending") else "no-pending")
                s.V_(f"inv3-member-without-access ({tag})", uid, f"bot user={s.subs.state['users'].get(str(uid))}")

    # ---------- bot lifecycle
    async def snap(s):
        s.backup = json.dumps(s.subs.export())

    async def make_subs(s):
        app = NS(db=s.db, bot=FakeBot(s.w), users_closed=False, coordinator=None)
        sub = S.Subscriptions(app)
        sub.on_change = s.snap
        async def fetch():
            return [{"transaction_id": {"hash": t["h"]}, "utime": t["ut"],
                     "in_msg": {"source": "EQ", "value": str(t["v"]), "message": t["c"]}}
                    for t in reversed(s.w.chain[-50:])]
        sub._fetch_txs = fetch
        orig = sub.grant
        async def g(uid, seconds, reason, inv=None):
            r = await orig(uid, seconds, reason, inv=inv)
            if "администратором" in reason or "Пробный" in reason:
                s.add_ent(uid, seconds, "adm")
            return r
        sub.grant = g
        r, _ = S.build_sub_router(app, sub, lambda u: u in settings.admin_ids)
        s.hd = {h.callback.__name__: h.callback for h in r.callback_query.handlers + r.message.handlers}
        s.subs = sub
        data = json.loads(s.backup) if s.backup else None
        if data:
            await sub.import_(data)
        else:
            await sub.load()
        sub.ready.set()

    async def restart(s, kind):
        if kind == "graceful":
            await s.snap()
        if kind == "fresh":
            s.db = DB()
        s.w.updates.clear() if kind == "fresh_upd" else None
        s.w.fail = set(); s.w.crash_at = None
        await s.make_subs()

    async def safe(s, coro, where):
        try:
            await coro
        except Crash:
            raise
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            loc = next((f"{f.filename.split('/')[-1]}:{f.lineno}" for f in reversed(tb)
                        if "subscription.py" in f.filename), "?")
            s.V_(f"inv5-exc {type(e).__name__} @{loc}", None, f"{where}: {e!r}")

    async def drain(s, ev=None):
        while s.w.updates:
            d = s.w.updates.pop(0)
            if d.get("lose"): continue
            await s.safe(s.subs.on_join(mkev(d)), "on_join")

    async def tick(s):
        await s.safe(s._tick1(), "expire/remind/reconcile")
        await s.safe(s.subs._retry_undelivered(), "retry")
        await s.safe(s.subs.check_payments(), "check_payments")
        await s.drain()

    async def _tick1(s):
        await s.subs._expire(); await s.subs._remind(); await s.subs._reconcile()

    async def advance(s, dt, chunk=600):
        while dt > 0:
            st = min(dt, chunk); t0 = CLK.t; t1 = t0 + st
            for k, u in s.subs.state["users"].items():
                un = u.get("until") or 0
                if un != FOREVER and float(un) > t0:
                    s.cons[int(k)] = s.cons.get(int(k), 0.0) + min(float(un), t1) - t0
            CLK.t = t1; dt -= st
            await s.tick()

    def tx(s, v, c):
        s.w.nh += 1; s.w.chain.append(dict(h=f"h{s.w.nh}", v=int(v), c=c, ut=CLK.t))

    # ---------- events
    async def ex(s, e):
        k = e["k"]; uid = e.get("uid", 2); w = s.w
        if k == "inv":
            code = await s.subs.new_invoice(uid, e["plan"])
            if code not in s.inv:
                i = s.subs.state["invoices"][code]
                s.inv[code] = dict(uid=uid, plan=i["plan"], ts=i["ts"], nano=i["nano"])
        elif k == "pay":
            if not s.inv: return
            code = list(s.inv)[e["i"] % len(s.inv)]; n = s.inv[code]["nano"]; m = e["mode"]
            if m == "late": await s.advance(25 * 3600)
            if m in ("full", "late"): s.tx(n, code)
            elif m == "lower": s.tx(n, f"  {code.lower()} ")
            elif m == "partial": s.tx(n * e["r"], code)
            elif m == "split": a = int(n * e["r"]); s.tx(a, code); s.tx(n - a, code)
            elif m == "overpay": s.tx(n * 1.5, code)
            elif m == "wrong": s.tx(n, "TRKZZZZZZZZ")
            elif m == "dup" and w.chain: w.chain.append(dict(w.chain[e["i"] % len(w.chain)]))
            s.oracle_pay(code)
            await s.advance(20)
        elif k == "restart":
            await s.restart(e["kind"])
        elif k == "join":
            if uid in w.members: return
            link, adder = None, None
            valid = [l for l, r in w.links.items() if not r["revoked"] and not r["used"] and r["exp"] > CLK.t]
            if e["how"] == "own":
                own = [l for l in valid if w.links[l]["owner"] == uid]
                if not own: return
                link = own[-1]
            elif e["how"] == "other":
                oth = [l for l in valid if w.links[l]["owner"] != uid]
                if not oth: return
                link = oth[e["j"] % len(oth)]
            elif e["how"] == "admin":
                adder = 1
            if link: w.links[link]["used"] = 1
            w.members.add(uid)
            if adder: s.admin_added.add(uid)
            w.updates.append(dict(uid=uid, old="left", new="member", link=link, adder=adder, lose=e.get("lose", False)))
        elif k == "leave":
            if uid not in w.members: return
            w.members.discard(uid); s.admin_added.discard(uid)
            w.updates.append(dict(uid=uid, old="member", new="left", link=None, adder=None, lose=e.get("lose", False)))
        elif k == "my":
            await s.safe(s.hd["cb_my"](Q(uid, "sub:my")), "cb_my")
        elif k == "trial":
            await s.safe(s.hd["cb_trial"](Q(uid, "sub:trial")), "cb_trial")
        elif k == "grant":
            m = Q(1, ""); m.answer = m.answer
            await s.safe(s.hd["cmd_grant"](m, NS(args=f"{uid} {e['dur']}")), "cmd_grant")
        elif k == "revoke":
            m = Q(1, ""); s.revoking = uid
            try:
                await s.safe(s.hd["cmd_revoke"](m, NS(args=str(uid))), "cmd_revoke")
            finally:
                s.revoking = None
            if any(a and "забран" in a for a in m.answers):
                st = s.subs.state["invoices"]
                s.ent[uid] = [x for x in s.ent.get(uid, []) if x["src"] == "inv" and x["code"] in st
                              and not (st[x["code"]].get("paid") and st[x["code"]].get("granted") is True)]
                s.cons[uid] = 0.0
        elif k == "time":
            await s.advance(e["dt"])
        elif k == "bk":
            await s.snap()

    async def step(s, i, e):
        s.cur = i
        w = s.w
        w.fail = set(e.get("fail", ())); w.crash_at = (w.calls + e["crash"]) if e.get("crash") else None
        try:
            await s.ex(e)
            await s.drain()
        except Crash:
            await s.restart("crash")
        finally:
            w.fail = set(); w.crash_at = None
        s.check_upper()

    async def settle(s):
        s.cur = "settle"
        s.w.fail = set(); s.w.crash_at = None
        await s.drain()
        await s.advance(1800, chunk=120)
        s.check_upper(); s.check_lower()


KINDS = ["inv", "pay", "restart", "join", "leave", "my", "trial", "grant", "revoke", "time", "bk"]
WTS   = [10,    14,    5,         12,     5,       5,    3,       3,       1,        14,     2]


def gen(seed, n=40):
    rng = random.Random(seed)
    E = os.environ.get
    pfail = 0 if E("NOFAIL") else rng.choice([0, 0.15, 0.35])
    pcrash = 0 if E("NOCRASH") else rng.choice([0, 0.08])
    plose = 0 if E("NOLOSE") else rng.choice([0, 0, 0.08])
    fails = [f for f in ["gcm", "create", "ban", "revoke", "send", "unban"] if f not in (E("NOFAILS") or "").split(",")]
    evs = []
    for _ in range(n):
        k = rng.choices(KINDS, WTS)[0]
        e = {"k": k, "uid": rng.randint(2, 5)}
        if k == "inv": e["plan"] = rng.choice(list(S.PLANS))
        if k == "pay":
            e["i"] = rng.randrange(1000)
            e["mode"] = rng.choice(["full", "full", "lower", "partial", "split", "overpay", "wrong", "dup", "late"])
            e["r"] = rng.choice([0.3, 0.5, 0.7])
        if k == "restart": e["kind"] = rng.choice(["graceful", "crash", "crash"] + ([] if E("NOFRESH") else ["fresh"]))
        if k == "join":
            e["how"] = rng.choice(["own", "own", "own", "none", "admin"] + ([] if E("NOOTHER") else ["other"])); e["j"] = rng.randrange(100)
        if k == "grant": e["dur"] = rng.choice(["24", "30м", "1д", "навсегда"])
        if k == "time": e["dt"] = rng.choice([5, 60, 600, 1800, 3600, 3 * 3600, 13 * 3600, 25 * 3600, 49 * 3600])
        if rng.random() < pfail:
            e["fail"] = rng.sample(fails, min(len(fails), rng.randint(1, 2)))
        if rng.random() < pcrash: e["crash"] = rng.randint(1, 6)
        if rng.random() < plose and k in ("join", "leave"): e["lose"] = True
        evs.append(e)
    return evs


def run(evs):
    CLK.t = T0; _tok[0] = 0
    h = Harness(0)
    async def go():
        await h.make_subs()
        for i, e in enumerate(evs):
            await h.step(i, e)
        await h.settle()
    try:
        asyncio.run(go())
    except Exception as e:
        h.V_(f"harness-crash {type(e).__name__}", None, traceback.format_exc()[-600:])
    return h


def shrink(evs, key):
    def bad(x): return key in run(x).viol
    cur = list(evs)
    changed = True
    while changed:
        changed = False
        for size in (8, 4, 2, 1):
            i = 0
            while i < len(cur):
                cand = cur[:i] + cur[i + size:]
                if bad(cand): cur = cand; changed = True
                else: i += size
        for i, e in enumerate(cur):          # strip noise attributes
            for a in ("fail", "crash", "lose"):
                if a in e:
                    c2 = dict(e); c2.pop(a); cand = cur[:i] + [c2] + cur[i + 1:]
                    if bad(cand): cur = cand; changed = True
            if "fail" in cur[i] and len(cur[i]["fail"]) > 1:
                for f in cur[i]["fail"]:
                    c2 = dict(cur[i]); c2["fail"] = [f]; cand = cur[:i] + [c2] + cur[i + 1:]
                    if bad(cand): cur = cand; changed = True; break
    return cur


if __name__ == "__main__":
    N = int(sys.argv[1]); start = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 0
    do_shrink = "--shrink" in sys.argv
    found = {}; counts = {}; late = 0; t0 = _rt.time()
    for seed in range(start, start + N):
        evs = gen(seed); h = run(evs); late += h.late_ignored
        for k, msg in h.viol.items():
            counts[k] = counts.get(k, 0) + 1
            if k not in found: found[k] = (seed, evs, msg)
    print(f"{N} seeds in {_rt.time() - t0:.0f}s; late payments ignored by oracle/bot: {late}")
    for k, c in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"{c:5d}  {k}")
    if do_shrink:
        for k, (seed, evs, msg) in found.items():
            m = shrink(evs, k)
            print(f"\n=== {k}  (seed {seed})\n{run(m).viol[k]}")
            for e in m: print("   ", e)
