"""
Фильтр цены. Никакой конвертации Stars ↔ TON по выдуманному курсу:
  • если у лота есть цена в Stars → проходит при stars >= MIN_STARS;
  • если лот продаётся ТОЛЬКО за TON (resale_ton_only / нет цены в Stars) →
    проходит только при ALLOW_TON_ONLY=true и ton >= MIN_TON;
  • если MIN_TON > 0 и у лота есть обе цены — должны выполняться оба порога.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import settings
from database.models import Listing


@dataclass
class FilterResult:
    passed: bool
    reason: str


class PriceFilter:
    def check(self, l: Listing) -> FilterResult:
        min_stars = settings.min_stars
        min_ton = settings.min_ton
        ton = l.ton_price

        if l.stars_price is not None:
            if l.stars_price < min_stars:
                return FilterResult(False, f"{l.stars_price} Stars < {min_stars}")
            if min_ton > 0 and ton is not None and ton < min_ton:
                return FilterResult(False, f"{ton:g} TON < {min_ton:g}")
            return FilterResult(True, f"{l.stars_price} Stars >= {min_stars}")

        if ton is not None:
            if not settings.allow_ton_only:
                return FilterResult(False, "TON-only listing (ALLOW_TON_ONLY=false)")
            if ton < min_ton:
                return FilterResult(False, f"{ton:g} TON < {min_ton:g}")
            return FilterResult(True, f"{ton:g} TON >= {min_ton:g}")

        return FilterResult(False, "no price in API response")
