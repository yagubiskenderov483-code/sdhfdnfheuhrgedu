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
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import settings
from database.models import Listing
from utils import emoji as E
from utils.emoji import icon as _icon

DASH = "-"
# кастомные эмодзи в тексте лота (Maker)
STAR_TG = '<tg-emoji emoji-id="6028338546736107668">⭐️</tg-emoji>'
BOLT_TG = '<tg-emoji emoji-id="5936088245560419529">⚡️</tg-emoji>'
TON_TG = '<tg-emoji emoji-id="5956405141677548170">💎</tg-emoji>'  # цена в TON (раньше была обычным словом «TON» → в теме 5000+ смотрелось не как кастом)


def _units(s: str) -> str:
    """Заменяем единицы цены на кастомные эмодзи Maker'а: ⭐ и метку TON.
    Применяется к уже экранированному price_line / floor_line.
    TON меняем только когда он стоит единицей после числа (напр. «18.72 TON»),
    чтобы не задеть пояснение «(только TON)»."""
    s = s.replace("⭐", STAR_TG)
    label = html.escape(settings.ton_label)
    if label:
        s = re.sub(r"(?<=\d)(\s*)" + re.escape(label) + r"\b", r"\1" + TON_TG, s)
    return s


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
    if l.seller_type == "user" and l.seller_id and l.seller_id > 0:
        return f"tg://user?id={l.seller_id}"
    return None


def _ton_discount(ton: int) -> int:
    """Скидка в граммах по сумме в TON (Maker): <25 → −1; 25–49 → −2; 50–80 → −3; 80+ → −5."""
    if ton < 25:
        return 1
    if ton < 50:
        return 2
    if ton <= 80:
        return 3
    return 5


def _stars_discount(stars: int) -> int:
    """Скидка в ⭐ по цене лота (Maker):
      ≤1000 → −30 (1000→970); 1001–2000 → −50; 2001–3500 → −120;
      3501–5000 → −170; 5001–10000 → −250; 10001+ → −350."""
    if stars <= 1000:
        return 30
    if stars <= 2000:
        return 50
    if stars <= 3500:
        return 120
    if stars <= 5000:
        return 170
    if stars <= 10000:
        return 250
    return 350


def _trade_offer(l: Listing) -> tuple[int, str] | None:
    """Сумма и единица для оффера в торговый бот со СКИДКОЙ от текущей цены лота.
    Для ton-only лотов используется TON (граммы), иначе — Stars. Если ни того ни другого — None."""
    stars = l.stars_price
    ton_int = 0
    if l.ton_price and l.ton_price > 0:
        # до ближайшего целого (7.4→7, 7.5→8) — чтобы оффер в TON не дробный
        from decimal import Decimal, ROUND_HALF_UP
        ton_int = max(1, int(Decimal(str(l.ton_price)).quantize(Decimal("1"),
                                                                  rounding=ROUND_HALF_UP)))
    if getattr(l, "ton_only", False) and ton_int > 0:
        return (max(1, ton_int - _ton_discount(ton_int)), "g")
    if stars and stars > 0:
        return (max(1, stars - _stars_discount(stars)), "")
    if ton_int > 0:
        return (max(1, ton_int - _ton_discount(ton_int)), "g")
    return None


def offer_message(l: Listing, amount: tuple[int, str] | None = None) -> str | None:
    """Текст оффера, который подставится в поле ввода.
    • TRADE_BOT_USERNAME задан → «@бот <ссылка> <цена>» (инлайн-запрос торгового бота).
    • не задан → обычное сообщение продавцу «Здравствуйте! Готов забрать <ссылка> за N ⭐».
    Раньше без торгового бота оффер не собирался вообще, и '/<id> оффер' ничего не менял.
    amount — своя цена от админа (/фа @юз оффер 1500), иначе — скидка по шкале _trade_offer."""
    if not l.gift_link:
        return None
    offer = amount or _trade_offer(l)
    trade_bot = (getattr(settings, "trade_bot_username", "") or "").lstrip("@")
    if trade_bot:
        tail = f" {offer[0]} {offer[1]}".rstrip() if offer else ""
        return f"@{trade_bot} {l.gift_link}{tail}"
    if not offer:
        return f"Здравствуйте! Интересует {l.gift_link} — готов купить. Какая финальная цена?"
    unit = f"{settings.ton_label}" if offer[1] == "g" else "⭐"
    return f"Здравствуйте! Готов забрать {l.gift_link} за {_n(offer[0])} {unit}"


