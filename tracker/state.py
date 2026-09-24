"""
Состояние коллекций и алгоритм определения НОВЫХ лотов.

Telegram возвращает лоты коллекции отсортированными по времени последнего изменения цены
(новые сверху). Поэтому новые листинги всегда появляются В НАЧАЛЕ списка.

Алгоритм (для каждой коллекции, страница за страницей сверху вниз):

  known      — лот уже есть в state с тем же продавцом
  «якорь»    — known-лот с НЕИЗМЕНЁННОЙ ценой: он не двигался, значит всё, что выше него, появилось
               после прошлого опроса.
  price-move — known-лот с изменённой ценой: поднялся наверх из-за смены цены, это НЕ новый листинг.

  • все неизвестные лоты ВЫШЕ первого якоря → НОВЫЕ (идут в очередь, все до одного);
  • неизвестные лоты НИЖЕ якоря → старые лоты, «съехавшие» в окно из-за продаж → silently known;
  • если якоря на странице нет (всплеск > limit новых лотов) → берём следующую страницу
    (до LIVE_MAX_PAGES / RECOVERY_MAX_PAGES);
  • если дошли до конца списка без якоря: новые, только если коллекция ранее была
    синхронизирована полностью (complete=1), иначе — безопасная тихая ре-синхронизация;
  • если лимит страниц исчерпан без якоря — тихая ре-синхронизация + WARNING в лог
    (лучше пропустить, чем разослать тысячи старых лотов).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from tracker.marketplace import ResaleItem


@dataclass
class KnownItem:
    seller_key: str
    stars: int | None
    ton_nano: int | None


@dataclass
class CollectionState:
    gift_id: int
    title: str
    synced: bool = False
    complete: bool = False
    last_cursor: str | None = None            # unique_id верхнего лота при последнем опросе
    last_seen_listing_id: str | None = None   # последний НОВЫЙ листинг
    last_seen_timestamp: str | None = None
    availability_resale: int | None = None
    last_poll: str | None = None
    last_session: str | None = None
    known: dict[int, KnownItem] = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "gift_id": self.gift_id, "title": self.title, "synced": int(self.synced),
            "complete": int(self.complete), "last_cursor": self.last_cursor,
            "last_seen_listing_id": self.last_seen_listing_id,
            "last_seen_timestamp": self.last_seen_timestamp,
            "availability_resale": self.availability_resale, "last_poll": self.last_poll,
            "last_session": self.last_session,
        }

    def remember(self, items: Iterable[ResaleItem]) -> list[tuple[int, int, str, int | None, int | None]]:
        """Запомнить лоты; вернуть только изменившиеся строки для записи в БД."""
        rows = []
        for it in items:
            new = KnownItem(it.seller_key, it.stars, it.ton_nano)
            if self.known.get(it.unique_id) != new:
                self.known[it.unique_id] = new
                rows.append((it.unique_id, it.gift_id, it.seller_key, it.stars, it.ton_nano))
        return rows


@dataclass
class Classification:
    decided: bool                         # удалось однозначно разметить (найден якорь / конец списка)
    new: list[ResaleItem] = field(default_factory=list)       # сверху вниз (новейшие первыми)
    silent: list[ResaleItem] = field(default_factory=list)    # неизвестные, но старые
    moved: list[ResaleItem] = field(default_factory=list)     # known с изменённой ценой
    anchor: ResaleItem | None = None


def classify(state: CollectionState, items: list[ResaleItem], reached_end: bool) -> Classification:
    candidates: list[ResaleItem] = []
    moved: list[ResaleItem] = []
    anchor_idx: int | None = None

    for idx, it in enumerate(items):
        k = state.known.get(it.unique_id)
        if k is not None and k.seller_key == it.seller_key:
            if k.stars == it.stars and k.ton_nano == it.ton_nano:
                anchor_idx = idx
                break
            moved.append(it)
        else:
            candidates.append(it)

    if anchor_idx is not None:
        below = items[anchor_idx + 1:]
        silent = [it for it in below
                  if not (it.unique_id in state.known and state.known[it.unique_id].seller_key == it.seller_key)]
        moved_below = [it for it in below
                       if it.unique_id in state.known and state.known[it.unique_id].seller_key == it.seller_key
                       and (state.known[it.unique_id].stars != it.stars
                            or state.known[it.unique_id].ton_nano != it.ton_nano)]
        return Classification(True, new=candidates, silent=silent, moved=moved + moved_below,
                              anchor=items[anchor_idx])

    if reached_end:
        if state.complete:
            return Classification(True, new=candidates, moved=moved)
        return Classification(True, silent=candidates, moved=moved)

    return Classification(False, moved=moved)
