"""Canonical Telegram links; never invent a gift slug or a username."""
from __future__ import annotations
import re
from urllib.parse import unquote, urlsplit

_SLUG = re.compile(r"[A-Za-z][A-Za-z0-9]*-[1-9][0-9]*\Z")
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\Z")


def gift_link(slug: str | None) -> str | None:
    value = (slug or "").strip()
    if value.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
            if parsed.hostname not in {"t.me", "telegram.me"} or not parsed.path.startswith("/nft/"):
                return None
            value = unquote(parsed.path[5:]).rstrip("/")
        except ValueError:
            return None
    return "https://t.me/nft/" + value if _SLUG.fullmatch(value) else None


def listing_gift_link(listing) -> str | None:
    return gift_link(getattr(listing, "gift_slug", None)) or gift_link(getattr(listing, "gift_link", None))


def normalize_username(username: str | None) -> str | None:
    value = (username or "").strip().lstrip("@")
    return value if _USERNAME.fullmatch(value) else None


def username_link(username: str | None) -> str | None:
    value = normalize_username(username)
    return "https://t.me/" + value if value else None


def user_id_deeplink(user_id: int | None) -> str | None:
    return f"tg://user?id={user_id}" if user_id and user_id > 0 else None
