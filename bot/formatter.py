"""
Форматирование сообщения по шаблону templates/listing.html (Telegram HTML parse mode).

Шаблон можно полностью менять. Доступные переменные ({имя}):
  gift_name, gift_title_link, gift_link, gift_num, gift_slug, gift_slug_link, detected_at,
  model, backdrop, symbol,
  price_line, stars_price, ton_price, currency,
  seller_line, seller_username, seller_id, seller_name, seller_profile, seller_check,
  level, messages, premium_status, listing_id, first_seen, session_source
Все значения уже экранированы для HTML. Неизвестная переменная → пустая строка.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import settings
from database.models import Listing

DASH = "—"


def _e(v) -> str:
    return html.escape(str(v)) if v not in (None, "") else DASH


def fmt_ton(ton: float | None) -> str | None:
    if ton is None:
        return None
    s = f"{ton:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def detected_at(l: Listing) -> str:
    try:
        dt = datetime.fromisoformat(l.first_seen)
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    try:
        dt = dt.astimezone(ZoneInfo(settings.timezone))
    except Exception:
        pass
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def seller_url(l: Listing) -> str | None:
    if l.seller_profile_link:
        return l.seller_profile_link
    if l.seller_type == "user" and l.seller_id:
        return f"tg://user?id={l.seller_id}"
    return None


def listing_keyboard(l: Listing, with_seller: bool = True) -> InlineKeyboardMarkup:
    """Кнопки: [✓ Занять] / [🎁 Открыть лот] [👤 Продавец]"""
    rows = [[InlineKeyboardButton(text="✓ Занять", callback_data=f"take:{l.gift_unique_id}"[:64])]]
    second = []
    if l.gift_link:
        second.append(InlineKeyboardButton(text="🎁 Открыть лот", url=l.gift_link))
    url = seller_url(l) if with_seller else None
    if url:
        second.append(InlineKeyboardButton(text="👤 Продавец", url=url))
    if second:
        rows.append(second)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def price_line(l: Listing) -> str:
    parts = []
    if l.stars_price is not None:
        parts.append(f"{l.stars_price} Stars")
    if l.ton_price is not None:
        parts.append(f"{fmt_ton(l.ton_price)} {settings.ton_label}")
    return " / ".join(parts) if parts else DASH


class _Safe(dict):
    def __missing__(self, key):
        return ""


class MessageFormatter:
    def __init__(self, template_path: Path):
        self.template_path = template_path
        self.template = ""
        self.reload()

    def reload(self) -> None:
        self.template = self.template_path.read_text(encoding="utf-8").strip()

    def variables(self, l: Listing) -> dict[str, str]:
        gift_name = l.gift_name
        if l.gift_link:
            gift_title_link = f'<a href="{html.escape(l.gift_link)}">{html.escape(gift_name)}</a>'
        else:
            gift_title_link = html.escape(gift_name)

        if l.seller_username:
            handle = f"@{l.seller_username}"
            seller_line = f'<a href="{html.escape(l.seller_profile_link or "")}">{html.escape(handle)}</a>'
        elif l.seller_type == "user" and l.seller_id:
            seller_line = f'<a href="tg://user?id={l.seller_id}">{html.escape(l.seller_name or "user")}</a>'
        elif l.seller_type == "address":
            seller_line = "TON-кошелёк"
        else:
            seller_line = html.escape(l.seller_name or DASH)
        if l.seller_id:
            seller_line += f" ({l.seller_id})"

        if l.seller_profile_link:
            seller_profile = html.escape(l.seller_profile_link)
        elif l.seller_id:
            seller_profile = f"ID {l.seller_id}"
        else:
            seller_profile = DASH

        premium_status = DASH if l.premium is None else ("Premium" if l.premium else "без Premium")
        if l.gift_link and l.gift_slug:
            gift_slug_link = f'<a href="{html.escape(l.gift_link)}">{html.escape(l.gift_slug)}</a>'
        else:
            gift_slug_link = _e(l.gift_slug)

        return _Safe(
            gift_name=_e(gift_name),
            gift_title_link=gift_title_link,
            gift_link=_e(l.gift_link),
            gift_num=_e(l.gift_num),
            gift_slug=_e(l.gift_slug),
            gift_slug_link=gift_slug_link,
            detected_at=html.escape(detected_at(l)),
            model=_e(l.model),
            backdrop=_e(l.backdrop),
            symbol=_e(l.symbol),
            price_line=html.escape(price_line(l)),
            stars_price=_e(l.stars_price),
            ton_price=_e(fmt_ton(l.ton_price)),
            currency=_e(l.currency),
            seller_line=seller_line,
            seller_username=_e(f"@{l.seller_username}" if l.seller_username else None),
            seller_id=_e(l.seller_id),
            seller_name=_e(l.seller_name),
            seller_profile=seller_profile,
            seller_check=_e(l.seller_filter_result),
            level=_e(l.level),
            messages=_e(l.messages),
            premium_status=premium_status,
            listing_id=_e(l.listing_id),
            first_seen=_e(l.first_seen),
            session_source=_e(l.session_source),
        )

    def render(self, l: Listing) -> str:
        return self.template.format_map(self.variables(l))
