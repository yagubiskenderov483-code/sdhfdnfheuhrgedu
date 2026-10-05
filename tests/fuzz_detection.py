"""Property-based fuzzer for the NEW-lot detection pipeline (fast sync mode, 1 page).

Запуск: python tests/fuzz_detection.py [runs] [steps] [seed0]
"""
from __future__ import annotations

import os
import asyncio
import json
import logging
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.disable(logging.CRITICAL)

from config import settings  # noqa: E402
from database.db import Database  # noqa: E402
from tracker.marketplace import ResaleItem  # noqa: E402
from tests.test_tracker import GID, Env, FakeMarket, patch_market, reset_settings  # noqa: E402

import utils.logger as _ul  # noqa: E402
_ul.llog = lambda *a, **k: None
import tracker.coordinator as _cm, tracker.queue as _qm  # noqa: E402
_cm.llog = _qm.llog = lambda *a, **k: None

SCRATCH = Path(__import__("tempfile").gettempdir()) / "wawe_fuzz"
SCRATCH.mkdir(parents=True, exist_ok=True)
import os
IGNORE = set(filter(None, os.environ.get("IGNORE", "").split(",")))

# ---------------------------------------------------------------- admission recorder
ADMITTED: list[tuple[int, str]] = []
_orig_insert = Database.insert_listing_if_new


async def _rec_insert(self, l):
    ok = await _orig_insert(self, l)
    if ok:
        ADMITTED.append((l.gift_unique_id, l.seller_key))
    return ok
Database.insert_listing_if_new = _rec_insert


def mk(uid: int, stars: int, seller: str) -> ResaleItem:
    hidden = seller.startswith("h")
    key = "n" if hidden else seller
    oid = None if hidden else int(seller[1:])
    return ResaleItem(unique_id=uid, gift_id=GID, title="Ice Cream", slug=f"IceCream-{uid}", num=uid,
                      model="M", backdrop="B", symbol="S", seller_key=key,
                      owner_type=None if hidden else "user", owner_id=oid, owner_name=None,
                      owner_address=None, stars=stars, ton_nano=None, ton_only=False)


class World:
    """Ground truth on top of FakeMarket. Listing identity = (uid, true_seller)."""

    def __init__(self, market: FakeMarket):
        self.m = market
        self.seller_of: dict[int, str] = {}            # uid -> true seller of current/last listing
        self.history: dict[int, list[str]] = {}        # uid -> sellers in order
        self.pre: set[tuple[int, str]] = set()         # listings existing at baseline
        self.repriced: set[tuple[int, str]] = set()
        self.new: set[tuple[int, str]] = set()          # genuinely new listings (after baseline)
        self.exempt: set[tuple[int, str]] = set()       # hidden->hidden resale

    def on_market(self, uid):
        return any(x.unique_id == uid for x in self.m.lots[GID])

    def get(self, uid):
        return next((x for x in self.m.lots[GID] if x.unique_id == uid), None)

    # ---- ops (return False when not applicable, e.g. after shrinking)
    def list_new(self, uid, stars, seller, baseline=False):
        if self.on_market(uid):
            return False
        prev = self.history.get(uid, [])
        L = (uid, seller)
        if not baseline and L not in self.pre and (not prev or prev[-1] != seller):
            self.new.add(L)
            if prev and prev[-1].startswith("h") and seller.startswith("h"):
                self.exempt.add(L)
        elif not baseline and prev and prev[-1] == seller:
            pass  # same seller re-list: same listing
        if baseline:
            self.pre.add(L)
        self.history.setdefault(uid, []).append(seller) if (not prev or prev[-1] != seller) else None
        self.seller_of[uid] = seller
        self.m.lots[GID].insert(0, mk(uid, stars, seller))
        return True

    def sell(self, uid):
        if not self.on_market(uid):
            return False
        self.m.sell(uid)
        return True

    def reprice(self, uid, stars):
        it = self.get(uid)
        if it is None or abs(it.stars - stars) / it.stars <= 0.01:
            return False
        self.m.change_price(uid, stars)
        self.repriced.add((uid, self.seller_of[uid]))
        return True

    def relist_same(self, uid):
        it = self.get(uid)
        if it is None:
            return False
        self.m.sell(uid)
        self.m.lots[GID].insert(0, it)
        return True

    def resell(self, uid, stars, seller):
        it = self.get(uid)
        if it is None or self.seller_of[uid] == seller:
            return False
        self.m.sell(uid)
        return self.list_new(uid, stars, seller)


