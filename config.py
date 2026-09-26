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


_TRUE = {"1", "true", "yes", "on", "да", "y", "t"}
_FALSE = {"0", "false", "no", "off", "нет", "n", "f"}


def _bool_strict(v: str) -> bool:
    """Для /set: явный булев ввод, иначе ValueError (чтобы опечатка не отключала фильтр молча)."""
    s = (v or "").strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"ожидается true/false, а не {v!r}")


def _choice(v: str, allowed: set[str]) -> str:
    """Для /set: значение из набора, иначе ValueError (чтобы мусор не принимался молча)."""
    s = (v or "").strip().lower()
    if s not in allowed:
        raise ValueError(f"ожидается одно из {sorted(allowed)}, а не {v!r}")
    return s


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


def _chat_thread(s: str) -> tuple[int, int | None]:
    """'-1004297759767' или '-1004297759767/67' → (chat_id, thread_id|None).
    Тема (topic) форум-группы: число после / — это ID темы из ссылки t.me/c/<...>/<thread>."""
    chat_s, _, thread_s = s.strip().partition("/")
    return int(chat_s), (int(thread_s) if thread_s.strip() else None)


def _price_routes(v: str | None) -> list[tuple[int, int | None, int, int | None]]:
    """Разбор PRICE_ROUTES: 'lo-hi:chat[/topic]' через запятую. hi пустой = без верхней границы.
    Пример: '250-1000:-100..,5000-:-100../67'. Цена в Stars. Пустая строка — маршрутов нет."""
    routes: list[tuple[int, int | None, int, int | None]] = []
    for part in (v or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        rng, _, dest = part.partition(":")
        lo_s, _, hi_s = rng.partition("-")
        lo = int(lo_s or 0)
        hi = int(hi_s) if hi_s.strip() else None
        chat, thread = _chat_thread(dest)
        routes.append((lo, hi, chat, thread))
    return routes


def _backdrop_routes(v: str | None) -> list[tuple[str, int, int | None]]:
    """Разбор BACKDROP_ROUTES: 'название_фона:chat[/topic]' через запятую. Совпадение по подстроке.
    Пример: 'Black:-100../67'. Пустая строка — маршрутов по фону нет."""
    routes: list[tuple[str, int, int | None]] = []
    for part in (v or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, dest = part.rpartition(":")
        if not sep or not name.strip() or not dest.strip():
            raise ValueError(f"нужен формат Название:ID[/тема], а не {part!r}")
        chat, thread = _chat_thread(dest)
        routes.append((name.strip(), chat, thread))
    return routes


@dataclass
class Settings:
    # --- Telegram / секреты ---
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    target_chat_id: int = 0
    price_routes: list[tuple[int, int | None, int]] = field(default_factory=list)  # каналы по диапазону цены
    backdrop_routes: list[tuple[str, int]] = field(default_factory=list)  # каналы по фону подарка (тема)
    admin_ids: set[int] = field(default_factory=set)
    allow_all_users: bool = True
    # --- подписка на канал (оплата TON) ---
    sub_chat_id: int = 0                 # куда пускать подписчиков (0 = TARGET_CHAT_ID)
    sub_ton_address: str = ""            # кошелёк для оплаты
    toncenter_api_key: str = ""          # необязательно: снимает лимит 1 запрос/сек у toncenter

    # --- фильтр цены ---
    min_stars: int = 5000
    collection_floor_mode: str = "skip"      # slow — дешёвые коллекции реже; skip — не отслеживать вовсе
    low_priority_poll_interval: float = 60.0 # страховочный обход дешёвых коллекций
    low_priority_hot_min: float = 10.0       # дешёвая коллекция опрашивается по изменению не чаще раза в N сек
    collection_min_floor: int = 4500         # отслеживать только коллекции с флором на маркете от N Stars (0 = все)
    min_ton: float = 0.0
    allow_ton_only: bool = False

    # --- фильтр продавца ---
    russian_only: bool = True
    unknown_seller_policy: str = "block"      # allow | block
    require_free_messages: bool = True        # продавцу можно написать бесплатно (без платных сообщений / Premium-only)
    max_seller_nfts: int = 12                 # не больше N NFT в профиле продавца (0 = не проверять)
    max_seller_level: int = 5                 # максимальный Level продавца (0 = не проверять)
    unique_gifts: bool = True                 # один и тот же NFT не отправлять повторно
    unique_sellers: bool = True               # одного и того же продавца не отправлять повторно
    seller_repeat_hours: float = 0.0          # 0 = никогда не повторять продавца
    seller_phone_prefixes: list[str] = field(default_factory=lambda: ["7", "380"])

    # --- трекер ---
    initial_sync: bool = True
    initial_sync_pages: int = 0              # 0 = все страницы (полная синхронизация, рекомендуется)
    sync_passes: int = 3                     # доп. проходы полной синхронизации, если выдача «уехала»
    resync_retry_seconds: float = 300.0
    max_same_collection_streak: int = 1      # не больше N отправок подряд из одной коллекции (0 = без лимита)
    tracker_interval: float = 1.0
    page_limit: int = 30
    live_max_pages: int = 5
    recovery_max_pages: int = 20
    catalog_interval: float = 1.0            # как часто смотреть счётчики всех коллекций (1 запрос); можно 0.3–0.5 для скорости
    cold_poll_interval: float = 15.0        # страховочный опрос без изменений счётчика; новые лоты и так ловит каталог за 2с
    market_backup_minutes: float = 30.0      # как часто сохранять снимок рынка в Telegram
    market_snapshot_max_age_hours: float = 6.0
    collections: list[str] = field(default_factory=list)   # пусто = все коллекции

    # --- sessions / rate limits ---
    max_concurrent_sessions: int = 20
    session_rps: float = 1.0
    global_rps: float = 100.0
    connect_concurrency: int = 5
    accounts_per_proxy: int = 3              # сколько аккаунтов можно посадить на один прокси
    startup_delay: float = 25.0              # пауза перед подключением аккаунтов при запуске (защита при деплое)

    # --- обработка ---
    processor_workers: int = 8
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

    def route_chat(self, stars: int | None, backdrop: str | None = None) -> tuple[int, int | None]:
        """Куда слать лот → (chat_id, thread_id): сначала по фону, затем по цене, иначе общий канал."""
        if backdrop:
            bl = backdrop.lower()
            for name, chat, thread in self.backdrop_routes:
                if name.lower() in bl:
                    return chat, thread
        if stars is not None:
            for lo, hi, chat, thread in self.price_routes:
                if stars >= lo and (hi is None or stars < hi):
                    return chat, thread
        return self.target_chat_id, None

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
    "MIN_STARS": ("min_stars", lambda v: max(0, int(v))),
    "TARGET_CHAT_ID": ("target_chat_id", lambda v: int(v)),
    "PRICE_ROUTES": ("price_routes", _price_routes),
    "BACKDROP_ROUTES": ("backdrop_routes", _backdrop_routes),
    "COLLECTION_MIN_FLOOR": ("collection_min_floor", lambda v: max(0, int(v))),
    "COLLECTION_FLOOR_MODE": ("collection_floor_mode", lambda v: _choice(v, {"skip", "slow"})),
    "LOW_PRIORITY_POLL_INTERVAL": ("low_priority_poll_interval", lambda v: max(5.0, float(v))),
    "MIN_TON": ("min_ton", lambda v: float(v.replace(",", "."))),
    "ALLOW_TON_ONLY": ("allow_ton_only", _bool_strict),
    "RUSSIAN_ONLY": ("russian_only", _bool_strict),
    "UNKNOWN_SELLER_POLICY": ("unknown_seller_policy", lambda v: _choice(v, {"allow", "block"})),
    "SELLER_PHONE_PREFIXES": ("seller_phone_prefixes", _str_list),
    "TRACKER_INTERVAL": ("tracker_interval", lambda v: max(0.2, float(v))),
    "PAGE_LIMIT": ("page_limit", lambda v: max(1, min(100, int(v)))),
    "SESSION_RPS": ("session_rps", lambda v: max(0.05, float(v))),
    "GLOBAL_RPS": ("global_rps", lambda v: max(0.0, float(v))),
    "MAX_CONCURRENT_SESSIONS": ("max_concurrent_sessions", lambda v: max(1, int(v))),
    "TEST_MODE": ("test_mode", _bool_strict),
    "UNIQUE_GIFTS": ("unique_gifts", _bool_strict),
    "REQUIRE_FREE_MESSAGES": ("require_free_messages", _bool_strict),
    "MAX_SELLER_NFTS": ("max_seller_nfts", lambda v: max(0, int(v))),
    "MAX_SELLER_LEVEL": ("max_seller_level", lambda v: max(0, int(v))),
    "COLD_POLL_INTERVAL": ("cold_poll_interval", lambda v: max(1.0, float(v))),
    "CATALOG_INTERVAL": ("catalog_interval", lambda v: max(0.3, float(v))),
    "MAX_SAME_COLLECTION_STREAK": ("max_same_collection_streak", lambda v: max(0, int(v))),
    "UNIQUE_SELLERS": ("unique_sellers", _bool_strict),
    "SELLER_REPEAT_HOURS": ("seller_repeat_hours", lambda v: max(0.0, float(v))),
    "ALLOW_ALL_USERS": ("allow_all_users", _bool_strict),
    "FETCH_FULL_SELLER": ("fetch_full_seller", _bool_strict),
}


def _from_env() -> Settings:
    load_dotenv(ENV_PATH, override=True)
    e = os.environ.get
    return Settings(
        api_id=_int(e("API_ID"), 0),
        api_hash=(e("API_HASH") or "").strip().strip('"'),
        bot_token=(e("BOT_TOKEN") or "").strip().strip('"'),
        target_chat_id=_int(e("TARGET_CHAT_ID"), 0),
        price_routes=_price_routes(e("PRICE_ROUTES")),
        backdrop_routes=_backdrop_routes(e("BACKDROP_ROUTES")),
        admin_ids=_int_set(e("ADMIN_IDS")),
        allow_all_users=_bool(e("ALLOW_ALL_USERS"), True),
        sub_chat_id=_int(e("SUB_CHAT_ID"), 0),
        sub_ton_address=(e("SUB_TON_ADDRESS") or "").strip(),
        toncenter_api_key=(e("TONCENTER_API_KEY") or "").strip(),
        min_stars=_int(e("MIN_STARS"), 5000),
        collection_min_floor=max(0, _int(e("COLLECTION_MIN_FLOOR"), 4500)),
        collection_floor_mode=(e("COLLECTION_FLOOR_MODE") or "skip").strip().lower(),
        low_priority_poll_interval=max(5.0, _float(e("LOW_PRIORITY_POLL_INTERVAL"), 60.0)),
        low_priority_hot_min=max(1.0, _float(e("LOW_PRIORITY_HOT_MIN"), 10.0)),
        min_ton=_float(e("MIN_TON"), 0.0),
        allow_ton_only=_bool(e("ALLOW_TON_ONLY"), False),
        russian_only=_bool(e("RUSSIAN_ONLY"), True),
        unknown_seller_policy=(e("UNKNOWN_SELLER_POLICY") or "block").strip().lower(),
        unique_gifts=_bool(e("UNIQUE_GIFTS"), True),
        require_free_messages=_bool(e("REQUIRE_FREE_MESSAGES"), True),
        max_seller_nfts=max(0, _int(e("MAX_SELLER_NFTS"), 12)),
        max_seller_level=max(0, _int(e("MAX_SELLER_LEVEL"), 5)),
        cold_poll_interval=max(1.0, _float(e("COLD_POLL_INTERVAL"), 15.0)),
        market_backup_minutes=max(5.0, _float(e("MARKET_BACKUP_MINUTES"), 30.0)),
        market_snapshot_max_age_hours=max(0.5, _float(e("MARKET_SNAPSHOT_MAX_AGE_HOURS"), 6.0)),
        unique_sellers=_bool(e("UNIQUE_SELLERS"), True),
        seller_repeat_hours=max(0.0, _float(e("SELLER_REPEAT_HOURS"), 0.0)),
        seller_phone_prefixes=_str_list(e("SELLER_PHONE_PREFIXES") or "7,380"),
        initial_sync=_bool(e("INITIAL_SYNC"), True),
        initial_sync_pages=max(0, _int(e("INITIAL_SYNC_PAGES"), 0)),
        sync_passes=max(1, _int(e("SYNC_PASSES"), 3)),
        resync_retry_seconds=max(10.0, _float(e("RESYNC_RETRY_SECONDS"), 300.0)),
        max_same_collection_streak=max(0, _int(e("MAX_SAME_COLLECTION_STREAK"), 1)),
        tracker_interval=max(0.2, _float(e("TRACKER_INTERVAL"), 1.0)),
        page_limit=max(1, min(100, _int(e("PAGE_LIMIT"), 30))),
        live_max_pages=max(1, _int(e("LIVE_MAX_PAGES"), 5)),
        recovery_max_pages=max(1, _int(e("RECOVERY_MAX_PAGES"), 20)),
        catalog_interval=max(0.3, _float(e("CATALOG_INTERVAL"), 1.0)),
        collections=_str_list(e("COLLECTIONS")),
        max_concurrent_sessions=max(1, _int(e("MAX_CONCURRENT_SESSIONS"), 20)),
        session_rps=max(0.05, _float(e("SESSION_RPS"), 1.0)),
        global_rps=max(0.0, _float(e("GLOBAL_RPS"), 100.0)),
        connect_concurrency=max(1, _int(e("CONNECT_CONCURRENCY"), 5)),
        accounts_per_proxy=max(1, _int(e("ACCOUNTS_PER_PROXY"), 3)),
        startup_delay=max(0.0, _float(e("STARTUP_DELAY"), 25.0)),
        processor_workers=max(1, _int(e("PROCESSOR_WORKERS"), 8)),
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


def export_runtime_overrides() -> dict[str, str]:
    """Настройки, изменённые командой /set, — сохраняются в копию в Telegram и переживают деплой."""
    return _load_runtime_overrides()


def import_runtime_overrides(data: dict) -> None:
    clean = {k: str(v) for k, v in (data or {}).items() if k in RUNTIME_KEYS}
    if not clean:
        return
    current = _load_runtime_overrides()
    current.update(clean)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_SETTINGS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    reload_settings()


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
