"""
Конфигурация трекера.

Порядок приоритета значений:
  1. data/runtime_settings.json  (то, что меняется командой /set в админ-боте)
  2. .env
  3. значения по умолчанию ниже

Секреты (BOT_TOKEN, API_HASH) берутся ТОЛЬКО из .env и никогда не логируются.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
RUNTIME_SETTINGS_PATH = DATA_DIR / "runtime_settings.json"


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


def _int(v: str | None, default: int) -> int:
    try:
        return int(str(v).strip()) if v not in (None, "") else default
    except ValueError:
        return default


def _float(v: str | None, default: float) -> float:
    try:
        return float(str(v).strip().replace(",", ".")) if v not in (None, "") else default
    except ValueError:
        return default


def _int_set(v: str | None) -> set[int]:
    out: set[int] = set()
    for part in (v or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def _str_list(v: str | None) -> list[str]:
    return [p.strip() for p in (v or "").replace(";", ",").split(",") if p.strip()]


@dataclass
class Settings:
    # --- Telegram / секреты ---
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    target_chat_id: int = 0
    admin_ids: set[int] = field(default_factory=set)
    allow_all_users: bool = True

    # --- фильтр цены ---
    min_stars: int = 5000
    min_ton: float = 0.0
    allow_ton_only: bool = False

    # --- фильтр продавца ---
    russian_only: bool = True
    unknown_seller_policy: str = "block"      # allow | block
    unique_gifts: bool = True                 # один и тот же NFT не отправлять повторно
    unique_sellers: bool = True               # одного и того же продавца не отправлять повторно
    seller_repeat_hours: float = 0.0          # 0 = никогда не повторять продавца
    seller_phone_prefixes: list[str] = field(default_factory=lambda: ["7"])

    # --- трекер ---
    initial_sync: bool = True
    initial_sync_pages: int = 1
    tracker_interval: float = 1.0
    page_limit: int = 30
    live_max_pages: int = 5
    recovery_max_pages: int = 20
    catalog_interval: float = 30.0
    collections: list[str] = field(default_factory=list)   # пусто = все коллекции

    # --- sessions / rate limits ---
    max_concurrent_sessions: int = 20
    session_rps: float = 1.0
    global_rps: float = 30.0
    connect_concurrency: int = 5

    # --- обработка ---
    processor_workers: int = 4
    fetch_full_seller: bool = True
    seller_cache_ttl: int = 3600
    send_interval: float = 1.1

    # --- test mode ---
    test_mode: bool = False
    test_max_messages: int = 5
    test_chat_id: int = 0

    # --- прочее ---
    ton_label: str = "TON"
    timezone: str = "Europe/Moscow"
    session_backup_key: str = ""
    sessions_backup_chat_id: int = 0
    database_path: str = "data/tracker.db"
    sessions_dir: str = "telegram_sessions"
    template_path: str = "templates/listing.html"
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @property
    def base_dir(self) -> Path:
        return BASE_DIR

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else BASE_DIR / p

    def public_view(self) -> dict[str, Any]:
        """Настройки без секретов — для /settings."""
        hidden = {"api_hash", "bot_token", "session_backup_key"}
        out = {}
        for f in fields(self):
            if f.name in hidden:
                continue
            out[f.name] = getattr(self, f.name)
        out["api_hash"] = "***" if self.api_hash else "(не задан)"
        out["bot_token"] = "***" if self.bot_token else "(не задан)"
        return out

    def secrets(self) -> list[str]:
        return [s for s in (self.api_hash, self.bot_token, self.session_backup_key) if s]


# Ключи, которые можно менять из бота командой /set KEY VALUE
RUNTIME_KEYS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "MIN_STARS": ("min_stars", lambda v: int(v)),
    "MIN_TON": ("min_ton", lambda v: float(v.replace(",", "."))),
    "ALLOW_TON_ONLY": ("allow_ton_only", lambda v: _bool(v, False)),
    "RUSSIAN_ONLY": ("russian_only", lambda v: _bool(v, True)),
    "UNKNOWN_SELLER_POLICY": ("unknown_seller_policy", lambda v: v.strip().lower()),
    "SELLER_PHONE_PREFIXES": ("seller_phone_prefixes", _str_list),
    "TRACKER_INTERVAL": ("tracker_interval", lambda v: max(0.2, float(v))),
    "PAGE_LIMIT": ("page_limit", lambda v: max(1, min(100, int(v)))),
    "SESSION_RPS": ("session_rps", lambda v: max(0.05, float(v))),
    "GLOBAL_RPS": ("global_rps", lambda v: max(0.0, float(v))),
    "MAX_CONCURRENT_SESSIONS": ("max_concurrent_sessions", lambda v: max(1, int(v))),
    "TEST_MODE": ("test_mode", lambda v: _bool(v, False)),
    "UNIQUE_GIFTS": ("unique_gifts", lambda v: _bool(v, True)),
    "UNIQUE_SELLERS": ("unique_sellers", lambda v: _bool(v, True)),
    "SELLER_REPEAT_HOURS": ("seller_repeat_hours", lambda v: max(0.0, float(v))),
    "ALLOW_ALL_USERS": ("allow_all_users", lambda v: _bool(v, True)),
    "FETCH_FULL_SELLER": ("fetch_full_seller", lambda v: _bool(v, True)),
}


def _from_env() -> Settings:
    load_dotenv(ENV_PATH, override=True)
    e = os.environ.get
    return Settings(
        api_id=_int(e("API_ID"), 0),
        api_hash=(e("API_HASH") or "").strip().strip('"'),
        bot_token=(e("BOT_TOKEN") or "").strip().strip('"'),
        target_chat_id=_int(e("TARGET_CHAT_ID"), 0),
        admin_ids=_int_set(e("ADMIN_IDS")),
        allow_all_users=_bool(e("ALLOW_ALL_USERS"), True),
        min_stars=_int(e("MIN_STARS"), 5000),
        min_ton=_float(e("MIN_TON"), 0.0),
        allow_ton_only=_bool(e("ALLOW_TON_ONLY"), False),
        russian_only=_bool(e("RUSSIAN_ONLY"), True),
        unknown_seller_policy=(e("UNKNOWN_SELLER_POLICY") or "block").strip().lower(),
        unique_gifts=_bool(e("UNIQUE_GIFTS"), True),
        unique_sellers=_bool(e("UNIQUE_SELLERS"), True),
        seller_repeat_hours=max(0.0, _float(e("SELLER_REPEAT_HOURS"), 0.0)),
        seller_phone_prefixes=_str_list(e("SELLER_PHONE_PREFIXES") or "7"),
        initial_sync=_bool(e("INITIAL_SYNC"), True),
        initial_sync_pages=max(1, _int(e("INITIAL_SYNC_PAGES"), 1)),
        tracker_interval=max(0.2, _float(e("TRACKER_INTERVAL"), 1.0)),
        page_limit=max(1, min(100, _int(e("PAGE_LIMIT"), 30))),
        live_max_pages=max(1, _int(e("LIVE_MAX_PAGES"), 5)),
        recovery_max_pages=max(1, _int(e("RECOVERY_MAX_PAGES"), 20)),
        catalog_interval=max(5.0, _float(e("CATALOG_INTERVAL"), 30.0)),
        collections=_str_list(e("COLLECTIONS")),
        max_concurrent_sessions=max(1, _int(e("MAX_CONCURRENT_SESSIONS"), 20)),
        session_rps=max(0.05, _float(e("SESSION_RPS"), 1.0)),
        global_rps=max(0.0, _float(e("GLOBAL_RPS"), 30.0)),
        connect_concurrency=max(1, _int(e("CONNECT_CONCURRENCY"), 5)),
        processor_workers=max(1, _int(e("PROCESSOR_WORKERS"), 4)),
        fetch_full_seller=_bool(e("FETCH_FULL_SELLER"), True),
        seller_cache_ttl=max(0, _int(e("SELLER_CACHE_TTL"), 3600)),
        send_interval=max(0.0, _float(e("SEND_INTERVAL"), 1.1)),
        test_mode=_bool(e("TEST_MODE"), False),
        test_max_messages=max(0, _int(e("TEST_MAX_MESSAGES"), 5)),
        test_chat_id=_int(e("TEST_CHAT_ID"), 0),
        ton_label=(e("TON_LABEL") or "TON").strip(),
        timezone=(e("TIMEZONE") or "Europe/Moscow").strip(),
        session_backup_key=(e("SESSION_BACKUP_KEY") or "").strip(),
        sessions_backup_chat_id=_int(e("SESSIONS_BACKUP_CHAT_ID"), 0),
        database_path=(e("DATABASE_PATH") or "data/tracker.db").strip(),
        sessions_dir=(e("SESSIONS_DIR") or "telegram_sessions").strip(),
        template_path=(e("TEMPLATE_PATH") or "templates/listing.html").strip(),
        log_level=(e("LOG_LEVEL") or "INFO").strip().upper(),
    )


def _load_runtime_overrides() -> dict[str, str]:
    try:
        return json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_settings() -> Settings:
    s = _from_env()
    for key, raw in _load_runtime_overrides().items():
        if key in RUNTIME_KEYS:
            attr, conv = RUNTIME_KEYS[key]
            try:
                setattr(s, attr, conv(str(raw)))
            except (ValueError, TypeError):
                pass
    return s


def set_runtime_value(key: str, raw: str) -> Any:
    """Меняет настройку на лету и сохраняет её в data/runtime_settings.json."""
    key = key.upper()
    if key not in RUNTIME_KEYS:
        raise KeyError(key)
    attr, conv = RUNTIME_KEYS[key]
    value = conv(raw)
    setattr(settings, attr, value)
    data = _load_runtime_overrides()
    data[key] = raw
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return value


def reload_settings() -> Settings:
    """Перечитать .env + runtime overrides, обновив глобальный объект на месте."""
    fresh = load_settings()
    for f in fields(fresh):
        setattr(settings, f.name, getattr(fresh, f.name))
    return settings


settings: Settings = load_settings()
