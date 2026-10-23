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

from telethon import errors, TelegramClient
from telethon.tl import types
from telethon.tl.functions.payments import (GetResaleStarGiftsRequest, GetSavedStarGiftsRequest,
                                            GetStarGiftsRequest, GetUniqueStarGiftRequest)
from telethon.tl.functions.users import GetFullUserRequest

from utils.logger import get_logger

log = get_logger("marketplace")
from telethon.tl.functions.stories import GetPeerStoriesRequest


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
    model_rarity: str | None = None     # редкость атрибута: «1.2%» / «Легендарная» …
    backdrop_rarity: str | None = None
    symbol_rarity: str | None = None
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


def _rarity(a) -> str | None:
    r = getattr(a, "rarity", None)
    if r is None:
        pm = getattr(a, "rarity_permille", None)            # старый слой API
        return f"{pm / 10:g}%" if isinstance(pm, int) else None
    if isinstance(r, types.StarGiftAttributeRarity):
        return f"{r.permille / 10:g}%"
    return {"StarGiftAttributeRarityLegendary": "легендарная", "StarGiftAttributeRarityEpic": "эпическая",
            "StarGiftAttributeRarityRare": "редкая", "StarGiftAttributeRarityUncommon": "необычная"
            }.get(type(r).__name__)


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


def _rarities(attrs: list | None) -> tuple[str | None, str | None, str | None]:
    m = b = p = None
    for a in attrs or []:
        try:
            if isinstance(a, types.StarGiftAttributeModel):
                m = _rarity(a)
            elif isinstance(a, types.StarGiftAttributeBackdrop):
                b = _rarity(a)
            elif isinstance(a, types.StarGiftAttributePattern):
                p = _rarity(a)
        except Exception:
            pass
    return m, b, p


def _peer_to_owner(p: Any) -> tuple[str, str, int] | None:
    if isinstance(p, types.PeerUser):
        return f"u{p.user_id}", "user", p.user_id
    if isinstance(p, types.PeerChannel):
        return f"c{p.channel_id}", "channel", p.channel_id
    if isinstance(p, types.PeerChat):
        return f"g{p.chat_id}", "chat", p.chat_id
    return None


_OWNER_DIAG_LOGGED = 0           # разовая диагностика: сколько полностью-скрытых лотов уже сдампили
_OWNER_DIAG_LIMIT = 5             # не больше 5 дампов за время жизни процесса — чтобы не засрать логи


def _owner(g: types.StarGiftUnique) -> tuple[str, str | None, int | None]:
    """Определяет продавца по starGiftUnique.
    Приоритет (по схеме Layer 229, core.telegram.org/constructor/starGiftUnique):
      1) owner_id — если задан (у не-TON подарков)
      2) host_id — для NFT на TON-блокчейне (владение у TON-кошелька в owner_address,
         а host_id — профиль, НА КОТОРОМ подарок отображается, т.е. продавец, выставивший лот)
      3) owner_address — TON-кошелёк (ID Telegram-аккаунта неизвестен)
      4) owner_name — fallback строка, когда ничего другого нет
    """
    o = _peer_to_owner(g.owner_id)
    if o is not None:
        return o
    o = _peer_to_owner(getattr(g, "host_id", None))
    if o is not None:
        return o
    # Диагностика: первые N полностью-скрытых лотов — одной строкой в warning, чтобы увидеть
    # что реально отдаёт Telegram и понять, есть ли ещё поле, в котором спрятан продавец.
    global _OWNER_DIAG_LOGGED
    if _OWNER_DIAG_LOGGED < _OWNER_DIAG_LIMIT:
        _OWNER_DIAG_LOGGED += 1
        try:
            log.warning("HIDDEN_OWNER_DIAG slug=%s owner_id=%r host_id=%r released_by=%r theme_peer=%r "
                        "owner_address=%r gift_address=%r owner_name=%r",
                        getattr(g, "slug", None), g.owner_id, getattr(g, "host_id", None),
                        getattr(g, "released_by", None), getattr(g, "theme_peer", None),
                        g.owner_address, getattr(g, "gift_address", None), g.owner_name)
        except Exception:
            pass
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
    mr, br, sr = _rarities(g.attributes)
    seller_key, owner_type, owner_id = _owner(g)
    return ResaleItem(
        unique_id=g.id,
        gift_id=g.gift_id,
        title=g.title or "",
        slug=g.slug,
        num=getattr(g, "num", None),
        model=model,
        backdrop=backdrop,
        symbol=symbol,
        seller_key=seller_key,
        owner_type=owner_type,
        owner_id=owner_id,
        owner_name=getattr(g, "owner_name", None),
        owner_address=getattr(g, "owner_address", None),
        stars=stars,
        ton_nano=ton,
        ton_only=bool(getattr(g, "resale_ton_only", False)),
        model_rarity=mr, backdrop_rarity=br, symbol_rarity=sr,
        user=users.get(owner_id) if owner_type == "user" else None,
        chat=chats.get(owner_id) if owner_type in ("channel", "chat") else None,
    )


