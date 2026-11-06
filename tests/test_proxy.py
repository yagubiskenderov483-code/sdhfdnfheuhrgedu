"""Проверки utils.proxy.parse_proxy / normalize_proxy / mask_proxy.

Охват:
- базовые форматы (socks5/socks4/http, user:pass, no-auth);
- shorthand ip:port и ip:port:user:pass (частый формат у прокси-продавцов);
- URL-кодирование паролей со спецсимволами (@, :, %), чтобы urlparse не терял host;
- мусор → None;
- маскирование login/password.

Запуск: PYTHONPATH=. python tests/test_proxy.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.getcwd())
from utils.proxy import mask_proxy, normalize_proxy, parse_proxy


ok = 0


def check(cond, what):
    global ok
    if not cond:
        print(f"FAIL: {what}")
        sys.exit(1)
    ok += 1
    print(f"OK {what}")


def test_basic() -> None:
    p = parse_proxy("socks5://1.2.3.4:1080")
    check(p and p["addr"] == "1.2.3.4" and p["port"] == 1080, "socks5 без auth")
    check(p and p["username"] is None and p["password"] is None, "socks5 без auth: user/pass None")

    p = parse_proxy("socks5://user:pass@1.2.3.4:1080")
    check(p and p["username"] == "user" and p["password"] == "pass", "socks5 с auth")

    p = parse_proxy("http://u:p@proxy.local:8080")
    check(p and p["proxy_type"] == "http" and p["addr"] == "proxy.local", "http proxy")


def test_shorthand() -> None:
    p = parse_proxy("1.2.3.4:1080")
    check(p and p["addr"] == "1.2.3.4" and p["port"] == 1080, "ip:port → socks5")

    p = parse_proxy("1.2.3.4:1080:user:pass")
    check(p and p["username"] == "user" and p["password"] == "pass",
          "ip:port:user:pass → socks5 с auth")


def test_password_with_special_chars() -> None:
    """Регрессия: пароль с '@' ломал urlparse — прокси молча отбрасывался."""
    p = parse_proxy("1.2.3.4:1080:user:p@ss")
    check(p is not None, "ip:port:user:p@ss не отвергается (регрессия)")
    check(p and p["username"] == "user", "username корректен с паролем-с-@")
    check(p and p["password"] == "p@ss", "password с '@' декодируется обратно")
    check(p and p["addr"] == "1.2.3.4" and p["port"] == 1080, "host/port не поломался")

    # Пароль со слешем
    p = parse_proxy("1.2.3.4:1080:user:a/b")
    check(p and p["password"] == "a/b", "password с '/' декодируется")


def test_normalize_roundtrip() -> None:
    orig = "1.2.3.4:1080:user:p@ss"
    norm = normalize_proxy(orig)
    check(norm == "socks5://user:p%40ss@1.2.3.4:1080",
          f"normalize экранирует '@' в пароле → {norm}")
    # второй круг — нормализация уже нормализованной строки не ломает
    p = parse_proxy(norm)
    check(p and p["password"] == "p@ss", "round-trip: пароль восстанавливается")


def test_invalid() -> None:
    check(parse_proxy("") is None, "пустая строка → None")
    check(parse_proxy("   ") is None, "whitespace → None")
    check(parse_proxy(None) is None, "None → None")
    check(parse_proxy("# comment") is None, "комментарий → None")
    check(parse_proxy("http://host:abc") is None, "нечисловой порт → None")
    check(parse_proxy("ftp://host:21") is None, "неподдерживаемая схема → None")
    check(parse_proxy("1.2.3.4") is None, "без порта → None")
    check(parse_proxy("1:2:3:4:5") is None, "5 частей (неизвестный формат) → None")


def test_mask() -> None:
    check(mask_proxy("socks5://user:pass@1.2.3.4:1080") == "socks5://1.2.3.4:1080",
          "mask убирает логин/пароль")
    check(mask_proxy("") == "без прокси", "пустой → без прокси")
    check(mask_proxy("garbage") == "без прокси", "мусор → без прокси")


def main() -> None:
    test_basic()
    test_shorthand()
    test_password_with_special_chars()
    test_normalize_roundtrip()
    test_invalid()
    test_mask()
    print(f"\n=== {ok} tests PASSED ===")


if __name__ == "__main__":
    main()
