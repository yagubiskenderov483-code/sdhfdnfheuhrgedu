"""
SellerFilter — отдельный модуль, алгоритм можно менять независимо от остального кода.

ВАЖНО: Telegram API НЕ отдаёт национальность, страну или язык чужого пользователя.
  • User.lang_code заполняется только для ботов (и только в контексте бота) — для продавцов недоступен;
  • UserFull не содержит страны/локали.
Определять «русского продавца» по имени, фамилии, фото или username запрещено (и ненадёжно).

Поэтому RUSSIAN_ONLY использует ТОЛЬКО объективные данные, по порядку:
  1. data/seller_blacklist.txt  (user id или @username)          → BLOCK
  2. data/seller_whitelist.txt  (user id или @username)          → PASS
  3. номер телефона продавца, ЕСЛИ Telegram его отдал (User.phone виден только когда
     продавец открыл номер в приватности / он у вас в контактах) и он начинается с
     SELLER_PHONE_PREFIXES (по умолчанию 7 — Россия; учтите, +7 это также Казахстан) → PASS
  4. иначе — критерий неизвестен → UNKNOWN_SELLER_POLICY: allow (пропустить) | block (отсеять)

RUSSIAN_ONLY=false → фильтр пропускает всех (кроме blacklist).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import settings
from database.models import Listing


@dataclass
class SellerResult:
    passed: bool
    code: str       # BLACKLIST | WHITELIST | PHONE_MATCH | UNKNOWN_ALLOWED | UNKNOWN_BLOCKED | DISABLED
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

        if settings.unknown_seller_policy == "block":
            return SellerResult(False, "UNKNOWN_BLOCKED", "no objective data (policy=block)")
        return SellerResult(True, "UNKNOWN_ALLOWED", "no objective data (policy=allow)")
