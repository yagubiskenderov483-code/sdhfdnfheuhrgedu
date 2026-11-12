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
import math
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


def _bot_list(v: str | None) -> list[str]:
    """Как _str_list, но убирает @ и дубликаты, сохраняя порядок (первый задан = первым пробуется).
    Валидация: Telegram username должен быть 5-32 символа, [A-Za-z0-9_], начинаться с буквы.
    Невалидные имена игнорируются с warning в stderr — лучше тихо дропнуть опечатку
    'funstat 2' чем взять и получить resolveUsername error позже."""
    import re as _re
    import sys as _sys
    _TG_USERNAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
    seen: set[str] = set()
    out: list[str] = []
    for p in _str_list(v):
        name = p.lstrip("@").strip()
        key = name.lower()
        if not name:
            continue
        if key in seen:
            continue
        if not _TG_USERNAME_RE.match(name):
            print(f"WARNING: LOOKUP_BOTS: пропущен невалидный username {name!r} "
                  "(нужно: 5-32 символа, [A-Za-z0-9_], начинается с буквы)", file=_sys.stderr)
            continue
        seen.add(key)
        out.append(name)
    return out


def _lookup_bot_username(v: str | None) -> str:
    """Валидация одного bot username (для primary LOOKUP_BOT_USERNAME).
    Невалидный → пустая строка + warning в stderr. Пустой → пустая строка (норма)."""
    import re as _re
    import sys as _sys
    _TG_USERNAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
    s = (v or "").strip().lstrip("@").strip()
    if not s:
        return ""
    if not _TG_USERNAME_RE.match(s):
        print(f"WARNING: LOOKUP_BOT_USERNAME={s!r} невалиден — очищено "
              "(нужно: 5-32 символа, [A-Za-z0-9_], начинается с буквы)", file=_sys.stderr)
        return ""
    return s


def _bot_cmds(v: str | None) -> dict[str, str]:
    """Разбор LOOKUP_CMDS: 'funstat:/human,infostat:/find' → {'funstat': '/human {name}'}.
    Если шаблон не содержит '{name}', подставляется автоматически через пробел."""
    out: dict[str, str] = {}
    for part in (v or "").replace(";", ",").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        bot, _, cmd = part.partition(":")
        bot = bot.strip().lstrip("@").lower()
        cmd = cmd.strip()
        if not bot or not cmd:
            continue
        if "{name}" not in cmd:
            cmd = f"{cmd} {{name}}"
        out[bot] = cmd
    return out


def _chat_thread(s: str) -> tuple[int, int | None]:
    """'-1004297759767' или '-1004297759767/67' → (chat_id, thread_id|None).
    Тема (topic) форум-группы: число после / — это ID темы из ссылки t.me/c/<...>/<thread>."""
    chat_s, _, thread_s = s.strip().partition("/")
    return int(chat_s), (int(thread_s) if thread_s.strip() else None)


