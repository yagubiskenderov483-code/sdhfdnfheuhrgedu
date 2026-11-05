"""Smoke-тест HTTP health endpoint. Не стартует реальный сервер, только проверяет
что модуль импортируется и health_port_from_env парсит env правильно.

Запуск: PYTHONPATH=. python tests/test_health_server.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

ok_count = 0


def check(cond, what):
    global ok_count
    if not cond:
        print("FAIL:", what)
        sys.exit(1)
    ok_count += 1
    print("OK", what)


def test_health_port_default():
    """HEALTH_PORT не задан → 0 (endpoint выключен)."""
    os.environ.pop("HEALTH_PORT", None)
    from utils.health_server import health_port_from_env
    check(health_port_from_env() == 0, "unset HEALTH_PORT → 0")


def test_health_port_valid():
    """HEALTH_PORT=8080 → 8080."""
    from utils.health_server import health_port_from_env
    os.environ["HEALTH_PORT"] = "8080"
    check(health_port_from_env() == 8080, "HEALTH_PORT=8080")
    os.environ["HEALTH_PORT"] = "10000"
    check(health_port_from_env() == 10000, "HEALTH_PORT=10000")


def test_health_port_invalid():
    """Невалидные значения → 0."""
    from utils.health_server import health_port_from_env
    for v in ("", "abc", "-1", "99999999", "0", " "):
        os.environ["HEALTH_PORT"] = v
        check(health_port_from_env() == 0, f"HEALTH_PORT={v!r} → 0")
    os.environ.pop("HEALTH_PORT", None)


async def test_start_server_disabled():
    """start_health_server с port=0 возвращает None (не падает)."""
    from utils.health_server import start_health_server
    class FakeApp: pass
    res = await start_health_server(FakeApp(), 0)
    check(res is None, "start_health_server(port=0) → None")


def test_import():
    """Модуль импортируется без ошибок."""
    import utils.health_server as hs
    check(hasattr(hs, "start_health_server"), "start_health_server exists")
    check(hasattr(hs, "health_port_from_env"), "health_port_from_env exists")


def main():
    test_import()
    test_health_port_default()
    test_health_port_valid()
    test_health_port_invalid()
    asyncio.run(test_start_server_disabled())
    print(f"\n=== {ok_count} tests PASSED ===")


if __name__ == "__main__":
    main()
