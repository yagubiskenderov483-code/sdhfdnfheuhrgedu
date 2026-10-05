"""Property-based fuzzer for sessions/backup.py (SessionBackup) — runs against a COPY of the repo.

usage: python fuzz.py START N [events]      -> prints JSON lines of violations
"""
from __future__ import annotations

import asyncio, io, json, logging, os, random, shutil, sys, tempfile, traceback
from pathlib import Path
from types import SimpleNamespace as NS

REPO = os.environ.get("REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)
logging.disable(logging.CRITICAL)

from aiogram.exceptions import TelegramRetryAfter, TelegramNetworkError, TelegramBadRequest, TelegramForbiddenError
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession
import config as cfg
from config import settings
import sessions.backup as B
from sessions.storage import SessionStorage

GRAVE = 14 * 86400
TMP = os.path.join(__import__("tempfile").gettempdir(), "wawe_fuzz_backup")


class Crash(BaseException):
    """process killed"""


class Clock:
    t = 1_900_000_000.0


CLOCK = Clock()
_real_sleep = asyncio.sleep


class _AsyncioProxy:
    def __getattr__(self, k):
        return getattr(asyncio, k)

    @staticmethod
    async def sleep(d, *a):
        CLOCK.t += d
        await _real_sleep(0)


B.asyncio = _AsyncioProxy()
B.time = NS(time=lambda: CLOCK.t)

# transient unreadable session file (sqlite "database is locked" while Telethon writes it)
_orig_s2s = B.session_file_to_string
SQL_FAIL = {"p": 0.0, "rng": random.Random(0)}


def _flaky_s2s(path):
    if SQL_FAIL["p"] and SQL_FAIL["rng"].random() < SQL_FAIL["p"]:
        return None
    return _orig_s2s(path)


B.session_file_to_string = _flaky_s2s


class FakeBot:
    def __init__(self, rng: random.Random, P: dict):
        self.rng, self.P = rng, P
        self.files: dict[str, bytes] = {}
        self.fseq: dict[str, int] = {}
        self.msgs: dict[tuple, str] = {}
        self.deleted: set = set()
        self.desc: dict = {}
        self.n = 0
        self.deliveries: list = []        # (chat, mid, fid, filename)
        self.blocked: set = set()
        self.crash_p = 0.0
        self.dead = False
        self.fail_get = 0                 # restore failing N times
        self.fail_dl: list[str] = []

    def _crash_pt(self):
        if self.dead:
            raise Crash()
        if self.crash_p and self.rng.random() < self.crash_p:
            self.dead = True
            raise Crash()

    def _p(self, k):
        return self.rng.random() < self.P.get(k, 0)

    async def send_document(self, chat, f, **kw):
        self._crash_pt()
        await _real_sleep(0)
        if chat in self.blocked:
            raise TelegramForbiddenError(None, "Forbidden: bot was blocked by the user")
        if self._p("send_fail"):
            raise TelegramNetworkError(None, "send: connection reset")
        self.n += 1
        fid = f"fid{self.n}"
        self.files[fid], self.fseq[fid] = f.data, self.n
        self.msgs[(chat, self.n)] = fid
        self.deliveries.append((chat, self.n, fid, f.filename))
        self._crash_pt()
        if self._p("send_ambig"):
            raise TelegramNetworkError(None, "send: request timeout")
        return NS(message_id=self.n, document=NS(file_id=fid, file_size=len(f.data)))

    async def set_my_description(self, description=None, language_code=None):
        self._crash_pt()
        await _real_sleep(0)
        if self._p("set_retry"):
            raise TelegramRetryAfter(None, "Too Many Requests", self.rng.choice([1, 5, 30, 119, 121, 300]))
        if self._p("set_fail"):
            raise TelegramNetworkError(None, "setMyDescription: connection reset")
        self.desc[language_code] = description
        self._crash_pt()
        if self._p("set_ambig"):
            raise TelegramNetworkError(None, "setMyDescription: timeout")

    async def get_my_description(self, language_code=None):
        self._crash_pt()
        await _real_sleep(0)
        if self.fail_get > 0:
            self.fail_get -= 1
            raise TelegramNetworkError(None, "getMyDescription: forced")
        if self._p("get_fail"):
            raise TelegramNetworkError(None, "getMyDescription: reset")
        return NS(description=self.desc.get(language_code, ""))

    async def download(self, file_id, destination):
        self._crash_pt()
        await _real_sleep(0)
        mode = self.fail_dl.pop(0) if self.fail_dl else None
        if mode is None:
            r, P = self.rng.random(), self.P
            a, b, c = P.get("dl_net", 0), P.get("dl_trunc", 0), P.get("dl_corrupt", 0)
            mode = "net" if r < a else "trunc" if r < a + b else "corrupt" if r < a + b + c else "ok"
        if file_id not in self.files:
            raise TelegramBadRequest(None, "Bad Request: wrong file_id")
        data = self.files[file_id]
        if mode == "net":
            raise TelegramNetworkError(None, "download: reset")
        if mode == "trunc":
            destination.write(data[: self.rng.randrange(0, max(1, len(data)))])
            return
        if mode == "corrupt":
            b = bytearray(data)
            b[self.rng.randrange(len(b))] ^= 0x41
            destination.write(bytes(b))
            return
        destination.write(data)

    async def delete_message(self, chat, mid):
        self._crash_pt()
        await _real_sleep(0)
        if (chat, mid) not in self.msgs or (chat, mid) in self.deleted:
            raise TelegramBadRequest(None, "Bad Request: message to delete not found")
        if self._p("del_fail"):
            raise TelegramNetworkError(None, "delete: reset")
        self.deleted.add((chat, mid))


def mk_session(path: Path, key: bytes):
    x = SQLiteSession(str(path.with_suffix("")))
    x.set_dc(2, "149.154.167.51", 443)
    x.auth_key = AuthKey(key)
    x.save()
    x.close()


def disk_strings(storage: SessionStorage) -> dict:
    out = {}
    for p in sorted(storage.dir.glob("*.session")):
        s = _orig_s2s(p)
        if s:
            out.setdefault(s, []).append(p.stem)
    return out


def snapshot_dir(d: Path) -> dict:
    return {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}


class World:
    def __init__(self, seed: int, n_events: int, only=None):
        self.seed = seed
        self.rng = random.Random(seed)
        r = self.rng
        lvl = r.choice([0.0, 0.03, 0.1, 0.25])
        self.P = {k: lvl * r.random() for k in
                  ("send_fail", "send_ambig", "set_retry", "set_fail", "set_ambig", "get_fail",
                   "dl_net", "dl_trunc", "dl_corrupt", "del_fail")}
        self.sql_p = r.choice([0, 0, 0, 0.05])
        self.bot = FakeBot(random.Random(seed * 7 + 1), self.P)
        self.root = Path(tempfile.mkdtemp(prefix=f"fz{seed}_", dir=TMP))
        self.uid = 777
        self.admins = {self.uid}
        self.hint_chats = [777, 778, 779]
        settings.sessions_backup_chat_id = r.choice([0, -1001])
        settings.target_chat_id = -100
        if r.random() < 0.3:
            self.bot.blocked.add(r.choice([777, -1001, -100]))
        self.alive: dict[str, dict] = {}
        self.removed: dict[str, float] = {}
        self.removed_ok: dict[str, bool] = {}
        self.removed_names: dict[str, set] = {}
        self.last_ok_seq = 0
        self.subs = {}
        self.violations = []
        self.trace = []
        self.n_events = n_events
        self.run_i = 0
        self.in_conc = False
        self.fresh = False
        self.shadow = set()
        self.seen = set()
        self.new_run(wipe=True)

    def V(self, inv, msg, **kw):
        inv = str(inv)
        if self.fresh and inv in ("1", "2", "1u"):
            inv += "-afterFRESH"
        if (inv, msg[:40]) in self.seen:
            return
        self.seen.add((inv, msg[:40]))
        self.violations.append({"seed": self.seed, "inv": inv, "msg": msg, "P": {k: round(v, 3) for k, v in self.P.items() if v},
                                "sql_p": self.sql_p, "trace": list(self.trace[-int(os.environ.get("TN", 14)):]), **kw})

    def new_run(self, wipe: bool):
        if wipe:
            self.run_i += 1
            self.dir = self.root / f"run{self.run_i}"
            self.dir.mkdir()
        self.storage = SessionStorage(self.dir, 1, "h" * 32)
        self.bk = B.SessionBackup(self.bot, self.storage, lambda: set(self.admins), self.admins.update)
        self.bk.subs_export = lambda: dict(self.subs)

        async def subs_import(d):
            return len(d)
        self.bk.subs_import = subs_import

        async def mexp():
            return {"items": [1, 2, 3]}

        async def mimp(s):
            return len(s.get("items", []))
        self.bk.market_export, self.bk.market_import = mexp, mimp
        self._wrap(self.bk)

    def _wrap(self, bk):
        world = self
        orig = bk._save

        async def _save(chat_hint=None, allow_shrink=False):
            before = len(world.bot.deliveries)
            res = await orig(chat_hint, allow_shrink)
            new = [d for d in world.bot.deliveries[before:] if d[3] == B.BACKUP_FILENAME]
            world.trace.append(f"  save(shrink={allow_shrink})->{res} sent={[(d[0], d[1]) for d in new]}"
                               + ("" if res else f" err={str(bk.last_error)[:60]!r}"))
            if len(new) > 1:
                world.V(4, f"one save() delivered the backup {len(new)}x to chats {[d[0] for d in new]} (ret {res})")
            if res:
                if not new:
                    world.V(3, "save() returned True without sending")
                else:
                    fid = new[-1][2]
                    world.last_ok_seq = max(world.last_ok_seq, world.bot.fseq[fid])
                    data = world.decode(fid)
                    inside = {it["string"] for it in data["sessions"]}
                    for s, meta in world.alive.items():
                        if s in inside:
                            meta["acked"] = True
                    # an account on disk that save() claimed to have saved but omitted
                    disk = disk_strings(bk.storage)
                    for s in disk:
                        if not world.in_conc and s in world.alive and s not in inside:
                            world.V("1s", f"save() returned True but omitted on-disk account {disk[s]}")
            return res
        bk._save = _save

    def decode(self, fid):
        raw = B._fernet().decrypt(self.bot.files[fid])
        return json.loads(__import__("zlib").decompress(raw[2:]) if raw[:2] == b"Z1" else raw)

    async def call(self, what, coro):
        try:
            return await coro
        except Crash:
            raise
        except BaseException as e:
            self.V(6, f"exception escaped {what}: {type(e).__name__}: {e}", tb=traceback.format_exc()[-800:])
            return None

    # ---------------------------------------------------------------- events (mirror main.py / admin.py)
    async def ev_add(self):
        bk = self.bk
        taken = set(bk.backup_names or set()) | set(bk.graveyard)
        name = self.storage.next_name()
        while name in taken:
            n = int(name.rsplit("_", 1)[1]) + 1
            name = f"session_{n:03d}"
        mk_session(self.storage.session_path(name), self.rng.randbytes(256))
        self.storage.save_meta(name, 1, "h" * 32, owner_id=self.uid, proxy="")
        s = _orig_s2s(self.storage.session_path(name))
        self.alive[s] = {"acked": False, "name": name}
        self.trace.append(f"add {name}")
        await self.call("save", bk.save(chat_hint=self.rng.choice(self.hint_chats)))

    async def ev_remove(self, auto=False):
        recs = self.storage.list_sessions()
        if not recs:
            return
        rec = self.rng.choice(recs)
        s = _orig_s2s(rec.path)
        ok = self.bk.bury(rec.name, "AUTH_KEY_UNREGISTERED" if auto else "удалён вручную")
        self.trace.append(f"{'autoremove' if auto else 'remove'} {rec.name} bury={ok}")
        self.storage.remove(rec.name)
        was = self.alive.pop(s, None)
        if was is not None and not was["acked"]:
            self.trace.append("  (removed account was never acked)")
        else:
            self.removed[s] = CLOCK.t
        self.removed_ok[s] = ok
        self.removed_names.setdefault(s, set()).add(rec.name)
        await self.call("save", self.bk.save(allow_shrink=True))

    async def ev_revive(self):
        if not self.bk.graveyard:
            return
        name = self.rng.choice(sorted(self.bk.graveyard))
        g = self.bk.graveyard[name]
        target = self.bk.unbury(name)
        self.trace.append(f"revive {name} -> {target}")
        if target:
            self.removed.pop(g["string"], None)
            self.alive[g["string"]] = {"acked": False, "name": target}
        await self.call("save", self.bk.save())

    async def ev_flags(self):
        k = self.rng.choice(["users_closed", "speeds", "mig1"])
        self.bk.flags[k] = self.rng.randrange(5)
        self.subs[str(self.rng.randrange(10))] = int(CLOCK.t)
        self.trace.append(f"flags {k}")
        await self.call("save", self.bk.save())

    async def ev_market(self):
        self.trace.append("save_market")
        await self.call("save_market", self.bk.save_market())

    async def ev_plain_save(self):
        self.trace.append("save")
        await self.call("save", self.bk.save())

    async def ev_concurrent(self):
        k = self.rng.randrange(2, 5)
        evs = [self.rng.choice([self.ev_add, self.ev_remove, self.ev_flags, self.ev_market, self.ev_plain_save])
               for _ in range(k)]
        self.trace.append(f"concurrent x{k}")
        self.in_conc = True
        try:
            res = await asyncio.gather(*(e() for e in evs), return_exceptions=True)
        finally:
            self.in_conc = False
        for r in res:
            if isinstance(r, Crash):
                raise r
            if isinstance(r, BaseException):
                self.V(6, f"exception in concurrent: {r!r}")

    async def ev_time(self):
        dt = self.rng.choice([60, 3600, 86400, 5 * 86400])
        CLOCK.t += dt
        self.trace.append(f"time +{dt}")

    async def deploy(self, wipe=True, crashed=False, clean=False):
        bot = self.bot
        bot.dead, bot.crash_p = False, 0.0
        self.trace.append(f"{'crash+' if crashed else ''}deploy wipe={wipe}")
        self.new_run(wipe=wipe)
        if not clean and self.rng.random() < 0.2:      # user dropped a .session into a fresh folder
            name = self.rng.choice(["session_001", "session_002", "session_050"])
            p = self.storage.session_path(name)
            if not p.exists():
                mk_session(p, self.rng.randbytes(256))
                self.storage.save_meta(name, 1, "h" * 32, owner_id=4242)
                self.alive[_orig_s2s(p)] = {"acked": False, "name": name}
                self.shadow.add(name)
                self.trace.append(f"manual-drop {name}")
        if not clean and self.rng.random() < 0.35:
            n = self.rng.choice([1, 3, 9, 10, 12, 30, 40])
            kind = self.rng.choice(["get", "dl_net", "dl_trunc", "dl_corrupt"])
            if kind == "get":
                bot.fail_get = n
            else:
                bot.fail_dl = [kind[3:]] * n
            self.trace.append(f"restore-fail {kind} x{n}")
        before = snapshot_dir(self.dir)
        await self.call("restore", self.bk.restore())
        bot.fail_get, bot.fail_dl = 0, []
        after = snapshot_dir(self.dir)
        for fn, data in before.items():
            if after.get(fn) != data:
                self.V(5, f"restore modified local file {fn}")
        had_ptr = bool(self.bot.desc.get("yo"))
        if self.bk.restore_done and had_ptr and self.bk.backup_names == set() and self.bk.restored_count == 0:
            self.fresh = True
            self.trace.append("  !! FRESH START (old copy abandoned)")
        self.trace.append(f"  restored={self.bk.restored_count} done={self.bk.restore_done}")
        if self.bk.restore_done:
            self.check_alive("after deploy")
        await self.call("sync_if_needed", self.bk.sync_if_needed())

    def check_alive(self, where):
        disk = disk_strings(self.storage)
        for s, meta in self.alive.items():
            if s not in disk:
                if meta["acked"] and meta["name"] in self.shadow:
                    self.V("1-shadow", f"{where}: acked account {meta['name']} lost; same name was manually dropped")
                elif meta["acked"]:
                    self.V(1, f"{where}: acked account {meta['name']} lost (not on disk)")
                else:
                    self.V("1u", f"{where}: never-acked account {meta['name']} lost")
        for s, names in disk.items():
            if len(names) > 1:
                self.V("dup", f"{where}: same auth key on disk under {names}")

    def check_quiescent(self):
        bk = self.bk
        disk = disk_strings(self.storage)
        grave = {g["string"] for g in bk.graveyard.values()}
        for s, ts in (self.removed.items() if bk.restore_done else ()):
            if CLOCK.t - ts < GRAVE - 3600 and s not in grave and s not in disk:
                ow = [n for n, g in bk.graveyard.items() if n in self.removed_names.get(s, ())]
                self.V("2-overwritten" if ow else 2, f"removed account (bury_ok={self.removed_ok.get(s)}) not revivable "
                          f"after {(CLOCK.t - ts) / 86400:.1f} d")
        for s, names in disk.items():
            if len(names) > 1:
                self.V("dup", f"same auth key on disk under {names}")
        if self.last_ok_seq:
            desc = self.bot.desc.get("yo", "")
            try:
                ptr = json.loads(desc[4:])["s"]
                fid = ptr[0]
                seq = self.bot.fseq[fid]
                B._fernet().decrypt(self.bot.files[fid])
                if seq < self.last_ok_seq:
                    self.V(3, f"pointer at seq {seq} older than last successful save {self.last_ok_seq}")
                if (ptr[1], ptr[2]) in self.bot.deleted:
                    self.V("3b", f"pointer points at a DELETED message {ptr[1:3]}")
            except Exception as e:
                self.V(3, f"pointer not decodable: {e!r} desc={desc[:80]}")

    async def run(self):
        SQL_FAIL["p"], SQL_FAIL["rng"] = self.sql_p, random.Random(self.seed * 13)
        await self.deploy(wipe=True)
        evs = [(self.ev_add, 5), (self.ev_remove, 2), (lambda: self.ev_remove(True), 1), (self.ev_revive, 2),
               (self.ev_flags, 3), (self.ev_market, 1), (self.ev_concurrent, 2), (self.ev_time, 2),
               ("deploy", 2), ("restart", 1), ("crash", 2)]
        fns, w = zip(*evs)
        for _ in range(self.n_events):
            e = self.rng.choices(fns, w)[0]
            try:
                if e == "deploy":
                    await self.deploy(wipe=True)
                elif e == "restart":
                    await self.deploy(wipe=False)
                elif e == "crash":
                    self.bot.crash_p = self.rng.choice([0.1, 0.3, 0.6])
                    sub = self.rng.choice([self.ev_add, self.ev_remove, self.ev_flags, self.ev_concurrent,
                                           self.ev_revive])
                    self.trace.append("arm-crash")
                    try:
                        await sub()
                    except Crash:
                        pass
                    self.bot.crash_p = 0
                    if self.bot.dead or self.rng.random() < 0.5:
                        await self.deploy(wipe=self.rng.random() < 0.8, crashed=True)
                else:
                    await e()
            except Crash:
                await self.deploy(wipe=True, crashed=True)
            self.check_quiescent()
        for k in self.P:
            self.P[k] = 0
        SQL_FAIL["p"] = 0
        await self.deploy(wipe=True, clean=True)
        if not self.bk.restore_done:
            self.V("deg", "final clean deploy could not restore")
        self.check_quiescent()
        shutil.rmtree(self.root, ignore_errors=True)
        return self.violations


def run_seed(seed, n_events=40):
    settings.bot_token, settings.session_backup_key, settings.old_bot_token = "123:abc", "", ""
    cfg.RUNTIME_SETTINGS_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "rs.json"
    CLOCK.t = 1_900_000_000.0
    w = World(seed, n_events)
    try:
        return asyncio.run(w.run())
    except Exception as e:
        return [{"seed": seed, "inv": "harness", "msg": repr(e), "tb": traceback.format_exc()[-1500:]}]


if __name__ == "__main__":
    os.makedirs(TMP, exist_ok=True)
    start, n = int(sys.argv[1]), int(sys.argv[2])
    ne = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    for seed in range(start, start + n):
        for v in run_seed(seed, ne):
            print(json.dumps(v, ensure_ascii=False), flush=True)
