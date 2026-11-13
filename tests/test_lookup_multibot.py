"""Smoke-тесты новой multi-bot функциональности lookup.
Не требует Telegram — проверяет только логику выбора пула, dead-marks, cmd template.

Запуск:  PYTHONPATH=. python tests/test_lookup_multibot.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.getcwd())

from config import settings
settings.only_with_username = False   # старые сценарии мягкой зоны / EARLY_POST
import tracker.lookup as L

ok_count = 0


def check(cond, what):
    global ok_count
    if not cond:
        print("FAIL:", what)
        sys.exit(1)
    ok_count += 1
    print("OK", what)


class FakeWorker:
    def __init__(self, name, usable=True):
        self.name = name
        self._usable = usable
        self.speed = 1.0

    def is_usable(self):
        return self._usable


class FakeSessions:
    def __init__(self, workers):
        self.workers = {w.name: w for w in workers}

    def get(self, name):
        return self.workers.get(name)

    def usable(self, include_reserved=False):
        return [w for w in self.workers.values() if w.is_usable()]


def test_bot_pool_single():
    """Один primary, extras пуст → пул = [primary]."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = []
    lk = L.OwnerLookup(FakeSessions([]))
    check(lk._bot_pool() == ["funstat"], "bot_pool single primary")


def test_bot_pool_fallback():
    """Primary + extras → пул в правильном порядке, без дублей."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = ["infostat", "findtg", "funstat"]      # funstat дубль — должен отфильтроваться
    lk = L.OwnerLookup(FakeSessions([]))
    pool = lk._bot_pool()
    check(pool == ["funstat", "infostat", "findtg"], f"bot_pool order + dedup: got {pool}")


def test_bot_pool_dead_excluded():
    """Боты НИКОГДА не выпадают из пула (просьба админа): старая dead-метка игнорируется,
    молчавший бот (_bot_slow_until) просто спрашивается последним."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = ["infostat"]
    lk = L.OwnerLookup(FakeSessions([]))
    lk._bot_dead_until["funstat"] = time.monotonic() + 600
    check(lk._bot_pool() == ["funstat", "infostat"], f"dead-mark не выкидывает бота: {lk._bot_pool()}")
    lk._bot_slow_until["funstat"] = time.monotonic() + 600
    check(lk._bot_pool() == ["infostat", "funstat"], f"молчавший бот — последним: {lk._bot_pool()}")


def test_bot_pool_expired_dead_included():
    """Бот с истёкшей «медленной» меткой → снова первый."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = ["infostat"]
    lk = L.OwnerLookup(FakeSessions([]))
    lk._bot_slow_until["funstat"] = time.monotonic() - 100
    pool = lk._bot_pool()
    check(pool == ["funstat", "infostat"], f"bot_pool after slow expired: got {pool}")


def test_bot_cmd_default():
    """Без LOOKUP_CMDS — используется общий шаблон."""
    settings.lookup_cmd_template = "/human {name}"
    settings.lookup_cmds = {}
    lk = L.OwnerLookup(FakeSessions([]))
    check(lk._bot_cmd("funstat", "Иван") == "/human Иван", "default cmd")


def test_bot_cmd_per_bot():
    """LOOKUP_CMDS переопределяет для конкретного бота."""
    settings.lookup_cmd_template = "/human {name}"
    settings.lookup_cmds = {"infostat": "/find {name}"}
    lk = L.OwnerLookup(FakeSessions([]))
    check(lk._bot_cmd("funstat", "Ivan") == "/human Ivan", "funstat uses default")
    check(lk._bot_cmd("infostat", "Ivan") == "/find Ivan", "infostat uses override")
    check(lk._bot_cmd("INFOSTAT", "Ivan") == "/find Ivan", "case-insensitive lookup")


def test_bot_cmd_missing_placeholder():
    """Шаблон без {name} → добавляется через пробел."""
    settings.lookup_cmd_template = "/human"
    settings.lookup_cmds = {}
    lk = L.OwnerLookup(FakeSessions([]))
    check(lk._bot_cmd("x", "Ivan") == "/human Ivan", "placeholder auto-append")


def test_all_bots_dead_detection():
    """all_bots_dead() = True когда все настроенные боты в блеклисте."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = ["infostat"]
    lk = L.OwnerLookup(FakeSessions([]))
    check(not lk.all_bots_dead(), "not dead when pool is live")
    lk._bot_dead_until["funstat"] = time.monotonic() + 100
    check(not lk.all_bots_dead(), "not dead when only 1 of 2 is dead")
    lk._bot_dead_until["infostat"] = time.monotonic() + 100
    check(not lk.all_bots_dead(), "боты никогда не считаются мёртвыми — поиск идёт всегда")


def test_any_bot_configured():
    """any_bot_configured() учитывает и primary, и extras."""
    settings.lookup_bot_username = ""
    settings.lookup_bots = []
    lk = L.OwnerLookup(FakeSessions([]))
    check(not lk.any_bot_configured(), "no primary, no extras → not configured")
    settings.lookup_bots = ["infostat"]
    check(lk.any_bot_configured(), "only extras → configured")
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = []
    check(lk.any_bot_configured(), "only primary → configured")


