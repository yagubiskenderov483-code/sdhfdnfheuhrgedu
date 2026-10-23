"""
Форматирование сообщения по шаблону templates/listing.html (Telegram HTML parse mode).

Шаблон можно полностью менять. Доступные переменные ({имя}):
  gift_name, gift_title_link, gift_link, gift_num, gift_slug, gift_slug_link, detected_at,
  model, backdrop, symbol,
  price_line, floor_line, stars_price, ton_price, currency,
  seller_line, seller_username, seller_id, seller_name, seller_profile, seller_check,
  level, messages, premium_status, seller_nfts, listing_id, first_seen, session_source
Все значения уже экранированы для HTML. Неизвестная переменная → пустая строка.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import settings
from database.models import Listing

DASH = "-"


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
    return dt.strftime("%H:%M:%S · %d.%m.%Y")


def seller_url(l: Listing) -> str | None:
    if l.seller_profile_link:
        return l.seller_profile_link
    if l.seller_type == "user" and l.seller_id:
        return f"tg://user?id={l.seller_id}"
    return None


def _trade_offer(l: Listing) -> tuple[int, str] | None:
    """Сумма и единица для оффера в торговый бот.
    Правила скидки от текущей цены лота (по требованию админа):
      Stars: ≤700 → −35; 701–1800 → −50; 1801–2500 → −60; 2501–5000 → −100;
             5001–10000 → −140; 10001+ → −215.
      TON (целых): <48 → −1 g; ≥48 → −2 g.
    Для ton-only лотов используется TON, иначе — Stars. Если ни того ни другого — None."""
    stars = l.stars_price
    ton_int = int(l.ton_price) if l.ton_price else 0
    if getattr(l, "ton_only", False) and ton_int > 0:
        off = ton_int - (2 if ton_int >= 48 else 1)
        return (max(1, off), "g")
    if stars and stars > 0:
        if stars <= 700:
            d = 35
        elif stars <= 1800:
            d = 50
        elif stars <= 2500:
            d = 60
        elif stars <= 5000:
            d = 100
        elif stars <= 10000:
            d = 140
        else:
            d = 215
        return (max(1, stars - d), "")
    if ton_int > 0:
        off = ton_int - (2 if ton_int >= 48 else 1)
        return (max(1, off), "g")
    return None


def listing_keyboard(l: Listing, with_seller: bool = True) -> InlineKeyboardMarkup:
    """Кнопки: [Занять] / [Лот] [Написать] / [🔍 Найти по имени].
    «Написать» ведёт либо на профиль продавца (стандарт), либо, когда задан TRADE_BOT_USERNAME,
    на торговый бот с ГОТОВЫМ оффером «@<бот> <ссылка на NFT> <сниженная_цена>» в input'е."""
    rows = [[InlineKeyboardButton(text="Занять", callback_data=f"take:{l.gift_unique_id}"[:64])]]
    second = []
    if l.gift_link:
        second.append(InlineKeyboardButton(text="Лот", url=l.gift_link))
    # «Написать»: либо торговый бот (приоритет), либо профиль продавца
    trade_bot = (getattr(settings, "trade_bot_username", "") or "").lstrip("@")
    offer = _trade_offer(l) if trade_bot else None
    if trade_bot and offer and l.gift_link:
        amount, unit = offer
        tail = f" {amount} {unit}".rstrip()
        text = f"@{trade_bot} {l.gift_link}{tail}"
        second.append(InlineKeyboardButton(text="Написать",
                                           url=f"https://t.me/{trade_bot}?text={quote(text, safe='/')}"))
    else:
        url = seller_url(l) if with_seller else None
        if url:
            second.append(InlineKeyboardButton(text="Написать", url=url))
    # Для лотов со скрытым Telegram-ом владельцем — кнопка открывает бота поиска
    # (например, funstat) с ПРЕДЗАПОЛНЕННЫМ текстом «/human <имя>».
    # URL-формат  t.me/<bot>?text=<cmd>  поддерживается большинством клиентов (Desktop, Android,
    # большинство версий iOS) — текст автоматически попадает в input, админу остаётся нажать «Отправить».
    # На клиенте, где не предзаполнит, бот всё равно откроется — имя всегда можно скопировать
    # из <code>…</code> в самом сообщении.
    if with_seller and not seller_url(l) and l.seller_name and getattr(settings, "lookup_bot_username", ""):
        bot = settings.lookup_bot_username.lstrip("@")
        cmd = f"/human {l.seller_name}"
        second.append(InlineKeyboardButton(text="🔍 Клик",
                                           url=f"https://t.me/{bot}?text={quote(cmd, safe='/')}"))
    if second:
        rows.append(second)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _n(v: int) -> str:
    """1250 → «1 250»"""
    return f"{int(v):,}".replace(",", " ")


def floor_line(l: Listing) -> str:
    """Флор коллекции и насколько цена лота от него отличается."""
    if not l.floor_stars:
        return DASH
    out = f"{_n(l.floor_stars)} ⭐"
    if l.stars_price:
        diff = (l.stars_price - l.floor_stars) / l.floor_stars * 100
        pct = int(abs(diff) + 0.5)              # обычное округление (не «банковское»: 2.5 → 3)
        if pct == 0:
            out += " - по флору"
        else:
            out += f" - {'+' if diff > 0 else '−'}{pct}% к флору"
    return out


