"""
Ссылки. Используются только реальные форматы Telegram:

  https://t.me/nft/<slug>   — deep link на коллекционный подарок (slug приходит из API в starGiftUnique.slug)
  https://t.me/<username>   — публичный username (приходит из API в User.username / Channel.username)
  tg://user?id=<id>         — ссылка на пользователя по ID. Работает в клиентах Telegram, только если
                              у читателя есть доступ к этому пользователю (зависит от приватности).
Username никогда не генерируется «из головы».
"""
from __future__ import annotations

import re


def gift_slug(slug: str | None, title: str | None = None, num: int | None = None) -> str | None:
    """slug из API; если API его не отдал — собираем по правилу Telegram: «Lol Pop» #40171 → LolPop-40171.
    Без этого у части лотов не было ссылки на NFT (ни в заголовке, ни в кнопке «Лот», ни в оффере)."""
    if slug:
        return slug
    base = re.sub(r"[^A-Za-z0-9]", "", title or "")
    return f"{base}-{int(num)}" if base and num else None


def gift_link(slug: str | None) -> str | None:
    return f"https://t.me/nft/{slug}" if slug else None


def username_link(username: str | None) -> str | None:
    return f"https://t.me/{username}" if username else None


def user_id_deeplink(user_id: int | None) -> str | None:
    # Negative user_id — наши псевдо-ID (хеш username), tg:// их не откроет
    return f"tg://user?id={user_id}" if (user_id and user_id > 0) else None
