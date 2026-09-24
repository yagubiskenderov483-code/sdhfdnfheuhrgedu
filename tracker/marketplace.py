"""
Работа с реальным MTProto API Telegram Gift Marketplace (проверено по схеме Telethon 1.45 / Layer 229
и документации core.telegram.org):

  payments.getStarGifts(hash)                        → payments.StarGifts
      список базовых подарков. У коллекций, которые можно перепродавать, есть поле
      StarGift.availability_resale (кол-во лотов на маркете) и resell_min_stars.

  payments.getResaleStarGifts(gift_id, offset, limit, [sort_by_price], [sort_by_num], [stars_only], ...)
                                                     → payments.ResaleStarGifts(count, gifts, next_offset, users, chats, ...)
      лоты маркетплейса конкретной коллекции. Если не передавать sort_by_price / sort_by_num,
      Telegram сортирует по «unixtime (descending) when their resell price was last changed»,
      т.е. новые листинги (и изменения цены) всегда наверху. На этом построен live-трекер.

  users.getFullUser(id)                              → users.UserFull
      UserFull.stars_rating.level  — Level продавца;
      User.premium, User.send_paid_messages_stars, User.contact_require_premium — статус сообщений.

Каждый лот — это starGiftUnique: id, gift_id, title, slug, num, owner_id, owner_name, owner_address,
attributes (model / backdrop / pattern), resell_amount: Vector<StarsAmount> (StarsAmount — звёзды,
StarsTonAmount — TON в нано-единицах), resale_ton_only.

Отдельного «listing ID» и даты выставления в API нет. Поэтому:
  listing_id = "<starGiftUnique.id>:<seller_key>"
  (один и тот же подарок, выставленный другим продавцом, — это новый листинг).
Realtime-апдейтов о новых лотах маркета в MTProto нет → используется быстрый polling
с курсором (state) по каждой коллекции.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient
from telethon.tl import types
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest
from telethon.tl.functions.users import GetFullUserRequest


@dataclass
class CollectionInfo:
    gift_id: int
    title: str
    availability_resale: int | None
    resell_min_stars: int | None


@dataclass
class ResaleItem:
    unique_id: int
    gift_id: int
    title: str
    slug: str | None
    num: int | None
    model: str | None
    backdrop: str | None
    symbol: str | None
    seller_key: str                 # u<id> | c<id> | g<id> | a<ton address> | n:<name> | n
    owner_type: str | None          # user | channel | chat | address | None
    owner_id: int | None
    owner_name: str | None
    owner_address: str | None
    stars: int | None
    ton_nano: int | None
    ton_only: bool
    user: Any = None                # telethon User (если есть)
    chat: Any = None                # telethon Channel/Chat (если продавец — канал)

    @property
    def listing_id(self) -> str:
        return f"{self.unique_id}:{self.seller_key}"


@dataclass
class ResalePage:
    items: list[ResaleItem]
    next_offset: str | None
    count: int
    raw_users: dict[int, Any] = field(default_factory=dict)


def parse_price(resell_amount: list | None) -> tuple[int | None, int | None]:
    """→ (stars, ton_nano). Без пересчёта курсов — только то, что вернул Telegram."""
    stars: int | None = None
    ton: int | None = None
    for a in resell_amount or []:
        if isinstance(a, types.StarsTonAmount):
            ton = int(a.amount)
        elif isinstance(a, types.StarsAmount):
            stars = int(a.amount)
    return stars, ton


def _attributes(attrs: list | None) -> tuple[str | None, str | None, str | None]:
    model = backdrop = symbol = None
    for a in attrs or []:
        if isinstance(a, types.StarGiftAttributeModel):
            model = a.name
        elif isinstance(a, types.StarGiftAttributeBackdrop):
            backdrop = a.name
        elif isinstance(a, types.StarGiftAttributePattern):
            symbol = a.name
    return model, backdrop, symbol


def _owner(g: types.StarGiftUnique) -> tuple[str, str | None, int | None]:
    p = g.owner_id
    if isinstance(p, types.PeerUser):
        return f"u{p.user_id}", "user", p.user_id
    if isinstance(p, types.PeerChannel):
        return f"c{p.channel_id}", "channel", p.channel_id
    if isinstance(p, types.PeerChat):
        return f"g{p.chat_id}", "chat", p.chat_id
    if g.owner_address:
        return f"a{g.owner_address}", "address", None
    if g.owner_name:
        return f"n:{g.owner_name}", None, None
    return "n", None, None


def parse_unique_gift(g: Any, users: dict[int, Any], chats: dict[int, Any]) -> ResaleItem | None:
    if not isinstance(g, types.StarGiftUnique):
        return None
    stars, ton = parse_price(g.resell_amount)
    model, backdrop, symbol = _attributes(g.attributes)
    seller_key, owner_type, owner_id = _owner(g)
    return ResaleItem(
        unique_id=g.id,
        gift_id=g.gift_id,
        title=g.title,
        slug=g.slug,
        num=g.num,
        model=model,
        backdrop=backdrop,
        symbol=symbol,
        seller_key=seller_key,
        owner_type=owner_type,
        owner_id=owner_id,
        owner_name=g.owner_name,
        owner_address=g.owner_address,
        stars=stars,
        ton_nano=ton,
        ton_only=bool(g.resale_ton_only),
        user=users.get(owner_id) if owner_type == "user" else None,
        chat=chats.get(owner_id) if owner_type in ("channel", "chat") else None,
    )


async def fetch_catalog(client: TelegramClient) -> list[CollectionInfo]:
    """Все коллекции, у которых есть вторичный рынок."""
    res = await client(GetStarGiftsRequest(hash=0))
    out: list[CollectionInfo] = []
    for g in getattr(res, "gifts", []) or []:
        if not isinstance(g, types.StarGift):
            continue
        if g.availability_resale is None:
            continue
        out.append(CollectionInfo(
            gift_id=g.id,
            title=g.title or f"gift_{g.id}",
            availability_resale=g.availability_resale,
            resell_min_stars=g.resell_min_stars,
        ))
    return out


async def fetch_resale_page(client: TelegramClient, gift_id: int, offset: str, limit: int) -> ResalePage:
    """Одна страница лотов коллекции, отсортированная по времени (новые сверху)."""
    res = await client(GetResaleStarGiftsRequest(gift_id=gift_id, offset=offset or "", limit=limit))
    users = {u.id: u for u in (res.users or [])}
    chats = {c.id: c for c in (res.chats or [])}
    items = []
    for g in res.gifts or []:
        it = parse_unique_gift(g, users, chats)
        if it is not None:
            items.append(it)
    return ResalePage(items=items, next_offset=res.next_offset or None, count=res.count, raw_users=users)


@dataclass
class SellerFull:
    level: int | None
    premium: bool | None
    username: str | None
    paid_message_stars: int | None
    contact_require_premium: bool | None
    phone: str | None
    about: str | None = None


async def fetch_full_user(client: TelegramClient, user_id: int) -> SellerFull:
    entity = await client.get_input_entity(types.PeerUser(user_id))
    res = await client(GetFullUserRequest(entity))
    full = res.full_user
    user = next((u for u in res.users if getattr(u, "id", None) == user_id), None)
    level = full.stars_rating.level if getattr(full, "stars_rating", None) else None
    paid = getattr(full, "send_paid_messages_stars", None)
    if paid is None and user is not None:
        paid = getattr(user, "send_paid_messages_stars", None)
    return SellerFull(
        level=level,
        premium=getattr(user, "premium", None) if user else None,
        username=getattr(user, "username", None) if user else None,
        paid_message_stars=paid,
        contact_require_premium=(getattr(full, "contact_require_premium", None)
                                 or (getattr(user, "contact_require_premium", None) if user else None)),
        phone=getattr(user, "phone", None) if user else None,
        about=getattr(full, "about", None),
    )