def test_register_bot_unresolved_threshold():
    """3 РАЗНЫЕ сессии с unresolvable → бот уходит в конец очереди (slow-mark), dead-mark не ставится:
    по просьбе админа боты из работы не выпадают."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = []
    settings.lookup_sessions = []
    lk = L.OwnerLookup(FakeSessions([]))
    lk._register_bot_unresolved("funstat", "s1")
    lk._register_bot_unresolved("funstat", "s1")
    check("funstat" not in lk._bot_slow_until, "2 фейла с ОДНОЙ сессии — ещё не slow")
    lk._register_bot_unresolved("funstat", "s2")
    lk._register_bot_unresolved("funstat", "s3")
    check("funstat" in lk._bot_slow_until and "funstat" not in lk._bot_dead_until,
          "3 разные сессии → slow-mark, но не dead")


def test_clear_bot_unresolved():
    """После успеха метка снимается."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = []
    settings.lookup_sessions = []
    lk = L.OwnerLookup(FakeSessions([]))
    for s_ in ("s1", "s2", "s3"):
        lk._register_bot_unresolved("funstat", s_)
    check("funstat" in lk._bot_slow_until, "pre: slow")
    lk._clear_bot_unresolved("funstat")
    check("funstat" not in lk._bot_slow_until, "clear: slow-mark removed")
    check(lk._bot_consec_unres.get("funstat", 0) == 0, "clear: counter reset")


def test_pick_sessions_fallback_when_want_dead():
    """Выделенный LOOKUP_SESSIONS пул dead → fallback на любые usable."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = []
    settings.lookup_sessions = ["ded1", "ded2"]        # эти специально отмечены dead ниже
    workers = [FakeWorker("ded1", usable=False),       # dead
               FakeWorker("ded2", usable=False),
               FakeWorker("live1", usable=True),       # fallback должен выбрать
               FakeWorker("live2", usable=True)]
    lk = L.OwnerLookup(FakeSessions(workers))
    order = lk._pick_sessions()
    check(set(order) == {"live1", "live2"}, f"fallback picked live sessions: {order}")


def test_pick_sessions_unresolvable_for_primary_only():
    """Сессия отмечена unresolvable для primary, но fallback-бот доступен →
    сессия остаётся в выборке (регрессия: b9768a5)."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = ["infostat"]
    settings.lookup_sessions = ["s1", "s2"]
    workers = [FakeWorker("s1", usable=True), FakeWorker("s2", usable=True)]
    lk = L.OwnerLookup(FakeSessions(workers))
    # s1 помечена unresolvable только для funstat — но infostat у неё работает
    lk._bot_unresolvable[("s1", "funstat")] = time.monotonic()
    order = lk._pick_sessions()
    check("s1" in order, f"s1 остаётся — unres только на primary, fallback свободен: {order}")
    check("s2" in order, f"s2 без меток, тоже в выборке: {order}")


def test_pick_sessions_unresolvable_for_all_bots():
    """Сессия unresolvable для ВСЕХ активных ботов → исключена."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = ["infostat"]
    settings.lookup_sessions = ["s1", "s2"]
    workers = [FakeWorker("s1", usable=True), FakeWorker("s2", usable=True)]
    lk = L.OwnerLookup(FakeSessions(workers))
    now = time.monotonic()
    lk._bot_unresolvable[("s1", "funstat")] = now
    lk._bot_unresolvable[("s1", "infostat")] = now
    order = lk._pick_sessions()
    check("s1" not in order, f"s1 unres ко всем ботам пула — исключена: {order}")
    check("s2" in order, f"s2 без меток — осталась: {order}")


def test_pick_sessions_primary_dead_fallback_bot_alive():
    """Primary в глобальном dead-mark, сессия unres к primary — но fallback-бот жив
    и сессия к нему не unres → сессия нужна (scenario из коммита b9768a5)."""
    settings.lookup_bot_username = "funstat"
    settings.lookup_bots = ["infostat"]
    settings.lookup_sessions = ["s1"]
    workers = [FakeWorker("s1", usable=True)]
    lk = L.OwnerLookup(FakeSessions(workers))
    now = time.monotonic()
    lk._bot_dead_until["funstat"] = now + 900     # primary dead-marked
    lk._bot_unresolvable[("s1", "funstat")] = now  # и у сессии метка на primary
    # В пуле остался только infostat; s1 к нему не unres → должна остаться
    lk._bot_slow_until["funstat"] = now + 600     # «молчащий» бот не выкидывается — идёт последним
    check(lk._bot_pool() == ["infostat", "funstat"], "молчащий primary — в конце пула, не выкинут")
    order = lk._pick_sessions()
    check("s1" in order, f"s1 остаётся: primary dead, fallback жив, сессия к fallback'у ок: {order}")


def main():
    test_bot_pool_single()
    test_bot_pool_fallback()
    test_bot_pool_dead_excluded()
    test_bot_pool_expired_dead_included()
    test_bot_cmd_default()
    test_bot_cmd_per_bot()
    test_bot_cmd_missing_placeholder()
    test_all_bots_dead_detection()
    test_any_bot_configured()
    test_register_bot_unresolved_threshold()
    test_clear_bot_unresolved()
    test_pick_sessions_fallback_when_want_dead()
    test_pick_sessions_unresolvable_for_primary_only()
    test_pick_sessions_unresolvable_for_all_bots()
    test_pick_sessions_primary_dead_fallback_bot_alive()
    print(f"\n=== {ok_count} tests PASSED ===")


if __name__ == "__main__":
    main()
