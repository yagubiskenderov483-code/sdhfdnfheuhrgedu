"""Строгая / мягкая зона фильтров.

Строгая — тема 300-1000 (там больше всего скама). Мягкая — все остальные темы.
Тема считается так же, как при отправке поста: сначала маршруты по фону (BACKDROP_ROUTES),
потом по цене — флору коллекции при ROUTE_BY=floor, иначе цене лота.
"""
from __future__ import annotations

from config import settings


def route_value(l) -> int | None:
    """Значение, по которому выбирается тема (то же, что в ListingPipeline._send_stage)."""
    floor = getattr(l, "floor_stars", None)
    if settings.route_by == "floor" and floor:
        return floor
    return getattr(l, "stars_price", None)


def is_soft(l) -> bool:
    soft = int(getattr(settings, "soft_filter_stars", 0) or 0)
    if not soft:
        return False
    bd = getattr(l, "backdrop", None)
    if bd and any(name.lower() in bd.lower() for name, _c, _t in settings.backdrop_routes):
        return True                      # отдельная тема по фону (Black) — не 300-1000
    value = route_value(l)
    if value is None:
        value = getattr(l, "stars_price", None)
    return bool(value and value >= soft)
