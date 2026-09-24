"""
Ссылки. Используются только реальные форматы Telegram:

  https://t.me/nft/<slug>   — deep link на коллекционный подарок (slug приходит из API в starGiftUnique.slug)
  https://t.me/<username>   — публичный username (приходит из API в User.username / Channel.username)
  tg://user?id=<id>         — ссылка на пользователя по ID. Работает в клиентах Telegram, только если
                              у читателя есть доступ к этому пользователю (зависит от приватности).
Username никогда не генерируется «из головы».
"""
from __future__ import annotations


def gift_link(slug: str | None) -> str | None:
    return f"https://t.me/nft/{slug}" if slug else None


def username_link(username: str | None) -> str | None:
    return f"https://t.me/{username}" if username else None


def user_id_deeplink(user_id: int | None) -> str | None:
    return f"tg://user?id={user_id}" if user_id else None
