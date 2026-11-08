"""Formatting policies; existing template variables are retained in formatter_core."""
from __future__ import annotations
from urllib.parse import quote
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from config import settings
from utils.links import listing_gift_link, normalize_username, username_link
from . import formatter_core as _core
from .formatter_core import *


def __getattr__(name):
    return getattr(_core, name)


def seller_url(listing):
    return username_link(listing.seller_username)


def listing_keyboard(l, with_seller=True):
    link = listing_gift_link(l)
    rows = [[InlineKeyboardButton(text="\u0417\u0430\u043d\u044f\u0442\u044c", callback_data=f"take:{l.gift_unique_id}"[:64])]]
    actions = []
    if link:
        actions.append(InlineKeyboardButton(text="\u041b\u043e\u0442", url=link))
    username = normalize_username(l.seller_username)
    if with_seller and username:
        url = username_link(username)
        trade_bot = normalize_username(getattr(settings, "trade_bot_username", ""))
        offer = _core._trade_offer(l) if trade_bot else None
        if trade_bot and link:
            # ссылка на NFT в оффере ВСЕГДА (раньше без рассчитанной цены оффер уходил пустым)
            if offer:
                amount, unit = offer
                text = f"@{trade_bot} {link} {amount} {unit}".rstrip()
            else:
                text = f"@{trade_bot} {link}"
            url += "?text=" + quote(text, safe="")
        actions.append(InlineKeyboardButton(text="\u041d\u0430\u043f\u0438\u0441\u0430\u0442\u044c", url=url))
    if actions:
        rows.append(actions)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class MessageFormatter(_core.MessageFormatter):
    def render(self, l):
        l.gift_link = listing_gift_link(l)
        l.seller_username = normalize_username(l.seller_username)
        l.seller_profile_link = username_link(l.seller_username)
        text = super().render(l)
        if l.gift_link:
            text += "\nNFT: " + l.gift_link
        return text
