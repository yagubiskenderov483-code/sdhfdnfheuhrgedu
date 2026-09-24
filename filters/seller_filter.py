"""
SellerFilter — отдельный модуль, алгоритм можно менять независимо от остального кода.

Telegram API НЕ отдаёт национальность или страну чужого пользователя (User.lang_code —
только для ботов). Имя, фамилия, фото и username НЕ используются.

RUSSIAN_ONLY=true пропускает продавца, только если есть объективный признак, по порядку:
  1. data/seller_blacklist.txt (ID или @username)                       → отсеять
  2. data/seller_whitelist.txt                                          → пропустить
  3. телефон виден (Telegram отдал User.phone) и начинается с
     SELLER_PHONE_PREFIXES (по умолчанию 7)                             → пропустить, иначе отсеять
  4. описание профиля (bio, UserFull.about) написано по-русски:
     есть русские буквы (ы э ъ ё) / кириллица и нет украинских/белорусских/
     казахских букв (і ї є ґ ў ә ғ қ ң ө ұ ү һ)                         → пропустить
  5. ничего из этого нет → UNKNOWN_SELLER_POLICY: block (отсеять, по умолчанию) | allow

RUSSIAN_ONLY=false → пропускаются все (кроме blacklist).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config import settings
from database.models import Listing

_CYR = re.compile(r"[а-яё]", re.IGNORECASE)
_RU_ONLY = re.compile(r"[ыэъё]", re.IGNORECASE)
_NOT_RU = re.compile(r"[іїєґўәғқңөұүһ]", re.IGNORECASE)
_LAT_WORD = re.compile(r"[a-z]{3,}", re.IGNORECASE)


def is_russian_text(text: str | None) -> bool:
    """Текст написан по-русски (а не на другом кириллическом языке / латиницей)."""
    if not text:
        return False
    t = text.strip()
    cyr = len(_CYR.findall(t))
    if cyr < 4 or _NOT_RU.search(t):
        return False
    # кириллица должна преобладать над латиницей
    lat = sum(len(w) for w in _LAT_WORD.findall(t))
    if cyr < lat:
        return False
    return bool(_RU_ONLY.search(t)) or cyr >= 6


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

        if settings.unknown_seller_policy == "allow":
            return SellerResult(True, "UNKNOWN_ALLOWED", "no objective data (policy=allow)")
        return SellerResult(False, "UNKNOWN_BLOCKED", "no Russian bio / phone / whitelist (policy=block)")
