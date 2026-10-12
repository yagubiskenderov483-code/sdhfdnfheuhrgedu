"""
SellerFilter — отдельный модуль, алгоритм можно менять независимо от остального кода.

Telegram API НЕ отдаёт национальность или страну чужого пользователя (User.lang_code —
только для ботов). Имя, фамилия, фото и username НЕ используются.

RUSSIAN_ONLY=true пропускает продавца, только если есть объективный признак, по порядку:
  1. data/seller_blacklist.txt (ID или @username)                       → отсеять
  2. data/seller_whitelist.txt                                          → пропустить
  3. телефон виден (Telegram отдал User.phone) и начинается с
     SELLER_PHONE_PREFIXES (по умолчанию 7)                             → пропустить, иначе отсеять
  4. описание профиля (bio), название личного канала или тексты к подаркам на славянской
     кириллице (русский/украинский/белорусский), без казахских букв (ә ғ қ ...) → пропустить
  5. ничего из этого нет → UNKNOWN_SELLER_POLICY: block (отсеять, по умолчанию) | allow

RUSSIAN_ONLY=false → пропускаются все (кроме blacklist).

До проверки языка применяются ещё два фильтра:
  • REQUIRE_FREE_MESSAGES=true — продавцу можно написать бесплатно (нет платных сообщений
    и нет ограничения «только Premium»);
  • MAX_SELLER_NFTS=12 — в профиле продавца видно не больше 12 NFT (payments.getSavedStarGifts).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config import settings
from database.models import Listing

_CYR = re.compile(r"[Ѐ-ӿ]", re.IGNORECASE)        # любая кириллица
# Буквы, которых НЕТ в русском/украинском/белорусском (казахский, таджикский, башкирский,
# чувашский, осетинский, сербский, македонский и др.). Их наличие → текст не наш.
# ў/і/ї/є/ґ/ъ (бел/укр/рус) НЕ включены — их не режем.
_NON_SLAVIC = re.compile(r"[әғқңөұүһҳҷҙҗӣӯҫӑӕҝҕӧҭҡӳӗђћџњљјѓќѕ]", re.IGNORECASE)


# Письменности, которых нет у RU/UA/BY: арабская/персидская, иврит, индийские, тайская,
# китайская/японская/корейская, грузинская, армянская.
_FOREIGN_SCRIPT = re.compile(r"[\u0590-\u06FF\u0750-\u077F\u0900-\u0DFF\u0E00-\u0E7F"
                             r"\u10A0-\u10FF\u0530-\u058F\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]")


def is_foreign_text(text: str | None) -> bool:
    """Текст явно не на нашем языке: чужая письменность или неславянская кириллица."""
    if not text:
        return False
    return bool(_FOREIGN_SCRIPT.search(text) or _NON_SLAVIC.search(text))


def is_russian_text(text: str | None) -> bool:
    """Текст на славянской кириллице (русский / украинский / белорусский).
    Смягчено: латиница (бренды/ссылки) и короткий текст не мешают.
    Отсекаем только НЕ славянскую кириллицу (казахский и т.п.) и чистую латиницу."""
    if not text:
        return False
    t = text.strip()
    if _NON_SLAVIC.search(t):             # ә ғ қ ң ... → казахский и др., не наш
        return False
    # хоть одно кириллическое слово (2+ буквы подряд); латиница рядом не мешает.
    # «Gіfts fоr sale» — латиница с подменёнными і/о — русским не считается
    return bool(re.search(r"[Ѐ-ӿ]{2,}", t))


_FLAGS = ("🇷🇺", "🇺🇦", "🇧🇾")
# Русскоязычные сервисы/деньги/города в тексте профиля (часто пишут латиницей)
_RU_MARKERS = re.compile(
    r"(vk\.com|vk\.ru|\bvk\b|ok\.ru|mail\.ru|yandex|\.ru\b|\.su\b|\.by\b|\.ua\b|₽|\brub\b|\bрубл|"
    r"sber|tinkoff|t-bank|tbank|\bozon\b|wildberries|\bwb\b|avito|funpay|playerok|lolz|"
    r"moscow|\bmsk\b|\bspb\b|\bpiter\b|saint.?petersburg|kazan|novosibirsk|ekaterinburg|ekb\b|krasnodar|minsk|kyiv|kiev|"
    r"kharkiv|odesa|odessa|country:(ru|ua|by))", re.IGNORECASE)


def has_ru_marker(*texts: str | None) -> bool:
    return any(t and _RU_MARKERS.search(t) for t in texts)


def has_flag(*texts: str | None) -> bool:
    """Продавец сам поставил флаг RU/UA/BY в имя, био или канал."""
    return any(t and any(f in t for f in _FLAGS) for t in texts)


@dataclass
class SellerResult:
    passed: bool
    code: str       # BLACKLIST | WHITELIST | PHONE_MATCH | PHONE_MISMATCH | BIO_RU | UNKNOWN_ALLOWED | UNKNOWN_BLOCKED | DISABLED
    reason: str


def _load_list(path: Path) -> tuple[set[int], set[str]]:
    ids: set[int] = set()
    names: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.lstrip("-").isdigit():
                ids.add(int(line))
            else:
                names.add(line.lstrip("@").lower())
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# один user id или @username на строку\n", encoding="utf-8")
    return ids, names


class SellerFilter:
    def __init__(self, data_dir: Path):
        self.whitelist_path = data_dir / "seller_whitelist.txt"
        self.blacklist_path = data_dir / "seller_blacklist.txt"
        self.reload()

    def reload(self) -> None:
        self.white_ids, self.white_names = _load_list(self.whitelist_path)
        self.black_ids, self.black_names = _load_list(self.blacklist_path)

    @staticmethod
    def _match(l: Listing, ids: set[int], names: set[str]) -> bool:
        if l.seller_id is not None and l.seller_id in ids:
            return True
        return bool(l.seller_username and l.seller_username.lower() in names)

    def check(self, l: Listing) -> SellerResult:
        if self._match(l, self.black_ids, self.black_names):
            return SellerResult(False, "BLACKLIST", "seller in blacklist")

        # продавцу должно быть можно написать бесплатно
        if settings.require_free_messages and l.seller_type == "user" and l.messages != "бесплатно":
            return SellerResult(False, "PAID_MESSAGES",
                                f"messages: {l.messages or 'unknown'} (REQUIRE_FREE_MESSAGES=true)")

        # у продавца не слишком много NFT
        if settings.max_seller_nfts > 0 and l.seller_nfts is not None and l.seller_nfts > settings.max_seller_nfts:
            return SellerResult(False, "TOO_MANY_NFTS",
                                f"{l.seller_nfts} NFT > MAX_SELLER_NFTS={settings.max_seller_nfts}")

        # Level продавца (UserFull.stars_rating.level); нет рейтинга = Level 0
        if settings.max_seller_level > 0 and (l.level or 0) > settings.max_seller_level:
            return SellerResult(False, "LEVEL_TOO_HIGH",
                                f"level {l.level} > MAX_SELLER_LEVEL={settings.max_seller_level}")

        if not settings.russian_only:
            return SellerResult(True, "DISABLED", "RUSSIAN_ONLY=false")

        if self._match(l, self.white_ids, self.white_names):
            return SellerResult(True, "WHITELIST", "seller in whitelist")

        phone = (l.seller_phone or "").lstrip("+")
        if phone:
            if any(phone.startswith(p) for p in settings.seller_phone_prefixes):
                return SellerResult(True, "PHONE_MATCH", "visible phone prefix matches")
            return SellerResult(False, "PHONE_MISMATCH", "visible phone prefix does not match")

        if is_russian_text(l.seller_about):
            return SellerResult(True, "BIO_RU", "profile bio is written in Russian")

        if is_russian_text(l.seller_channel):
            return SellerResult(True, "CHANNEL_RU", "personal channel title is in Russian")

        if is_russian_text(l.seller_gift_text):
            return SellerResult(True, "GIFT_MSG_RU", "gift messages are written in Russian")

        if has_flag(l.seller_name, l.seller_about, l.seller_channel, l.seller_business):
            return SellerResult(True, "FLAG_RU", "RU/UA/BY flag in profile")

        if is_russian_text(l.seller_business):
            return SellerResult(True, "BUSINESS_RU", "business profile is in Russian")

        if is_russian_text(l.seller_extra_text):
            return SellerResult(True, "STORIES_RU", "stories / channel posts are in Russian")

        if has_ru_marker(l.seller_about, l.seller_channel, l.seller_business, l.seller_extra_text):
            return SellerResult(True, "RU_MARKER", "Russian services / cities / currency in profile")

        # явно чужой язык в био / канале / подписях подарков → не наш продавец
        for field in (l.seller_about, l.seller_channel, l.seller_gift_text, l.seller_business, l.seller_extra_text):
            if is_foreign_text(field):
                return SellerResult(False, "FOREIGN_TEXT", "profile text is in a foreign language")

        if settings.unknown_seller_policy == "allow":
            return SellerResult(True, "UNKNOWN_ALLOWED", "no objective data (policy=allow)")
        return SellerResult(False, "UNKNOWN_BLOCKED", "no Russian bio / phone / whitelist (policy=block)")