def _safe_parse(fn, raw, name):
    """Битое значение в .env не должно ронять бота — работаем без маршрутов и пишем предупреждение."""
    try:
        return fn(raw)
    except (ValueError, TypeError) as e:
        import sys
        print(f"WARNING: {name} в .env некорректен ({e}) — маршруты отключены", file=sys.stderr)
        return []


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
    # Отдельный бот для пользовательского интерфейса подписки + Stars (Maker: оплата Stars
    # должна идти в siqveoabot, а не в основной админ-бот). Если пусто — подписка идёт через
    # основной BOT_TOKEN, как раньше.
    sub_bot_token: str = ""
    target_chat_id: int = 0
    # (lo, hi, chat_id, thread_id) — каналы по диапазону цены. Аннотация ранее была
    # неточной (3-tuple вместо 4-tuple) — для IDE/mypy теперь правильная.
    price_routes: list[tuple[int, int | None, int, int | None]] = field(default_factory=list)
    backdrop_routes: list[tuple[str, int, int | None]] = field(default_factory=list)  # (имя_фона, chat_id, thread_id)
    admin_ids: set[int] = field(default_factory=set)
    allow_all_users: bool = True
    # --- подписка на канал (оплата TON) ---
    sub_chat_id: int = 0                 # куда пускать подписчиков (0 = TARGET_CHAT_ID)
    sub_ton_address: str = ""            # кошелёк для оплаты
    toncenter_api_key: str = ""          # необязательно: снимает лимит 1 запрос/сек у toncenter
    # Crypto Bot (CryptoPay) — https://help.crypt.bot/crypto-pay-api. Если задан — оплата
    # через @send/@CryptoBot создаёт персональный invoice и ловится автоматически по API.
    # Без токена кнопка «Crypto Bot» ведёт на статическую ссылку и выдаётся вручную.
    crypto_bot_api_token: str = ""

    # --- фильтр цены ---
    min_stars: int = 5000
    collection_floor_mode: str = "skip"      # slow — дешёвые коллекции реже; skip — не отслеживать вовсе
    low_priority_poll_interval: float = 60.0 # страховочный обход дешёвых коллекций
    low_priority_hot_min: float = 10.0       # дешёвая коллекция опрашивается по изменению не чаще раза в N сек
    collection_min_floor: int = 4500         # отслеживать только коллекции с флором на маркете от N Stars (0 = все)
    min_ton: float = 0.0
    priority_floor: int = 2500           # коллекции с флором от N ⭐ опрашиваются чаще и первыми (0 = выкл)
    priority_share: float = 0.65         # какая доля мощности аккаунтов уходит на дорогие коллекции
    priority_poll_interval: float = 1.5  # ускорено (было 2.5): как часто опрашивать каждую дорогую коллекцию
    auto_remove_dead: bool = True        # сессию, которую снёс Telegram, удалять из трекера автоматически
    max_lot_age: int = 180               # лот старше 3 мин уже не свежий — не слать (0 = выкл)
    route_by: str = "floor"              # тема по: floor (флор коллекции = цена гифта) | price (цена лота)
    rescue_above_top: bool = False         # «спасать» незнакомые лоты выше прошлого верха без роста счётчика (давало старые лоты)
    premium_over_floor_percent: int = 333  # наценка к флору >= N%: только редкая модель/фон или Monochrome (0 = выкл)
    rare_attribute_percent: float = 1.5    # «редкая» модель/фон — редкость <= N%
    max_above_floor: int = 500           # не слать лоты дороже флора коллекции больше чем на N% (0 = выкл).
    # По умолчанию 500% (6х флора): фильтрует явный overprice типа «NFT с флором 400⭐ выставлен за 20k»,
    # но пропускает редкие атрибуты (2-5х от флора — нормально). /set MAX_ABOVE_FLOOR N для изменения.
    allow_ton_only: bool = False

    # --- фильтр продавца ---
    russian_only: bool = True
    unknown_seller_policy: str = "block"      # allow | block
    seller_name_signal: bool = False          # имя/фамилия кириллицей = русскоязычный продавец (по умолчанию выкл.)
    phone_mismatch_blocks: bool = True        # чужой телефон отсеивает сразу; false — решают тексты профиля
    allow_hidden_owner: bool = False          # true: лоты, у которых Telegram скрыл владельца, идут в канал
    # Дорогие лоты фильтруются мягче: если цена >= этого значения, пропускаем «скрытого владельца»,
    # «нет @username», «короткое имя» — Maker хочет, чтобы дорогие (1500+⭐) активно выдавались,
    # пусть и без явного контакта с продавцом. 0 = выкл (старый жёсткий фильтр для всех).
    # Платные ЛС и «только Premium» — всё равно отсеиваем независимо от цены.
    soft_filter_stars: int = 1500
                                              # (RUSSIAN_ONLY к ним неприменим — профиля просто нет)
    lookup_bot_username: str = ""             # @username бота ручного поиска (напр. "funstat") —
                                              # для лотов со скрытым владельцем в пост добавится
                                              # кнопка «🔍 Найти в @<бот>». Пусто = кнопки нет.
                                              # Для множественного поиска используется LOOKUP_BOTS.
    lookup_bots: list[str] = field(default_factory=list)  # @username'ы запасных ботов поиска
                                              # (funstat снесли → автоматический fallback на infostat).
                                              # Формат: 'funstat,infostat,otherbot'. Первым пробуется
                                              # LOOKUP_BOT_USERNAME, затем эти по порядку. Пустой список
                                              # = только LOOKUP_BOT_USERNAME.
    lookup_cmd_template: str = "/human {name}"  # команда боту поиска, {name} = имя продавца.
                                              # Разные боты могут требовать своего синтаксиса:
                                              # funstat — '/human', infostat — '/find', и т.д.
                                              # Для per-bot шаблонов используется LOOKUP_CMDS.
    lookup_cmds: dict = field(default_factory=dict)  # {bot_username: cmd_template} per-bot шаблоны.
                                              # Формат env: 'LOOKUP_CMDS=funstat:/human,infostat:/find'.
                                              # Отсутствующий бот → используется lookup_cmd_template.
    auto_lookup: bool = True                  # трекер сам пишет боту поиска «/human <имя>», проверяет
                                              # кандидатов (открыт ли у них этот NFT) и дописывает
                                              # найденного владельца в пост
    lookup_sessions: list[str] = field(default_factory=list)  # аккаунты, которые пишут боту поиска
                                              # (пусто = первый рабочий аккаунт трекера)
    require_username: bool = True             # слать только продавцов с @username
    gift_page_owner: bool = False             # искать скрытого владельца через страницу подарка (+1 запрос
                                              # на лот, раскрывает ~1%) — по умолчанию выкл., ищет бот поиска
    lookup_max_age: int = 180                 # после поиска владельца лот старше 3 мин не слать
    lookup_check_onsale: bool = True          # после поиска проверить, что лот ещё продаётся
    lookup_lang_filter: bool = False          # найденного ботом поиска владельца проверять и по языку профиля
    lookup_required: bool = True              # скрытый владелец не найден ботом поиска → лот не слать
    lookup_dedicated: bool = True             # аккаунты поиска НЕ опрашивают маркет — только пишут боту
    lookup_interval: float = 1.5              # ускорено (было 3.0): пауза между запросами к боту поиска с одного аккаунта
    lookup_captcha_wait: float = 90.0         # ускорено: ждём капчу авто-солвером/админом (было 600)
    lookup_timeout: float = 15.0              # ускорено (было 25): сколько секунд ждать ответа бота поиска
    lookup_skip_over: int = 20                # в ответе бота поиска N+ человек — лот сразу пропустить (0 = выкл)
    lookup_resolve_per_hour: int = 60         # ускорено (было 38 → 60): при 28 аккаунтах resolve-лимит бил ~2785/запуск
    lookup_name_check: bool = True            # найденный аккаунт должен называться так же, как владелец NFT
    lookup_nomatch_check: int = 1             # имя в ответе бота не совпало ни у кого — проверить первых N
    lookup_max_candidates: int = 3            # сколько кандидатов из ответа проверять
    lookup_trust_name_match: bool = True      # если funstat вернул ОДНОГО кандидата с совпадающим именем —
                                              # верим без проверки «NFT виден в профиле» (многие прячут подарки)
    trade_bot_username: str = ""              # @username торгового бота (напр. "iostherobot") —
                                              # кнопка «Написать» открывает его с готовым
                                              # оффером: «@<бот> <ссылка на NFT> <сниженная_цена>».
                                              # Пусто = обычная кнопка «Написать» продавцу.
    require_free_messages: bool = True        # продавцу можно написать бесплатно (без платных сообщений / Premium-only)
    max_seller_nfts: int = 12                 # не больше N NFT в профиле продавца (0 = не проверять)
    max_seller_level: int = 5                 # максимальный Level продавца (0 = не проверять)
    unique_gifts: bool = True                 # один и тот же NFT не отправлять повторно
    unique_sellers: bool = True               # одного и того же продавца не отправлять повторно
    seller_repeat_hours: float = 0.0          # 0 = никогда не повторять продавца
    seller_phone_prefixes: list[str] = field(default_factory=lambda: ["7", "380"])

    # --- трекер ---
    start_fresh: bool = True                 # после запуска/деплоя слать только НОВЫЕ лоты (без лотов «пока был выключен»)
    initial_sync: bool = True
    initial_sync_pages: int = 0              # 0 = все страницы (полная синхронизация, рекомендуется)
    sync_mode: str = "fast"                  # fast: база = верхняя страница (FAST_SYNC_PAGES), коллекция сразу LIVE | full: читать всё
    fast_sync_pages: int = 1
    sync_passes: int = 3                     # доп. проходы полной синхронизации, если выдача «уехала»
    resync_retry_seconds: float = 300.0
    max_same_collection_streak: int = 1      # не больше N отправок подряд из одной коллекции (0 = без лимита)
    tracker_interval: float = 0.5            # ускорено (было 1.0)
    page_limit: int = 50                     # ускорено: больше лотов за запрос (было 30)
    live_max_pages: int = 5
    recovery_max_pages: int = 20
    catalog_interval: float = 0.5            # ускорено (было 1.0): как часто смотреть счётчики всех коллекций
    cold_poll_interval: float = 15.0        # ускорено (было 30): страховочный опрос без изменений счётчика
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
    processor_workers: int = 24
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
    old_bot_token: str = ""              # прошлый токен: забрать копию аккаунтов при смене бота
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
        if stars is not None and self.price_routes:
            for lo, hi, chat, thread in self.price_routes:
                if stars >= lo and (hi is None or stars < hi):
                    return chat, thread
            lo, _hi, chat, thread = min(self.price_routes, key=lambda r: r[0])
            if stars < lo:
                return chat, thread        # всё что ниже нижней границы (напр. <400) — в нижнюю тему (400-1000)
        return self.target_chat_id, None

    def public_view(self) -> dict[str, Any]:
        """Настройки без секретов — для /settings."""
        hidden = {"api_hash", "bot_token", "sub_bot_token", "crypto_bot_api_token",
                  "session_backup_key", "old_bot_token"}
        out = {}
        for f in fields(self):
            if f.name in hidden:
                continue
            out[f.name] = getattr(self, f.name)
        out["api_hash"] = "***" if self.api_hash else "(не задан)"
        out["bot_token"] = "***" if self.bot_token else "(не задан)"
        out["sub_bot_token"] = "***" if self.sub_bot_token else "(не задан — Stars/подписка идут через BOT_TOKEN)"
        out["crypto_bot_api_token"] = "***" if self.crypto_bot_api_token else "(не задан — Crypto Bot оплата ручная)"
        return out

    def secrets(self) -> list[str]:
        olds = [t.strip() for t in (self.old_bot_token or "").split(",")]
        return [s for s in (self.api_hash, self.bot_token, self.sub_bot_token, self.crypto_bot_api_token,
                            self.session_backup_key, self.old_bot_token, *olds) if s]