def apply(w: World, op) -> bool:
    k = op[0]
    if k == "list":
        ok = False
        for uid, stars, seller in op[1]:
            ok |= w.list_new(uid, stars, seller)
        return ok
    if k == "sell":
        return w.sell(op[1])
    if k == "reprice":
        return w.reprice(op[1], op[2])
    if k == "relist":
        return w.relist_same(op[1])
    if k == "resell":
        return w.resell(op[1], op[2], op[3])
    return True


# ---------------------------------------------------------------- random op generation
def gen_ops(rng: random.Random, n_init: int, steps: int):
    """Generates ops against a shadow model (uids only) so the sequence is replayable."""
    on = list(range(1, n_init + 1))          # top first
    seller = {u: f"u{5000 + u}" for u in on}
    price = {u: 9000 for u in on}
    next_uid, next_seller, next_hidden = 100000, 900000, 1
    ops = []

    def new_seller(hidden_p=0.25):
        nonlocal next_seller, next_hidden
        if rng.random() < hidden_p:
            next_hidden += 1
            return f"h{next_hidden}"
        next_seller += 1
        return f"u{next_seller}"

    def pick(deep_ok=True):
        if not on:
            return None
        r = rng.random()
        if r < 0.25:
            return on[0]                                           # top / likely anchor
        if r < 0.45:
            return rng.choice(on[: min(5, len(on))])
        if deep_ok and r < 0.7 and len(on) > 100:
            return rng.choice(on[100:])                            # beyond the 1-page baseline
        return rng.choice(on)

    def newprice(old):
        while True:
            p = rng.randrange(5000, 60000, 50)
            if abs(p - old) / old > 0.02:
                return p

    for _ in range(steps):
        nops = 1 if rng.random() < 0.6 else rng.randint(2, 6)      # several ops = missed polls
        for _ in range(nops):
            r = rng.random()
            if r < 0.30:
                n = rng.choice([0, 1, 1, 1, 2, 3, 5, 10, rng.randint(0, 40)])
                batch = []
                for _ in range(n):
                    next_uid += 1
                    s = new_seller()
                    batch.append((next_uid, rng.randrange(5000, 60000, 50), s))
                    on.insert(0, next_uid); seller[next_uid] = s; price[next_uid] = batch[-1][1]
                ops.append(("list", batch))
            elif r < 0.50:
                u = pick()
                if u is None:
                    continue
                on.remove(u)
                ops.append(("sell", u))
                if rng.random() < 0.3:      # sold lot gets re-listed later by new owner
                    s = new_seller(0.4)
                    p = newprice(price[u]) if rng.random() < 0.7 else price[u]
                    ops.append(("list", [(u, p, s)])); on.insert(0, u); seller[u] = s; price[u] = p
            elif r < 0.70:
                u = pick()
                if u is None:
                    continue
                p = newprice(price[u]); price[u] = p
                on.remove(u); on.insert(0, u)
                ops.append(("reprice", u, p))
            elif r < 0.80:
                u = pick()
                if u is None:
                    continue
                on.remove(u); on.insert(0, u)
                ops.append(("relist", u))
            else:
                u = pick()
                if u is None:
                    continue
                hid = seller[u].startswith("h")
                s = new_seller(0.6 if hid else 0.3)
                p = newprice(price[u]) if rng.random() < 0.8 else price[u]
                on.remove(u); on.insert(0, u); seller[u] = s; price[u] = p
                ops.append(("resell", u, p, s))
        r = rng.random()
        if r < 0.06:
            ops.append(("restart",))
        elif r < 0.85:
            ops.append(("poll",))
    ops.append(("poll",))
    return ops