def listing_keyboard(l: Listing, with_seller: bool = True, with_offer: bool = False,
                     offer_amount: tuple[int, str] | None = None) -> InlineKeyboardMarkup:
    """Кнопки: [Занять] / [Лот] [Написать без оффера | С оффером].
    По умолчанию — «Написать без оффера»: открывает ЛС продавца пустой, человек пишет сам.
    Если админ командой '/<юз_или_id> оффер' подтвердил лот — бот редактирует клавиатуру,
    кнопка становится «С оффером» и в поле ввода уже стоит оффер
    «@<бот> <ссылка на NFT> <сниженная_цена>» — остаётся нажать «Отправить»."""
    # цвета у всех кнопок разные: «Занять» — зелёная, «Лот» — синяя, «Написать» — красная
    rows = [[InlineKeyboardButton(text="Занять", style="success", **_icon(E.PERSON),
                                  callback_data=f"take:{l.gift_unique_id}"[:64])]]
    second = []
    if l.gift_link:
        second.append(InlineKeyboardButton(text="Лот", style="primary", **_icon(E.GIFT), url=l.gift_link))
    trade_bot = (getattr(settings, "trade_bot_username", "") or "").lstrip("@")
    offer_text = offer_message(l, offer_amount) if with_offer else None
    url = None
    if with_seller and l.seller_username:
        url = f"https://t.me/{l.seller_username}"
        if offer_text:
            url += f"?text={quote(offer_text, safe='/')}"
    elif offer_text and trade_bot:
        # у продавца нет @юза — в его ЛС текст не подставить (tg://user?id= не умеет ?text=),
        # открываем торгового бота с готовым оффером
        url = f"https://t.me/{trade_bot}?text={quote(offer_text, safe='/')}"
    elif with_seller and seller_url(l):
        url = seller_url(l)
    if url:
        # Без админской команды — «Написать» (пустой ЛС). После '/фа <юз|id> оффер' —
        # «Написать с оффером»: в поле ввода уже стоит оффер, остаётся нажать «Отправить».
        has_offer = bool(offer_text) and ("?text=" in url)
        # Кнопка «Написать» всегда красная (чтобы «светилась»), и с оффером, и без —
        # оффер теперь на всех лотах по умолчанию (OFFER_IN_BUTTON).
        second.append(InlineKeyboardButton(text="Написать с оффером" if has_offer else "Написать",
                                           style="danger", **_icon(E.PENCIL), url=url))
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
        parts.append(f"{_n(l.stars_price)}⭐")
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
        elif l.seller_type == "user" and l.seller_id and l.seller_id > 0:
            # без @username — ссылка на профиль по ID и сам ID (можно скопировать и найти).
            # ID > 0 проверяем: отрицательные — это наши псевдо-ID (хеш username для dedup),
            # tg://user?id= с ними не работает.
            seller_line = (f'<a href="tg://user?id={l.seller_id}">{html.escape(l.seller_name or "профиль")}</a>'
                           f' · ID <code>{l.seller_id}</code>')
        elif l.seller_type == "address":
            seller_line = "TON-кошелёк"
        elif l.seller_type in ("channel", "chat") and l.seller_name:
            seller_line = html.escape(l.seller_name) + " (канал без ссылки)"
        elif l.seller_name:
            # Скрытый Telegram-ом владелец: имя идёт как <code> — один тап копирует его в буфер,
            # дальше можно вручную найти профиль через любой поиск (глобальный Telegram, бот поиска и т.п.)
            seller_line = f"<code>{html.escape(l.seller_name)}</code> (скрыт Telegram – скопируй имя)"
            if getattr(l, "owner_pending", False):
                seller_line = f"<code>{html.escape(l.seller_name)}</code> (🔎 ищу владельца…)"
            elif l.owner_not_found:
                seller_line = f"<code>{html.escape(l.seller_name)}</code> (⚠️ владелец не найден ботом поиска)"
        else:
            seller_line = DASH

        if l.seller_profile_link:
            seller_profile = html.escape(l.seller_profile_link)
        elif l.seller_id and l.seller_id > 0:
            # отрицательные ID — наши псевдо-ID (хеш username), пользователю ничего не говорят
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
        detect_line = (f"\n{BOLT_TG} Обнаружен за {l.detect_secs:.2f} сек"
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
            price_line=_units(html.escape(price_line(l))),
            floor_line=_units(html.escape(floor_line(l))),
            stars_price=_e(l.stars_price),
            ton_price=_e(fmt_ton(l.ton_price)),
            currency=_e(l.currency),
            seller_line=seller_line,
            seller_username=_e(f"@{l.seller_username}" if l.seller_username else None),
            seller_id=_e(l.seller_id if (l.seller_id and l.seller_id > 0) else None),
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
