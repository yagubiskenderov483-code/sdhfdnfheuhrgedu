"""API prices only. Rare patterns alone do not excuse a high markup."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from config import settings
from database.models import Listing


@dataclass
class FilterResult:
    passed: bool
    reason: str


def _rare(value):
    text = (value or "").strip().casefold()
    if text in {"rare", "epic", "legendary", "\u0440\u0435\u0434\u043a\u0430\u044f", "\u044d\u043f\u0438\u0447\u0435\u0441\u043a\u0430\u044f", "\u043b\u0435\u0433\u0435\u043d\u0434\u0430\u0440\u043d\u0430\u044f"}:
        return True
    if not text.endswith("%"):
        return False
    try:
        rarity = Decimal(text[:-1].replace(",", "."))
        return rarity.is_finite() and Decimal(0) < rarity <= Decimal(str(getattr(settings, "rare_attribute_percent", 1.5)))
    except (InvalidOperation, ValueError):
        return False


_MONO_WORDS = ("monochrome", "\u043c\u043e\u043d\u043e\u0445\u0440\u043e\u043c")


def is_monochrome(l) -> bool:
    """Монохром в модели, фоне или узоре (в т.ч. «Monochrome Black» и т.п.)."""
    for v in (l.model, l.backdrop, getattr(l, "symbol", None)):
        text = (v or "").casefold()
        if any(w in text for w in _MONO_WORDS):
            return True
    return False


def premium_exception(l):
    """+333% к флору и выше пропускаем ТОЛЬКО если редкая модель, редкий фон или монохром."""
    return is_monochrome(l) or _rare(l.model_rarity) or _rare(l.backdrop_rarity)


class PriceFilter:
    def check(self, l: Listing) -> FilterResult:
        stars, ton = l.stars_price, l.ton_price
        ton_only = l.ton_only or stars is None
        if (stars is None and ton is None) or (stars is not None and stars <= 0) or (ton is not None and ton <= 0):
            return FilterResult(False, "no positive price in API response")
        if ton_only and not settings.allow_ton_only:
            return FilterResult(False, "TON-only listing (ALLOW_TON_ONLY=false)")
        if l.ton_only and ton is None:
            return FilterResult(False, "TON-only listing has no TON price")
        minimum = settings.collection_min_floor or settings.min_stars
        if stars is not None and stars < minimum:
            return FilterResult(False, f"{stars} Stars < {minimum}")
        if ton is not None and ton < settings.min_ton:
            return FilterResult(False, f"{ton:g} TON < {settings.min_ton:g}")
        threshold = getattr(settings, "premium_over_floor_percent", 333)
        if stars is not None and l.floor_stars and l.floor_stars > 0:
            over = (stars - l.floor_stars) * 100
            special = premium_exception(l)
            if threshold > 0 and over >= l.floor_stars * threshold and not special:
                return FilterResult(False, f"premium >= {threshold}% above floor; no rare model/backdrop or Monochrome")
            cap = settings.max_above_floor
            if cap > 0 and over > l.floor_stars * cap and not (threshold > 0 and special):
                return FilterResult(False, f"{stars} Stars above floor {l.floor_stars} by more than {cap}%")
        return FilterResult(True, "API price and attribute rules passed")