# ---------------------------------------------------------------- execution + invariants
async def execute(n_init: int, ops, stop_on_first=True):
    reset_settings()
    settings.sync_mode = "fast"
    settings.fast_sync_pages = 1
    ADMITTED.clear()
    tmp = Path(tempfile.mkdtemp(dir=SCRATCH))
    market = FakeMarket()
    w = World(market)
    for uid in range(n_init, 0, -1):               # uid 1 on top
        w.list_new(uid, 9000, f"u{5000 + uid}", baseline=True)
    patch_market(market)
    env = await Env(tmp, market).boot()
    viol = []
    sent_by_listing: dict[tuple[int, str], int] = {}
    seen_adm = 0

    def check(step):
        nonlocal seen_adm
        for uid, key in ADMITTED[seen_adm:]:
            ts = w.seller_of.get(uid)
            L = (uid, ts)
            sent_by_listing[L] = sent_by_listing.get(L, 0) + 1
            if sent_by_listing[L] > 1:
                viol.append(("DUP_SEND", step, L))
            if L in w.pre and L not in w.repriced:
                tag = "deep" if uid > 100 else "shallow"
                viol.append((f"OLD_UNCHANGED_SENT:{tag}", step, L))
        seen_adm = len(ADMITTED)
        for it in market.lots[GID]:
            L = (it.unique_id, w.seller_of[it.unique_id])
            if L in w.new and L not in w.exempt and L not in sent_by_listing:
                h = w.history[it.unique_id]
                prev = h[h.index(L[1]) - 1] if h.index(L[1]) > 0 else None
                k = lambda x: "h" if x.startswith("h") else "u"
                tag = "fresh" if prev is None else f"{k(prev)}->{k(L[1])}"
                if prev is not None and k(L[1]) == "u" and any(x.startswith("h") for x in h[:h.index(L[1])]):
                    tag = "h->u"
                kind = f"MISSED_NEW:{tag}"
                if kind not in IGNORE:
                    viol.append((kind, step, L))

    try:
        for i, op in enumerate(ops):
            if op[0] == "poll":
                await env.poll()
                check(i)
            elif op[0] == "restart":
                await env.shutdown()
                env = await Env(tmp, market).boot()
                await env.pipe.queue.join()
                check(i)
            else:
                apply(w, op)
            viol = [v for v in viol if v[0] not in IGNORE]
            if viol and stop_on_first:
                break
    finally:
        try:
            await env.shutdown()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    return viol


async def shrink(n_init, ops, kind):
    async def fails(o):
        v = await execute(n_init, o)
        return any(x[0] == kind for x in v)

    # shrink initial market size first
    for n in (5, 20, 50, 101, 120, 150):
        if n < n_init and await fails(ops):
            if await fails_n(n, ops, kind):
                n_init = n
                break
    chunk = max(1, len(ops) // 2)
    while chunk >= 1:
        i = 0
        while i < len(ops):
            cand = ops[:i] + ops[i + chunk:]
            if cand and await fails_n(n_init, cand, kind):
                ops = cand
            else:
                i += chunk
        chunk //= 2
    # shrink list batches
    for i, op in enumerate(ops):
        if op[0] == "list" and len(op[1]) > 1:
            j = 0
            while j < len(ops[i][1]) and len(ops[i][1]) > 1:
                b = ops[i][1][:j] + ops[i][1][j + 1:]
                cand = ops[:i] + [("list", b)] + ops[i + 1:]
                if await fails_n(n_init, cand, kind):
                    ops = cand
                else:
                    j += 1
    return n_init, ops


async def fails_n(n, ops, kind):
    v = await execute(n, ops)
    return any(x[0] == kind for x in v)


async def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    steps = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    seed0 = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    t0 = time.time()
    found: dict[str, list] = {}
    for seed in range(seed0, seed0 + runs):
        rng = random.Random(seed)
        n_init = rng.choice([30, 80, 120, 200, 300])
        ops = gen_ops(rng, n_init, steps)
        v = await execute(n_init, ops)
        if v:
            kind = v[0][0]
            found.setdefault(kind, []).append((seed, n_init, ops, v[0]))
        if time.time() - t0 > 900:
            print("time budget hit at seed", seed)
            break
    print(f"runs done in {time.time() - t0:.0f}s")
    for kind, lst in found.items():
        print(f"\n=== {kind}: {len(lst)} seeds, e.g. {[s for s, *_ in lst[:10]]}")
    out = {}
    for kind, lst in found.items():
        seed, n_init, ops, v = min(lst, key=lambda x: len(x[2]))
        n2, ops2 = await shrink(n_init, ops, kind)
        v2 = await execute(n2, ops2)
        print(f"\n--- shrunk {kind} (seed {seed}): n_init={n2} ops={len(ops2)}")
        for o in ops2:
            print("   ", o)
        print("    violation:", [x for x in v2 if x[0] == kind][:3])
        out[kind] = {"seed": seed, "n_init": n2, "ops": ops2, "viol": [x for x in v2 if x[0] == kind][:3]}
    (SCRATCH / "results.json").write_text(json.dumps(out, default=str, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
