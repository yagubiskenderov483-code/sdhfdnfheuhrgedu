"""
Состояние коллекций и алгоритм определения НОВЫХ листингов.

Источник истины — SQLite (таблица market_items). В RAM держится только метаданные коллекций;
известные лоты для каждой полученной страницы запрашиваются из БД пачкой (lookup_market_items).

ИДЕНТИЧНОСТЬ ЛИСТИНГА
  listing_id = "<starGiftUnique.id>:<seller_key>"   (отдельного ID листинга в API нет — Layer 229)
  seller_key: u<user_id> | c<channel_id> | g<chat_id> | a<ton_address> — «сильные» ключи;
              n:<owner_name> | n — «слабые» (владелец скрыт / не пришёл в ответе).
  Слабый ключ нестабилен (в одном ответе владелец есть, в другом — нет), поэтому:
    • тот же NFT, оба ключа сильные и разные → другой продавец → ДРУГОЙ листинг (перепродажа);
    • тот же NFT, хотя бы один ключ слабый   → тот же листинг (продавец просто не пришёл).
  Цена в идентичность НЕ входит: смена цены = PRICE_MOVED того же листинга.

ПОРЯДОК ВЫДАЧИ
  payments.getResaleStarGifts без sort_by_* отдаёт лоты «by the unixtime (descending) when their
  resell price was last changed» — новые листинги И изменения цены поднимаются наверх.

КЛАССИФИКАЦИЯ страницы (сверху вниз):
  KNOWN (anchor) — известный листинг с той же ценой. Он не двигался → всё, что выше, свежее.
  PRICE_MOVED    — известный листинг с другой ценой → НЕ новый, только обновить цену.
  UNKNOWN        — листинга нет в market_items.
    • выше первого anchor и коллекция complete (все её лоты были прочитаны) → NEW;
    • выше anchor, но коллекция НЕ complete → SILENT_OLD (это может быть старый лот,
      который мы никогда не видели и у которого сменили цену) + нужна полная синхронизация;
    • ниже anchor → SILENT_OLD (старый лот впервые попал в окно сканирования).
  anchor не найден:
    • прочитан ВЕСЬ список (конец выдачи) и коллекция complete → UNKNOWN гарантированно
      отсутствовал в полном известном наборе → NEW (случай пустой/маленькой коллекции);
    • иначе → undecided: дочитать страницы, затем RESYNC без отправки.

first_seen — время ПЕРВОГО ОБНАРУЖЕНИЯ нашим трекером, не дата выставления (её API не отдаёт).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tracker.marketplace import ResaleItem

STRONG_PREFIXES = ("u", "c", "g", "a")


class MarketState:
    KNOWN = "KNOWN"
    PRICE_MOVED = "PRICE_MOVED"
    SILENT_OLD = "SILENT_OLD"
    NEW = "NEW"


def is_strong(seller_key: str | None) -> bool:
    return bool(seller_key) and seller_key[0] in STRONG_PREFIXES and not seller_key.startswith("n")


def same_listing(old_seller_key: str, new_seller_key: str) -> bool:
    """Тот же NFT: это тот же листинг или перепродажа другим продавцом?"""
    if old_seller_key == new_seller_key:
        return True
    return not (is_strong(old_seller_key) and is_strong(new_seller_key))


@dataclass
class KnownItem:
    """Строка market_items для одного NFT."""
    listing_id: str
    seller_key: str
    stars: int | None
    ton_nano: int | None


@dataclass
class CollectionState:
    gift_id: int
    title: str
    synced: bool = False
    complete: bool = False                    # все лоты коллекции были прочитаны полной синхронизацией
    last_cursor: str | None = None            # listing_id верхнего лота при последнем опросе
    last_seen_listing_id: str | None = None   # последний НОВЫЙ листинг
    last_seen_timestamp: str | None = None
    availability_resale: int | None = None
    last_poll: str | None = None
    last_session: str | None = None
    needs_full_sync: bool = False             # запланирована тихая полная ре-синхронизация
    floor_stars: int | None = None            # StarGift.resell_min_stars — флор коллекции (из каталога)

    def to_row(self) -> dict:
        return {
            "gift_id": self.gift_id, "title": self.title, "synced": int(self.synced),
            "complete": int(self.complete), "last_cursor": self.last_cursor,
            "last_seen_listing_id": self.last_seen_listing_id,
            "last_seen_timestamp": self.last_seen_timestamp,
            "availability_resale": self.availability_resale, "last_poll": self.last_poll,
            "last_session": self.last_session,
        }


@dataclass
class Classification:
    decided: bool                                             # найден anchor или прочитан весь список
    new: list[ResaleItem] = field(default_factory=list)       # сверху вниз (новейшие первыми)
    silent: list[tuple[ResaleItem, str]] = field(default_factory=list)   # (лот, причина)
    moved: list[tuple[ResaleItem, KnownItem]] = field(default_factory=list)
    known: list[ResaleItem] = field(default_factory=list)
    anchor: ResaleItem | None = None
    reason: str = ""


def item_status(it: ResaleItem, known: dict[int, KnownItem]) -> tuple[str, KnownItem | None]:
    """→ ("KNOWN" | "PRICE_MOVED" | "UNKNOWN", строка БД)"""
    k = known.get(it.unique_id)
    if k is None or not same_listing(k.seller_key, it.seller_key):
        return "UNKNOWN", k
    if k.stars == it.stars and k.ton_nano == it.ton_nano:
        return "KNOWN", k
    return "PRICE_MOVED", k


def classify(state: CollectionState, items: list[ResaleItem], known: dict[int, KnownItem],
             reached_end: bool) -> Classification:
    above: list[ResaleItem] = []
    c = Classification(decided=False)

    anchor_idx = None
    for idx, it in enumerate(items):
        st, k = item_status(it, known)
        if st == "KNOWN":
            anchor_idx = idx
            c.anchor = it
            break
        if st == "PRICE_MOVED":
            c.moved.append((it, k))
        else:
            above.append(it)

    if anchor_idx is not None:
        c.decided = True
        c.known.append(items[anchor_idx])
        if state.complete:
            c.new = above
            c.reason = "above_anchor"
        else:
            c.silent += [(it, "collection_not_fully_synced") for it in above]
            c.reason = "above_anchor_but_incomplete"
        for it in items[anchor_idx + 1:]:
            st, k = item_status(it, known)
            if st == "UNKNOWN":
                c.silent.append((it, "first_seen_below_anchor"))
            elif st == "PRICE_MOVED":
                c.moved.append((it, k))
            else:
                c.known.append(it)
        return c

    if reached_end:
        c.decided = True
        if state.complete:
            c.new = above
            c.reason = "end_of_list_complete"
        else:
            c.silent += [(it, "end_of_list_incomplete") for it in above]
            c.reason = "end_of_list_incomplete"
        return c

    c.reason = "anchor_not_found"
    return c