async def fetch_gift_owner(client: TelegramClient, slug: str) -> Any:
    """Владелец NFT по ссылке t.me/nft/<slug>. Три пути (в порядке приоритета):
      1) gift.owner_id = PeerUser   — обычные (не-TON) подарки
      2) gift.host_id = PeerUser    — NFT на TON-блокчейне, кто демонстрирует на профиле
      3) res.users[] содержит ровно одного (или одного не-released_by) юзера — Telegram возвращает
         владельца через users-масив, даже когда peer-поле в самом gift пустое.
    Namespace: только PeerUser; каналы/чаты не раскрываем здесь.
    Возвращает telethon User или None."""
    res = await client(GetUniqueStarGiftRequest(slug=slug))
    g = getattr(res, "gift", None)
    for peer in (getattr(g, "owner_id", None), getattr(g, "host_id", None)):
        if isinstance(peer, types.PeerUser):
            u = next((u for u in (res.users or []) if getattr(u, "id", None) == peer.user_id), None)
            if u is not None:
                return u
    # Fallback: owner пришёл в users[], но ни owner_id ни host_id на него не указывают.
    # Исключаем released_by (автор коллекции) и theme_peer (чат-тема).
    users = res.users or []
    exclude: set[int] = set()
    for peer_attr in ("released_by", "theme_peer"):
        peer = getattr(g, peer_attr, None)
        if isinstance(peer, types.PeerUser):
            exclude.add(peer.user_id)
    candidates = [u for u in users if getattr(u, "id", None) and u.id not in exclude]
    if len(candidates) == 1:
        log.info("Owner of %s resolved from users[] fallback (no peer-field pointed to them)", slug)
        return candidates[0]
    if candidates:
        log.info("Owner of %s ambiguous: users[]=%s — not guessing",
                 slug, [getattr(u, "id", None) for u in candidates])
    return None


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


_RESALE_DIAG_LOGGED = 0
_RESALE_DIAG_LIMIT = 3

async def fetch_resale_page(client: TelegramClient, gift_id: int, offset: str, limit: int) -> ResalePage:
    """Одна страница лотов коллекции, отсортированная по времени (новые сверху)."""
    res = await client(GetResaleStarGiftsRequest(gift_id=gift_id, offset=offset or "", limit=limit))
    users = {u.id: u for u in (res.users or [])}
    chats = {c.id: c for c in (res.chats or [])}
    items = []
    for g in res.gifts or []:
        try:
            it = parse_unique_gift(g, users, chats)
        except Exception as e:      # новый тип подарка/атрибута — один кривой лот не ломает всю страницу
            log.warning("Gift %s: cannot parse resale item (%s: %s) — skipped",
                        getattr(g, "slug", "?"), type(e).__name__, e)
            continue
        if it is not None:
            items.append(it)
    # Диагностика: сколько скрытых владельцев на странице vs сколько юзеров Telegram прислал.
    # Если users > 0 для страницы со скрытыми — значит владельцы ТУТ, нужно только привязать их к gifts.
    global _RESALE_DIAG_LOGGED
    hidden = [it for it in items if it.owner_type is None]
    if hidden and _RESALE_DIAG_LOGGED < _RESALE_DIAG_LIMIT:
        _RESALE_DIAG_LOGGED += 1
        try:
            uinfo = [(u.id, getattr(u, "username", None)) for u in list(users.values())[:10]]
            log.warning("RESALE_PAGE_DIAG gift_id=%s page: gifts=%d hidden=%d users=%d sample_users=%r",
                        gift_id, len(items), len(hidden), len(users), uinfo)
        except Exception:
            pass
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
    channel_title: str | None = None     # название личного канала профиля (UserFull.personal_channel_id)
    channel_id: int | None = None        # id личного канала (для чтения последних постов)
    business_text: str | None = None     # бизнес-профиль: приветствие, адрес


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
        username=(getattr(user, "username", None)
                  or next((u.username for u in (getattr(user, "usernames", None) or [])
                           if getattr(u, "active", False)), None)) if user else None,
        paid_message_stars=paid,
        contact_require_premium=(getattr(full, "contact_require_premium", None)
                                 or (getattr(user, "contact_require_premium", None) if user else None)),
        phone=getattr(user, "phone", None) if user else None,
        about=getattr(full, "about", None),
        channel_title=next((getattr(c, "title", None) for c in (res.chats or [])
                            if getattr(c, "id", None) == getattr(full, "personal_channel_id", None)), None),
        channel_id=getattr(full, "personal_channel_id", None),
        business_text=_business_text(full),
    )


