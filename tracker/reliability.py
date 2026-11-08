"""A retry or lookup never resets detection-to-delivery age."""
from __future__ import annotations
from datetime import datetime, timezone
import math
import time


def age_seconds(listing, *, now_wall=None, now_mono=None):
    wall = time.time() if now_wall is None else now_wall
    mono = time.monotonic() if now_mono is None else now_mono
    try:
        stamp = datetime.fromisoformat(listing.first_seen)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = wall - stamp.timestamp()
        if age < -5:
            return math.inf
    except (ValueError, TypeError, AttributeError, OverflowError):
        return math.inf
    found = getattr(listing, "t_found", None)
    if isinstance(found, (int, float)) and math.isfinite(found):
        age = max(age, mono - found)
    return max(0.0, age)


def age_limit(settings, *, lookup=False):
    limits = [float(getattr(settings, "max_lot_age", 180))]
    if lookup:
        limits.append(float(getattr(settings, "lookup_max_age", 180)))
    if getattr(settings, "strict_new_lots", True):
        limits.append(180.0)
    positive = [v for v in limits if math.isfinite(v) and v > 0]
    return min(positive) if positive else 0.0


def remaining_seconds(listing, settings, *, lookup=False):
    limit = age_limit(settings, lookup=lookup)
    return max(0.0, limit - age_seconds(listing)) if limit else 180.0
