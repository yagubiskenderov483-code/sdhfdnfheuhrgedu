"""Кастомные Premium-эмодзи для кнопок (Bot API: InlineKeyboardButton.icon_custom_emoji_id).

ID присланы Maker'ом (те же, что в тексте сообщений). Выключить иконки в кнопках:
BUTTON_EMOJI=false. Если Telegram отклонит кнопки с иконками, main._html_fallback
переотправит сообщение без них — пост/меню не потеряется."""
from __future__ import annotations

from config import settings

DIAMOND = "6032742198179532882"
STAR = "6028338546736107668"
TON = "5424905554432631390"          # Maker: эмодзи TON
GIFT = "6032644646587338669"
PAID = "6039486778597970865"
SUB = "5927118708873892465"
PERSON = "6035084557378654059"       # «Занять» / «Отменить»
PENCIL = "6039614175917903752"       # «Написать»
PROFILE = "5904630315946611415"      # «Оформить подписку»
SHOP = "5920332557466997677"         # «Реферальная система»
TAG = "5890883384057533697"          # «Промокод»
CLOUD = "5884330496619450755"        # «Испытать удачу»


def icon(eid: str | None) -> dict:
    """kwargs для InlineKeyboardButton: {'icon_custom_emoji_id': id} или {}."""
    if eid and getattr(settings, "button_emoji", True):
        return {"icon_custom_emoji_id": eid}
    return {}