async def fetch_full_user_failover(client: TelegramClient, user_id: int, gift_id: int) -> SellerFull:
    """Резервный путь, если аккаунт, нашедший лот, отвалился: другой аккаунт читает верх рынка
    этой коллекции (Telegram отдаёт продавцов вместе с доступом к ним) и уже потом их профиль."""
    try:
        return await fetch_full_user(client, user_id)
    except (ValueError, TypeError):
        await client(GetResaleStarGiftsRequest(gift_id=gift_id, offset="", limit=100))
        return await fetch_full_user(client, user_id)


def _business_text(full) -> str | None:
    parts = []
    intro = getattr(full, "business_intro", None)
    if intro:
        parts += [getattr(intro, "title", None), getattr(intro, "description", None)]
    loc = getattr(full, "business_location", None)
    if loc:
        parts.append(getattr(loc, "address", None))
    return "\n".join(p for p in parts if p) or None


async def fetch_extra_texts(client: TelegramClient, user_id: int, channel_id: int | None) -> str | None:
    """Подписи к историям продавца, адреса на историях и последние посты его личного канала.
    None — ничего не удалось прочитать из-за ошибки (такой результат не кэшируем)."""
    parts: list[str] = []
    failed = False
    entity = await client.get_input_entity(types.PeerUser(user_id))
    try:
        res = await client(GetPeerStoriesRequest(peer=entity))
        for st in getattr(getattr(res, "stories", None), "stories", None) or []:
            if getattr(st, "caption", None):
                parts.append(st.caption)
            for area in getattr(st, "media_areas", None) or []:
                if isinstance(getattr(area, "title", None), str):          # место (venue)
                    parts.append(area.title)
                addr = getattr(area, "address", None)                       # геометка с адресом
                if isinstance(addr, str):
                    parts.append(addr)
                elif addr is not None:
                    iso = getattr(addr, "country_iso2", None)
                    if iso:
                        parts.append(f"country:{iso}")
                    parts += [getattr(addr, "city", None) or "", getattr(addr, "street", None) or ""]
    except (errors.FloodError, errors.ServerError, ConnectionError, OSError,
            errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
        raise                     # FloodWait / обрыв должны дойти до session — она отдохнёт / переподключится
    except Exception:
        failed = True
    if channel_id:
        try:
            ch = await client.get_input_entity(types.PeerChannel(channel_id))
            async for m in client.iter_messages(ch, limit=5):
                if getattr(m, "message", None):
                    parts.append(m.message)
        except (errors.FloodError, errors.ServerError, ConnectionError, OSError,
                errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
            raise
        except Exception:
            failed = True
    text = "\n".join(p for p in parts if p)
    return None if (failed and not text) else text


async def fetch_gift_texts(client: TelegramClient, user_id: int, limit: int = 50) -> tuple[str, int | None]:
    """Тексты, приложенные к подаркам продавца (например поздравление от дарившего).
    Объективный признак языка — как bio, но из сообщений к подаркам. Скрытые подарки Telegram не отдаёт.
    Заодно → число NFT в профиле, если все подарки поместились в ответ (экономит отдельный запрос)."""
    entity = await client.get_input_entity(types.PeerUser(user_id))
    res = await client(GetSavedStarGiftsRequest(peer=entity, offset="", limit=limit))
    parts: list[str] = []
    gifts = getattr(res, "gifts", []) or []
    for g in gifts:
        msg = getattr(g, "message", None)
        text = getattr(msg, "text", None)
        if text:
            parts.append(text)
    nfts = None
    if int(getattr(res, "count", 0) or 0) <= len(gifts):
        nfts = sum(1 for g in gifts if isinstance(getattr(g, "gift", None), types.StarGiftUnique))
    return "\n".join(parts), nfts


async def fetch_nft_count(client: TelegramClient, user_id: int) -> int:
    """Сколько коллекционных (NFT) подарков видно в профиле продавца.
    payments.getSavedStarGifts без обычных подарков: exclude_unlimited + exclude_upgradable +
    exclude_unupgradable оставляют только коллекционные. Скрытые продавцом подарки Telegram не показывает."""
    entity = await client.get_input_entity(types.PeerUser(user_id))
    res = await client(GetSavedStarGiftsRequest(peer=entity, offset="", limit=1, exclude_unlimited=True,
                                                exclude_upgradable=True, exclude_unupgradable=True))
    return int(res.count)