def price_line(l: Listing) -> str:
    if getattr(l, "ton_only", False) and l.ton_price is not None:
        return f"{fmt_ton(l.ton_price)} {settings.ton_label} (только TON)"
    parts = []
    if l.stars_price is not None:
        parts.append(f"{_n(l.stars_price)} ⭐")
    if l.ton_price is not None:
        parts.append(f"{fmt_ton(l.ton_price)} {settings.ton_label}")
    return " / ".join(parts) if parts else DASH


def _msgs(v: str | None) -> str | None:
    if not v:
        return None
    return {"бесплатно": "ЛС открыты", "только Premium": "ЛС только с Premium"}.get(v, f"ЛС {v}")


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
        gift_name = l.gift_name or DASH        # None бы уронил html.escape → лот не отправился бы
        if l.gift_link:
            gift_title_link = f'<a href="{html.escape(l.gift_link)}">{html.escape(gift_name)}</a>'
        else:
            gift_title_link = html.escape(gift_name)

        if l.seller_username:
            handle = f"@{l.seller_username}"
            seller_line = f'<a href="{html.escape(l.seller_profile_link or f"https://t.me/{l.seller_username}")}">{html.escape(handle)}</a>'
            if l.owner_found_via:
                seller_line += f" (найден через @{html.escape(l.owner_found_via)}, NFT в профиле ✅)"
        elif l.seller_type == "user" and l.seller_id:
            # без @username — ссылка на профиль по ID и сам ID (можно скопировать и найти)
            seller_line = (f'<a href="tg://user?id={l.seller_id}">{html.escape(l.seller_name or "профиль")}</a>'
                           f' · ID <code>{l.seller_id}</code>')
            if l.owner_found_via:
                seller_line += f" (найден через @{html.escape(l.owner_found_via)}, NFT в профиле ✅)"
        elif l.seller_type == "address":
            seller_line = "TON-кошелёк"
        elif l.seller_type in ("channel", "chat") and l.seller_name:
            seller_line = html.escape(l.seller_name) + " (канал без ссылки)"
        elif l.seller_name:
            # Скрытый Telegram-ом владелец: имя идёт как <code> — один тап копирует его в буфер,
            # дальше можно вручную найти профиль через любой поиск (глобальный Telegram, бот поиска и т.п.)
            seller_line = f"<code>{html.escape(l.seller_name)}</code> (скрыт Telegram — скопируй имя)"
        else:
            seller_line = DASH

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

        attrs = " · ".join(html.escape(str(x)) for x in (l.model, l.backdrop, l.symbol) if x)
        meta = []
        if l.level not in (None, ""):
            meta.append(f"уровень {html.escape(str(l.level))}")
        if l.seller_nfts not in (None, ""):
            meta.append(f"NFT в профиле: {html.escape(str(l.seller_nfts))}")
        if _msgs(l.messages):
            meta.append(html.escape(_msgs(l.messages)))
        if l.premium:
            meta.append("Premium")
        detect_line = (f"\n⚡ Обнаружен за {l.detect_secs:.2f} сек"
                       if l.detect_secs is not None else "")
        deal_badge = ""
        if l.floor_stars and l.stars_price and l.stars_price < l.floor_stars:
            off = (l.floor_stars - l.stars_price) / l.floor_stars * 100
            if int(off + 0.5) >= 5:              # как в строке флора: «−5%» всегда с 🔥
                deal_badge = "  🔥"          # выгодный лот: ниже флора на 5%+
        return _Safe(
            deal_badge=deal_badge,
            detect_line=detect_line,
            attrs=attrs,
            seller_meta=(lambda t: t[:1].upper() + t[1:])(", ".join(meta)) if meta else "",
            gift_name=_e(gift_name),
            gift_title_link=gift_title_link,
            gift_link=_e(l.gift_link),
            gift_num=_e(l.gift_num),
            gift_slug=_e(l.gift_slug),
            gift_slug_link=gift_slug_link,
            detected_at=html.escape(detected_at(l)),
            model=_e(l.model) + (f" ({html.escape(l.model_rarity)})" if l.model_rarity else ""),
            backdrop=_e(l.backdrop) + (f" ({html.escape(l.backdrop_rarity)})" if l.backdrop_rarity else ""),
            symbol=_e(l.symbol) + (f" ({html.escape(l.symbol_rarity)})" if l.symbol_rarity else ""),
            price_line=html.escape(price_line(l)),
            floor_line=html.escape(floor_line(l)),
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
            seller_nfts=_e(l.seller_nfts),
            messages=_e(l.messages),
            premium_status=premium_status,
            listing_id=_e(l.listing_id),
            first_seen=_e(l.first_seen),
            session_source=_e(l.session_source),
        )

    def render(self, l: Listing) -> str:
        return self.template.format_map(self.variables(l))
