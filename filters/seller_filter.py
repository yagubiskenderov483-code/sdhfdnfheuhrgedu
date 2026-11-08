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


# Only Arabic/Persian script is a hard script-level reject. Other languages/scripts
# are not rejected merely because of how a nickname/name is written.
# Включая «красивые» формы из ников (presentation forms FB50–FDFF, FE70–FEFF) — раньше их пропускало.
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u0870-\u089F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")

# \u0422\u0443\u0440\u0435\u0446\u043A\u0438\u0435 / \u0430\u0437\u0435\u0440\u0431\u0430\u0439\u0434\u0436\u0430\u043D\u0441\u043A\u0438\u0435 / \u043D\u0435\u043C\u0435\u0446\u043A\u0438\u0435 \u0431\u0443\u043A\u0432\u044B \u0441 \u0434\u0438\u0430\u043A\u0440\u0438\u0442\u0438\u043A\u043E\u0439: \u00F6 \u00FC \u011F \u0259 \u00E7 \u015F \u0131.
# \u0412 \u0438\u043C\u0435\u043D\u0430\u0445 \u043D\u0430\u0441\u0442\u043E\u044F\u0449\u0438\u0445 RU/UA/BY-\u043F\u0440\u043E\u0434\u0430\u0432\u0446\u043E\u0432 \u043F\u0440\u0430\u043A\u0442\u0438\u0447\u0435\u0441\u043A\u0438 \u043D\u0435 \u0432\u0441\u0442\u0440\u0435\u0447\u0430\u044E\u0442\u0441\u044F \u2014 \u0434\u043B\u044F \u0441\u043A\u0440\u044B\u0442\u044B\u0445 \u0432\u043B\u0430\u0434\u0435\u043B\u044C\u0446\u0435\u0432
# \u044D\u0442\u043E \u0441\u0438\u043B\u044C\u043D\u044B\u0439 \u0441\u0438\u0433\u043D\u0430\u043B \u00AB\u043D\u0435 \u043D\u0430\u0448\u00BB. \u041F\u0440\u043E\u0432\u0435\u0440\u044F\u0435\u0442\u0441\u044F \u0442\u043E\u043B\u044C\u043A\u043E \u0432 hidden-owner \u0432\u0435\u0442\u043A\u0435.
_HIDDEN_FOREIGN_LETTERS = re.compile(r"[\u00F6\u00FC\u011F\u0259\u00E7\u015F\u0131\u0130\u00D6\u00DC\u011E\u018F\u00C7\u015E]")


def _letter_count(s: str) -> int:
    """\u0421\u043A\u043E\u043B\u044C\u043A\u043E \u0432 \u0441\u0442\u0440\u043E\u043A\u0435 \u0438\u043C\u0435\u043D\u043D\u043E \u0431\u0443\u043A\u0432 (\u0431\u0435\u0437 \u0446\u0438\u0444\u0440, \u043F\u0440\u043E\u0431\u0435\u043B\u043E\u0432, \u043F\u0443\u043D\u043A\u0442\u0443\u0430\u0446\u0438\u0438 \u0438 unicode-\u043C\u0443\u0441\u043E\u0440\u0430)."""
    return sum(1 for ch in s if ch.isalpha())


def is_foreign_text(text: str | None) -> bool:
    """Hard script reject requested by the tracker policy: Arabic/Persian only."""
    if not text or not getattr(settings, "block_arabic_script", True):
        return False
    return bool(_ARABIC_SCRIPT.search(text))


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


def is_russian_name(name: str | None) -> bool:
    """Имя/фамилия на славянской кириллице («Дмитрий», «Іван», «Я»). Одна кириллическая буква
    внутри латиницы («Jоhn») не считается. Используется только при SELLER_NAME_SIGNAL=true."""
    if not name or _NON_SLAVIC.search(name):
        return False
    return bool(re.search(r"[Ѐ-ӿ]{2,}", name) or (_CYR.search(name) and not re.search(r"[A-Za-z]", name)))


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
        # Minimal seller policy: keep explicit blacklist and block Arabic/Persian
        # script anywhere visible in the seller profile. All former nationality,
        # phone, bio, level, NFT-count and paid-message heuristics are intentionally
        # non-blocking. Username presence/currentness is enforced by tracker/queue.
        if self._match(l, self.black_ids, self.black_names):
            return SellerResult(False, "BLACKLIST", "seller in blacklist")

        visible_text = (
            l.seller_name,
            l.seller_about,
            l.seller_channel,
            l.seller_gift_text,
            l.seller_business,
            l.seller_extra_text,
        )
        if any(is_foreign_text(value) for value in visible_text):
            return SellerResult(False, "ARABIC_SCRIPT", "Arabic/Persian script in seller profile")

        return SellerResult(True, "RELAXED", "seller filters relaxed")