def _rng(lo: float, hi: float, cast=float):
    """Число в разумных пределах: слишком большие значения ломают даты/таймеры (опрос «никогда»)."""
    def f(v: str):
        x = cast(float(v.replace(",", ".")) if cast is float else v)
        if not math.isfinite(x):
            raise ValueError("нужно обычное число")
        return min(hi, max(lo, x))         # как раньше — подрезаем к границе, а не отказываем
    return f


def _chat_id(v: str) -> int:
    x = int(v)
    if x == 0:
        raise ValueError("ID чата не может быть 0")
    return x


# Ключи, которые можно менять из бота командой /set KEY VALUE
RUNTIME_KEYS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "MIN_STARS": ("min_stars", lambda v: max(0, int(v))),
    "TARGET_CHAT_ID": ("target_chat_id", _chat_id),
    "PRICE_ROUTES": ("price_routes", _price_routes),
    "BACKDROP_ROUTES": ("backdrop_routes", _backdrop_routes),
    "COLLECTION_MIN_FLOOR": ("collection_min_floor", lambda v: max(0, int(v))),
    "COLLECTION_FLOOR_MODE": ("collection_floor_mode", lambda v: _choice(v, {"skip", "slow"})),
    "LOW_PRIORITY_POLL_INTERVAL": ("low_priority_poll_interval", _rng(5, 3600)),
    "MIN_TON": ("min_ton", lambda v: float(v.replace(",", "."))),
    "MAX_ABOVE_FLOOR": ("max_above_floor", lambda v: max(0, int(v))),
    "PREMIUM_OVER_FLOOR_PERCENT": ("premium_over_floor_percent", lambda v: max(0, int(v))),
    "RESCUE_ABOVE_TOP": ("rescue_above_top", _bool_strict),
    "RARE_ATTRIBUTE_PERCENT": ("rare_attribute_percent", lambda v: max(0.1, min(100.0, float(v.replace(",", "."))))),
    "PRIORITY_FLOOR": ("priority_floor", lambda v: max(0, int(v))),
    "PRIORITY_SHARE": ("priority_share", lambda v: min(0.9, max(0.1, float(v.replace(",", "."))))),
    "PRIORITY_POLL_INTERVAL": ("priority_poll_interval", _rng(0.5, 600)),
    "SYNC_MODE": ("sync_mode", lambda v: _choice(v, {"fast", "full"})),
    "ROUTE_BY": ("route_by", lambda v: v.strip().lower() if v.strip().lower() in ("floor", "price") else "floor"),
    "ALLOW_TON_ONLY": ("allow_ton_only", _bool_strict),
    "RUSSIAN_ONLY": ("russian_only", _bool_strict),
    "UNKNOWN_SELLER_POLICY": ("unknown_seller_policy", lambda v: _choice(v, {"allow", "block"})),
    "SELLER_NAME_SIGNAL": ("seller_name_signal", _bool_strict),
    "PHONE_MISMATCH_BLOCKS": ("phone_mismatch_blocks", _bool_strict),
    "ALLOW_HIDDEN_OWNER": ("allow_hidden_owner", _bool_strict),
    "SOFT_FILTER_STARS": ("soft_filter_stars", lambda v: max(0, int(v))),
    "LOOKUP_BOT_USERNAME": ("lookup_bot_username", _lookup_bot_username),
    "LOOKUP_BOTS": ("lookup_bots", _bot_list),
    "LOOKUP_CMD_TEMPLATE": ("lookup_cmd_template",
                            lambda v: (v.strip() if "{name}" in v else f"{v.strip()} {{name}}")
                                       if v.strip() else "/human {name}"),
    "LOOKUP_CMDS": ("lookup_cmds", _bot_cmds),
    "AUTO_LOOKUP": ("auto_lookup", _bool_strict),
    "LOOKUP_SESSIONS": ("lookup_sessions", lambda v: [] if v.strip() in ("", "-") else _str_list(v)),
    "LOOKUP_DEDICATED": ("lookup_dedicated", _bool_strict),
    "LOOKUP_REQUIRED": ("lookup_required", _bool_strict),
    "REQUIRE_USERNAME": ("require_username", _bool_strict),
    "GIFT_PAGE_OWNER": ("gift_page_owner", _bool_strict),
    "LOOKUP_MAX_AGE": ("lookup_max_age", lambda v: max(0, min(86400, int(v)))),
    "START_FRESH": ("start_fresh", _bool_strict),
    "LOOKUP_CHECK_ONSALE": ("lookup_check_onsale", _bool_strict),
    "LOOKUP_LANG_FILTER": ("lookup_lang_filter", _bool_strict),
    "LOOKUP_TIMEOUT": ("lookup_timeout", _rng(5, 180)),
    "LOOKUP_INTERVAL": ("lookup_interval", _rng(2, 600)),   # min 2с — иначе funstat выдаёт капчу на каждый запрос
    "LOOKUP_CAPTCHA_WAIT": ("lookup_captcha_wait", _rng(30, 7200)),
    "LOOKUP_SKIP_OVER": ("lookup_skip_over", lambda v: max(0, min(1000, int(v)))),
    "LOOKUP_RESOLVE_PER_HOUR": ("lookup_resolve_per_hour", lambda v: max(1, min(200, int(v)))),
    "LOOKUP_NAME_CHECK": ("lookup_name_check", _bool_strict),
    "LOOKUP_TRUST_NAME_MATCH": ("lookup_trust_name_match", _bool_strict),
    "LOOKUP_NOMATCH_CHECK": ("lookup_nomatch_check", lambda v: max(0, min(10, int(v)))),
    "LOOKUP_MAX_CANDIDATES": ("lookup_max_candidates", lambda v: max(1, min(30, int(v)))),
    "TRADE_BOT_USERNAME": ("trade_bot_username", lambda v: v.strip().lstrip("@")),
    "SELLER_PHONE_PREFIXES": ("seller_phone_prefixes", _str_list),
    "TRACKER_INTERVAL": ("tracker_interval", _rng(0.2, 3600)),
    "PAGE_LIMIT": ("page_limit", lambda v: max(1, min(100, int(v)))),
    "SESSION_RPS": ("session_rps", _rng(0.05, 50)),
    "GLOBAL_RPS": ("global_rps", _rng(0, 1000)),
    "MAX_CONCURRENT_SESSIONS": ("max_concurrent_sessions", lambda v: max(1, min(1000, int(v)))),
    "TEST_MODE": ("test_mode", _bool_strict),
    "UNIQUE_GIFTS": ("unique_gifts", _bool_strict),
    "REQUIRE_FREE_MESSAGES": ("require_free_messages", _bool_strict),
    "MAX_SELLER_NFTS": ("max_seller_nfts", lambda v: max(0, int(v))),
    "MAX_SELLER_LEVEL": ("max_seller_level", lambda v: max(0, int(v))),
    "COLD_POLL_INTERVAL": ("cold_poll_interval", _rng(1, 3600)),
    "CATALOG_INTERVAL": ("catalog_interval", _rng(0.3, 3600)),
    "MAX_SAME_COLLECTION_STREAK": ("max_same_collection_streak", lambda v: max(0, int(v))),
    "UNIQUE_SELLERS": ("unique_sellers", _bool_strict),
    "SELLER_REPEAT_HOURS": ("seller_repeat_hours", _rng(0, 87600)),
    "ALLOW_ALL_USERS": ("allow_all_users", _bool_strict),
    "FETCH_FULL_SELLER": ("fetch_full_seller", _bool_strict),
}


