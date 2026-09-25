"""Модели данных (dataclasses), общие для БД, фильтров и отправки."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ListingStatus:
    NEW = "NEW"                        # вставлен, в обработке
    FILTERED_PRICE = "FILTERED_PRICE"  # не прошёл фильтр цены
    FILTERED_SELLER = "FILTERED_SELLER"
    FILTERED_DUPLICATE = "FILTERED_DUPLICATE"   # этот NFT / продавец уже отправлялся
    SENT = "SENT"
    TEST_ONLY = "TEST_ONLY"            # TEST_MODE: только лог / тестовый чат
    SEND_FAILED = "SEND_FAILED"
    ERROR = "ERROR"

    FINAL = {FILTERED_PRICE, FILTERED_SELLER, FILTERED_DUPLICATE, SENT, TEST_ONLY, SEND_FAILED, ERROR}


@dataclass
class Listing:
    listing_id: str                    # "<gift_unique_id>:<seller_key>" — см. tracker/marketplace.py
    gift_unique_id: int
    gift_id: int                       # ID базового подарка (коллекции)
    gift_name: str
    gift_num: int | None = None
    gift_slug: str | None = None
    gift_link: str | None = None
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None

    seller_key: str = "n"
    seller_id: int | None = None
    seller_type: str | None = None     # user | channel | chat | address | None
    seller_username: str | None = None
    seller_name: str | None = None
    seller_profile_link: str | None = None
    seller_phone: str | None = None    # НЕ сохраняется в БД, только для фильтра
    seller_about: str | None = None    # bio продавца — НЕ сохраняется в БД, только для фильтра
    floor_stars: int | None = None     # флор коллекции на момент обнаружения (для поста)
    seller_nfts: int | None = None     # сколько NFT видно в профиле продавца
    seller_channel: str | None = None  # название личного канала продавца (для фильтра языка)
    seller_gift_text: str | None = None  # тексты, приложенные к подаркам продавца — НЕ в БД, для фильтра языка

    stars_price: int | None = None
    ton_nano: int | None = None
    ton_only: bool = False

    level: int | None = None
    premium: bool | None = None
    messages: str | None = None        # "бесплатно" | "платно (N ⭐)" | "только Premium" | None
    seller_filter_result: str | None = None

    status: str = ListingStatus.NEW
    reason: str | None = None
    first_seen: str = field(default_factory=utcnow_iso)
    listing_created_at: str | None = None   # API не отдаёт дату выставления → None
    sent: bool = False
    message_id: int | None = None
    session_source: str | None = None

    @property
    def ton_price(self) -> float | None:
        return self.ton_nano / 1_000_000_000 if self.ton_nano is not None else None

    @property
    def currency(self) -> str:
        if self.stars_price is not None and self.ton_nano is not None:
            return "STARS+TON"
        if self.ton_nano is not None:
            return "TON"
        if self.stars_price is not None:
            return "STARS"
        return "UNKNOWN"
