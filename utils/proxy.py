"""
Прокси для аккаунтов (Telethon + python-socks).

Форматы строки:
  socks5://user:pass@1.2.3.4:1080
  socks5://1.2.3.4:1080
  http://user:pass@1.2.3.4:8080
  1.2.3.4:1080:user:pass          (частый формат у продавцов прокси — считается socks5)
  1.2.3.4:1080
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlparse

_SCHEMES = {"socks5", "socks4", "http"}


def parse_proxy(line: str | None) -> dict | None:
    """→ dict для TelegramClient(proxy=...) или None, если строка пустая/некорректная."""
    if not line:
        return None
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "://" not in s:
        parts = s.split(":")
        if len(parts) == 2:
            s = f"socks5://{parts[0]}:{parts[1]}"
        elif len(parts) == 4:
            s = f"socks5://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        else:
            return None
    try:
        u = urlparse(s)
        port = u.port           # бросает ValueError на нечисловом порту (напр. host:abc)
    except ValueError:
        return None
    if u.scheme.lower() not in _SCHEMES or not u.hostname or not port:
        return None
    return {
        "proxy_type": u.scheme.lower(),
        "addr": u.hostname,
        "port": int(port),
        "username": unquote(u.username) if u.username else None,
        "password": unquote(u.password) if u.password else None,
        "rdns": True,
    }


def normalize_proxy(line: str | None) -> str | None:
    """Приводит к виду scheme://[user:pass@]host:port (для хранения) или None."""
    p = parse_proxy(line)
    if not p:
        return None
    auth = ""
    if p["username"]:
        auth = quote(p["username"], safe="") + (f":{quote(p['password'], safe='')}" if p["password"] else "") + "@"
    host = f"[{p['addr']}]" if ":" in p["addr"] else p["addr"]       # IPv6
    return f"{p['proxy_type']}://{auth}{host}:{p['port']}"


def mask_proxy(line: str | None) -> str:
    """Для показа в боте — без логина и пароля."""
    p = parse_proxy(line)
    if not p:
        return "без прокси"
    return f"{p['proxy_type']}://{p['addr']}:{p['port']}"


def split_lines(text: str) -> list[str]:
    return [x for x in (normalize_proxy(l) for l in re.split(r"[\r\n]+", text or "")) if x]