def _from_env() -> Settings:
    # секреты можно держать в переменных окружения хостинга, а в .env оставить пустыми:
    # пустое значение в .env не затирает переменную окружения (токен не лежит в репозитории)
    _secrets = {k: os.environ.get(k) for k in ("BOT_TOKEN", "SUB_BOT_TOKEN", "API_HASH", "SESSION_BACKUP_KEY", "OLD_BOT_TOKEN",
                                               "TONCENTER_API_KEY")}
    load_dotenv(ENV_PATH, override=True)
    for k, v in _secrets.items():
        if v and not (os.environ.get(k) or "").strip():
            os.environ[k] = v
    e = os.environ.get
    return Settings(
        api_id=_int(e("API_ID"), 0),
        api_hash=(e("API_HASH") or "").strip().strip('"'),
        bot_token=(e("BOT_TOKEN") or "").strip().strip('"'),
        sub_bot_token=(e("SUB_BOT_TOKEN") or "").strip().strip('"'),
        crypto_bot_api_token=(e("CRYPTO_BOT_API_TOKEN") or "").strip().strip('"'),
        target_chat_id=_int(e("TARGET_CHAT_ID"), 0),
        price_routes=_safe_parse(_price_routes, e("PRICE_ROUTES"), "PRICE_ROUTES"),
        backdrop_routes=_safe_parse(_backdrop_routes, e("BACKDROP_ROUTES"), "BACKDROP_ROUTES"),
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
        max_above_floor=max(0, _int(e("MAX_ABOVE_FLOOR"), 500)),
        premium_over_floor_percent=max(0, _int(e("PREMIUM_OVER_FLOOR_PERCENT"), 333)),
        rescue_above_top=_bool(e("RESCUE_ABOVE_TOP"), False),
        rare_attribute_percent=max(0.1, min(100.0, _float(e("RARE_ATTRIBUTE_PERCENT"), 1.5))),
        priority_floor=max(0, _int(e("PRIORITY_FLOOR"), 2500)),
        priority_poll_interval=max(0.5, _float(e("PRIORITY_POLL_INTERVAL"), 1.5)),
        priority_share=min(0.9, max(0.1, _float(e("PRIORITY_SHARE"), 0.65))),
        max_lot_age=max(0, _int(e("MAX_LOT_AGE"), 180)),
        auto_remove_dead=_bool(e("AUTO_REMOVE_DEAD"), True),
        route_by=("price" if (e("ROUTE_BY") or "").strip().lower() == "price" else "floor"),
        allow_ton_only=_bool(e("ALLOW_TON_ONLY"), False),
        russian_only=_bool(e("RUSSIAN_ONLY"), True),
        unknown_seller_policy=(e("UNKNOWN_SELLER_POLICY") or "block").strip().lower(),
        seller_name_signal=_bool(e("SELLER_NAME_SIGNAL"), False),
        phone_mismatch_blocks=_bool(e("PHONE_MISMATCH_BLOCKS"), True),
        allow_hidden_owner=_bool(e("ALLOW_HIDDEN_OWNER"), False),
        soft_filter_stars=max(0, _int(e("SOFT_FILTER_STARS"), 1500)),
        lookup_bot_username=_lookup_bot_username(e("LOOKUP_BOT_USERNAME")),
        lookup_bots=_bot_list(e("LOOKUP_BOTS")),
        lookup_cmd_template=((e("LOOKUP_CMD_TEMPLATE") or "/human {name}").strip() or "/human {name}"),
        lookup_cmds=_bot_cmds(e("LOOKUP_CMDS")),
        auto_lookup=_bool(e("AUTO_LOOKUP"), True),
        lookup_sessions=_str_list(e("LOOKUP_SESSIONS") or e("LOOKUP_SESSION") or ""),
        lookup_dedicated=_bool(e("LOOKUP_DEDICATED"), True),
        lookup_required=_bool(e("LOOKUP_REQUIRED"), True),
        require_username=_bool(e("REQUIRE_USERNAME"), True),
        gift_page_owner=_bool(e("GIFT_PAGE_OWNER"), False),
        lookup_max_age=max(0, min(86400, _int(e("LOOKUP_MAX_AGE"), 180))),
        start_fresh=_bool(e("START_FRESH"), True),
        lookup_check_onsale=_bool(e("LOOKUP_CHECK_ONSALE"), True),
        lookup_lang_filter=_bool(e("LOOKUP_LANG_FILTER"), False),
        lookup_timeout=min(180.0, max(5.0, _float(e("LOOKUP_TIMEOUT"), 15.0))),
        lookup_interval=min(600.0, max(0.0, _float(e("LOOKUP_INTERVAL"), 1.5))),
        lookup_captcha_wait=min(7200.0, max(30.0, _float(e("LOOKUP_CAPTCHA_WAIT"), 90.0))),
        lookup_skip_over=max(0, min(1000, _int(e("LOOKUP_SKIP_OVER"), 20))),
        lookup_resolve_per_hour=max(1, min(200, _int(e("LOOKUP_RESOLVE_PER_HOUR"), 60))),
        lookup_name_check=_bool(e("LOOKUP_NAME_CHECK"), True),
        lookup_nomatch_check=max(0, min(10, _int(e("LOOKUP_NOMATCH_CHECK"), 1))),
        lookup_max_candidates=max(1, min(30, _int(e("LOOKUP_MAX_CANDIDATES"), 3))),
        lookup_trust_name_match=_bool(e("LOOKUP_TRUST_NAME_MATCH"), True),
        trade_bot_username=(e("TRADE_BOT_USERNAME") or "").strip().lstrip("@"),
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
        sync_mode=("full" if (e("SYNC_MODE") or "").strip().lower() == "full" else "fast"),
        fast_sync_pages=max(1, min(20, _int(e("FAST_SYNC_PAGES"), 1))),
        resync_retry_seconds=max(10.0, _float(e("RESYNC_RETRY_SECONDS"), 300.0)),
        max_same_collection_streak=max(0, _int(e("MAX_SAME_COLLECTION_STREAK"), 1)),
        tracker_interval=max(0.2, _float(e("TRACKER_INTERVAL"), 0.5)),
        page_limit=max(1, min(100, _int(e("PAGE_LIMIT"), 50))),
        live_max_pages=max(1, _int(e("LIVE_MAX_PAGES"), 5)),
        recovery_max_pages=max(1, _int(e("RECOVERY_MAX_PAGES"), 20)),
        catalog_interval=max(0.3, _float(e("CATALOG_INTERVAL"), 0.5)),
        collections=_str_list(e("COLLECTIONS")),
        max_concurrent_sessions=max(1, _int(e("MAX_CONCURRENT_SESSIONS"), 20)),
        session_rps=max(0.05, _float(e("SESSION_RPS"), 1.0)),
        global_rps=max(0.0, _float(e("GLOBAL_RPS"), 100.0)),
        connect_concurrency=max(1, _int(e("CONNECT_CONCURRENCY"), 5)),
        accounts_per_proxy=max(1, _int(e("ACCOUNTS_PER_PROXY"), 3)),
        startup_delay=max(0.0, _float(e("STARTUP_DELAY"), 25.0)),
        processor_workers=max(1, _int(e("PROCESSOR_WORKERS"), 24)),
        fetch_full_seller=_bool(e("FETCH_FULL_SELLER"), True),
        seller_cache_ttl=max(0, _int(e("SELLER_CACHE_TTL"), 3600)),
        send_interval=max(0.0, _float(e("SEND_INTERVAL"), 1.1)),
        test_mode=_bool(e("TEST_MODE"), False),
        test_max_messages=max(0, _int(e("TEST_MAX_MESSAGES"), 5)),
        test_chat_id=_int(e("TEST_CHAT_ID"), 0),
        ton_label=(e("TON_LABEL") or "TON").strip(),
        timezone=(e("TIMEZONE") or "Europe/Moscow").strip(),
        session_backup_key=(e("SESSION_BACKUP_KEY") or "").strip(),
        old_bot_token=(e("OLD_BOT_TOKEN") or "").strip().strip('"'),
        sessions_backup_chat_id=_int(e("SESSIONS_BACKUP_CHAT_ID"), 0),
        database_path=(e("DATABASE_PATH") or "data/tracker.db").strip(),
        sessions_dir=(e("SESSIONS_DIR") or "telegram_sessions").strip(),
        template_path=(e("TEMPLATE_PATH") or "templates/listing.html").strip(),
        log_level=(e("LOG_LEVEL") or "INFO").strip().upper(),
    )


def _load_runtime_overrides() -> dict[str, str]:
    try:
        data = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_settings() -> Settings:
    s = _from_env()
    for key, raw in _load_runtime_overrides().items():
        if key in RUNTIME_KEYS:
            attr, conv = RUNTIME_KEYS[key]
            try:
                v = conv(str(raw))
                if isinstance(v, float) and not math.isfinite(v):
                    continue
                setattr(s, attr, v)
            except (ValueError, TypeError):
                pass
    return s


def export_runtime_overrides() -> dict[str, str]:
    """Настройки, изменённые командой /set, — сохраняются в копию в Telegram и переживают деплой."""
    return _load_runtime_overrides()


def drop_runtime_overrides(keys) -> list[str]:
    """Убрать /set-значения этих ключей (тогда действуют значения из .env)."""
    current = _load_runtime_overrides()
    gone = [k for k in keys if k in current]
    if gone:
        for k in gone:
            current.pop(k, None)
        RUNTIME_SETTINGS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        reload_settings()
    return gone


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
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("not a finite number")     # inf/nan «вешали» циклы трекера
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
