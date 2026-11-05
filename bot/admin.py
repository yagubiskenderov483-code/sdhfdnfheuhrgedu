"""
Админ-бот (Bot API, aiogram 3).

Команды: /start /status /sessions /add_session /remove_session /reload /stats /test /settings /set /cancel
Доступ — только для ADMIN_IDS (из .env) и тех, кто подтвердил владение через /claim <код>
(одноразовый код печатается в консоль при запуске, если ADMIN_IDS пуст).

Безопасность /add_session:
  • сообщения с API HASH, кодом и 2FA-паролем удаляются из чата сразу после чтения;
  • секреты не пишутся в лог и не хранятся в FSM дольше, чем нужно;
  • session-файлы никогда не отправляются ботом.
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart, Filter, or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, LinkPreviewOptions, Message,
                           ReplyKeyboardMarkup)
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from config import DATA_DIR, RUNTIME_KEYS, reload_settings, set_runtime_value, settings
from sessions.backup import string_to_session_file
from sessions.storage import SessionRecord
from utils.proxy import mask_proxy, normalize_proxy, parse_proxy, split_lines
from utils.logger import get_logger

if TYPE_CHECKING:
    from main import App

log = get_logger("admin")
ADMINS_FILE = DATA_DIR / "admins.json"


# ---------------------------------------------------------------------------- admins
def _load_claimed() -> set[int]:
    try:
        return {int(x) for x in json.loads(ADMINS_FILE.read_text(encoding="utf-8"))}
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return set()


def all_admins() -> set[int]:
    return set(settings.admin_ids) | _load_claimed()


def save_admins(ids: set[int]) -> None:
    """Админы, назначенные ботом (первый добавивший аккаунт, /claim, восстановленные из копии)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ADMINS_FILE.write_text(json.dumps(sorted(_load_claimed() | set(ids))), encoding="utf-8")


def can_manage(user_id: int, owner_id: int | None) -> bool:
    # ADMIN_IDS задан — только они (старые админы из admins.json не в счёт); иначе — админы; свой аккаунт — всегда
    return is_manager(user_id) and (bool(settings.admin_ids) or user_id in all_admins()) \
        or (owner_id is not None and owner_id == user_id)


_owner_ids_getter = lambda: set()   # noqa: E731 — задаётся в build_routers (владельцы добавленных аккаунтов)


def is_manager(uid: int | None) -> bool:
    """Управлять трекером (/pause /resume /set /reload) может админ или тот, кто добавил аккаунт.
    Бутстрап первого админа — только явной командой /claim <код> (код печатается в логах при старте),
    а НЕ автоматически первому написавшему: иначе любой чужой первым сообщением стал бы админом."""
    if not uid:
        return False
    if settings.admin_ids:                 # ADMIN_IDS задан — управляют ТОЛЬКО они
        return uid in settings.admin_ids
    return uid in all_admins() or uid in _owner_ids_getter()


class IsAdmin(Filter):
    async def __call__(self, message: Message) -> bool:
        return is_manager(message.from_user.id if message.from_user else None)


# Кнопки под полем ввода
BTN_STATUS, BTN_PAUSE, BTN_RESUME = "📊 Статус", "⏸ Пауза", "▶️ Продолжить"
BTN_SESSIONS, BTN_ADD, BTN_STATS = "👥 Аккаунты", "➕ Добавить аккаунт", "📈 Статистика"
BTN_LAST = "🕘 Последние лоты"
BTN_COLLECTIONS = "📚 Коллекции"
MAIN_KB = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_STATS)],
    [KeyboardButton(text=BTN_PAUSE), KeyboardButton(text=BTN_RESUME)],
    [KeyboardButton(text=BTN_SESSIONS), KeyboardButton(text=BTN_ADD)],
    [KeyboardButton(text=BTN_LAST), KeyboardButton(text=BTN_COLLECTIONS)],
], resize_keyboard=True, is_persistent=True)


def is_allowed(user_id: int | None) -> bool:
    if settings.admin_ids:                 # ADMIN_IDS задан — бот-панель только у них, остальным подписка
        return bool(user_id) and user_id in settings.admin_ids
    return bool(user_id) and (settings.allow_all_users or user_id in all_admins())


class IsAllowed(Filter):
    """ALLOW_ALL_USERS=true — бот доступен всем, иначе только админам."""
    async def __call__(self, message: Message) -> bool:
        return is_allowed(message.from_user.id if message.from_user else None)


CLAIM_CODE: str | None = None


def init_claim_code() -> None:
    """Если админов нет — генерируем одноразовый код и печатаем в консоль (не в файлы логов)."""
    global CLAIM_CODE
    if not all_admins():
        CLAIM_CODE = secrets.token_hex(4)
        print("\n" + "=" * 60)
        print(f"  ADMIN_IDS не задан. Отправьте боту:  /claim {CLAIM_CODE}")
        print("=" * 60 + "\n", flush=True)


# ---------------------------------------------------------------------------- helpers
class AddSession(StatesGroup):
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


class SetLookupBot(StatesGroup):
    waiting = State()


class SetTradeBot(StatesGroup):
    waiting = State()


_REJ = {"PAID_MESSAGES": "платные сообщения / только Premium", "TOO_MANY_NFTS": "много NFT",
        "LEVEL_TOO_HIGH": "Level выше лимита", "UNKNOWN_BLOCKED": "нет признаков русского",
        "PHONE_MISMATCH": "номер не +7/+375/+380", "FOREIGN_TEXT": "профиль на чужом языке",
        "LOOKUP_FAILED": "профиль не прочитался (аккаунты заняты)", "BLACKLIST": "blacklist",
        "OWNER_NOT_FOUND": "скрытый владелец не найден ботом поиска",
        "OWNER_NOT_FOUND_SENT": "скрытый владелец (бот поиска недоступен → отправлен с пометкой)",
        "OWNER_TRUSTED": "скрытый владелец (доверяем боту поиска по имени)",
        "NO_USERNAME": "у продавца нет @username",
        "FOREIGN_NAME_HIDDEN": "имя продавца на чужом языке (профиль скрыт Telegram)",
        "SHORT_NAME_HIDDEN": "имя продавца короче 5 букв (профиль скрыт)",
        "FOREIGN_LETTER_HIDDEN": "иностранные буквы (ö ü ğ ə ç ş ı) в имени (профиль скрыт)"}


def _human_reason(status: str, reason: str, code: str | None) -> str:
    if status == "FILTERED_SELLER" and code:
        return _REJ.get(code, code)
    r = reason or ""
    if r.startswith("stale"):
        return "устарел, пока ждал очереди"
    if "seller was already sent" in r:
        return "этот продавец уже был"
    if "gift was already sent" in r:
        return "этот NFT уже был"
    if "in a row" in r:
        return "та же коллекция подряд"
    if "TON-only" in r:
        return "продаётся только за TON"
    if "Stars <" in r:
        return "дешевле порога"
    return r


_ST = {"SENT": "✅ отправлено", "FILTERED_SELLER": "👤 отсеяно по продавцу", "FILTERED_PRICE": "💲 отсеяно по цене",
       "FILTERED_DUPLICATE": "🔁 повтор", "ERROR": "⌛ устарел / ошибка", "SEND_FAILED": "⚠️ не отправилось",
       "NEW": "⏳ в обработке", "SENDING": "📤 отправлялся во время перезапуска", "TEST_ONLY": "🧪 тест"}


def _proxies_on() -> bool:
    import os
    return os.getenv("USE_PROXIES", "0").strip().lower() in ("1", "true", "yes", "on")


def _ago(ts: float | None) -> str:
    if not ts:
        return "—"
    d = int(time.time() - ts)
    if d < 60:
        return f"{d} с назад"
    if d < 3600:
        return f"{d // 60} мин назад"
    return f"{d // 3600} ч назад"


async def _delete(m: Message) -> None:
    try:
        await m.delete()
    except Exception:
        pass


HELP = (
    "<b>🎁 Gift Tracker · панель админа</b>\n\n"
    "<b>Состояние</b>\n"
    "/status — трекер, скорость, задержки\n"
    "/why — почему нет постов (что нашлось и на чём отсеялось)\n"
    "/panel — фильтры кнопками (включить/выключить в один клик)\n"
    "/lot ссылка — почему конкретный лот (t.me/nft/…) не пришёл\n"
    "/stats — статистика лотов · /last — последние лоты и почему не ушли\n"
    "/top — самые активные коллекции за сутки · /sellers [часов] — топ продавцов\n"
    "/find имя — всё о коллекции · /lot ссылка — почему конкретный лот не пришёл\n"
    "/mute имя · /unmute имя — выключить / включить лоты коллекции\n"
    "/collections — какие коллекции отслеживаются\n\n"
    "<b>Управление</b>\n"
    "/off · /on — выключить / включить парсер\n"
    "/close · /open — закрыть / открыть для пользователей (лоты идут)\n"
    "/settings — все настройки · /set КЛЮЧ ЗНАЧЕНИЕ — изменить\n\n"
    "<b>Аккаунты</b>\n"
    "/sessions — список, пинг, риск · /add_session — добавить\n"
    "/flood — диагностика FloodWait (кто ловит, speed, когда последний)\n"
    "/reset_speeds — сброс замедления аккаунтов (после шторма)\n"
    "/removed — удалённые за 14 дней · /revive ИМЯ|all — вернуть\n"
    "/proxies — прокси для аккаунтов\n\n"
    "<b>Маршруты и фильтры</b>\n"
    "/routes — PRICE_ROUTES и BACKDROP_ROUTES в удобной форме\n"
    "/captcha_status — работает ли OCR, сколько капч решено (авто/рандом)\n"
    "/clear_captcha_cache — сброс кеша решённых капч\n\n"
    "<b>Lookup-боты (funstat / infostat / fallback)</b>\n"
    "/bots — статус пула (основной, запасные, dead, команды) · /help_lookup — полный гайд\n"
    "/bots_set_primary @bot — сменить основной (старый → в запасные)\n"
    "/bots_add @bot · /bots_remove @bot — управление запасными\n"
    "/bots_clear_dead [@bot] — снять dead-mark (всех или одного)\n"
    "/test_bot [@bot] [имя] — реальный тест с ответом бота\n"
    "/test_bots — пинг всех настроенных ботов (бесплатно)\n"
    "/lookup_last — последние 20 lookup-операций с результатом\n"
    "/lookup_cache — состояние кешей, inflight, dead-marks\n"
    "/lookup_memory — обзор памяти найденных владельцев в БД\n"
    "/lookup_debug &lt;имя&gt; — пошагово что будет с таким именем\n"
    "/lookup_forget &lt;имя&gt; — удалить ложного владельца из памяти\n"
    "/lookup_reset — полный сброс всех lookup-кешей\n"
    "/sessions_stats — histogram скорости аккаунтов + топ замедленных\n"
    "/sessions_speedup — мягкий разгон (безопаснее /reset_speeds)\n"
    "/health — статус HTTP-endpoint'а (HEALTH_PORT) /health /stats /metrics\n\n"
    "<b>Подписки</b>\n"
    "/subs — подписчики и выручка\n"
    "/grant ID|@юз 24|3д|навсегда — выдать · /revoke ID|@юз — забрать\n\n"
    "/test — проверка работы · /reload — перечитать .env и файлы · /cancel — отменить диалог"
)


USER_HELP = (
    "<b>Gift Marketplace Live Tracker</b>\n\n"
    "/status — состояние трекера\n"
    "/sessions — состояние sessions\n"
    "/add_session — добавить свой Telegram-аккаунт в трекер\n"
    "/remove_session — удалить свой аккаунт\n"
    "/stats — статистика листингов\n"
    "/last — последние найденные лоты и почему не отправлены\n"
    "/collections — какие коллекции отслеживаются\n"
    "/pause, /resume — пауза (если вы добавили аккаунт)\n"
    "/settings или /filters — все настраиваемые фильтры со значениями\n"
    "/test — проверка работы (пример сообщения придёт вам)\n"
    "/cancel — отменить текущий диалог"
)


# Описания настраиваемых ключей и группировка для /set и /filters.
SETTING_DESC: dict[str, str] = {
    "MIN_STARS": "мин. цена лота в ⭐ (если COLLECTION_MIN_FLOOR=0)",
    "COLLECTION_MIN_FLOOR": "единый порог: от какой цены читать коллекции И слать лоты (⭐)",
    "COLLECTION_FLOOR_MODE": "что делать с коллекциями ниже порога: skip (не читать) | slow (реже)",
    "MIN_TON": "мин. цена лота в TON (0 = не проверять)",
    "PRIORITY_FLOOR": "коллекции с флором от N ⭐ опрашиваются чаще и первыми (0 = выкл)",
    "PRIORITY_SHARE": "какая доля аккаунтов работает на дорогих коллекциях (0.1–0.9)",
    "PRIORITY_POLL_INTERVAL": "как часто опрашивать каждую дорогую коллекцию, сек",
    "SYNC_MODE": "fast — коллекция начинает слать лоты сразу (база = верхние 100 лотов) | full — сперва читает ВСЕ лоты (медленно)",
    "ROUTE_BY": "в какую тему класть лот: floor (по флору коллекции = цене гифта) | price (по цене лота)",
    "MAX_ABOVE_FLOOR": "не слать лоты дороже флора коллекции больше чем на N% (0 = не проверять)",
    "ALLOW_TON_ONLY": "слать лоты, которые продаются только за TON (true/false)",
    "RUSSIAN_ONLY": "только русские продавцы (true/false)",
    "UNKNOWN_SELLER_POLICY": "продавец без явных данных: block (отсеять) | allow (пропускать)",
    "ALLOW_HIDDEN_OWNER": "true — пропускать лоты со скрытым Telegram-ом владельцем (без языкового фильтра)",
    "LOOKUP_BOT_USERNAME": "@username бота поиска по имени (напр. funstat); пусто = кнопки нет",
    "AUTO_LOOKUP": "true — трекер сам шлёт боту поиска /human «имя» и дописывает в пост владельца, у которого открыт этот NFT",
    "LOOKUP_SESSIONS": "аккаунты, которые пишут боту поиска, через запятую (напр. session_005,session_006); - = первый рабочий",
    "START_FRESH": "true — после запуска/деплоя слать только новые лоты (лоты, выставленные пока бот был выключен, не слать)",
    "REQUIRE_USERNAME": "true — слать только продавцов с @username (без юза — не слать)",
    "GIFT_PAGE_OWNER": "true — искать скрытого владельца ещё и через страницу подарка (+1 запрос на лот, раскрывает ~1%)",
    "LOOKUP_MAX_AGE": "лот найден больше N сек назад, пока искали владельца — не слать (0 = без лимита)",
    "LOOKUP_CHECK_ONSALE": "true — после поиска владельца проверить, что лот ещё продаётся (проданные не слать)",
    "LOOKUP_LANG_FILTER": "true — найденного ботом поиска владельца проверять и по языку профиля (меньше лотов)",
    "LOOKUP_REQUIRED": "true — лот со скрытым владельцем уходит только если бот поиска нашёл его @юз",
    "LOOKUP_DEDICATED": "true — аккаунты поиска не опрашивают маркет, только пишут боту поиска",
    "LOOKUP_TIMEOUT": "сколько секунд ждать ответ бота поиска",
    "LOOKUP_INTERVAL": "пауза между запросами к боту поиска с одного аккаунта, сек (меньше капч)",
    "LOOKUP_CAPTCHA_WAIT": "сколько ждать, пока вы пройдёте капчу бота поиска, сек",
    "LOOKUP_SKIP_OVER": "в ответе бота поиска N и больше человек — лот сразу пропускается (0 = проверять всё равно)",
    "LOOKUP_RESOLVE_PER_HOUR": "сколько новых @юзов в час может открыть один аккаунт поиска (больше — FloodWait на сутки)",
    "LOOKUP_NAME_CHECK": "true — найденный аккаунт должен называться так же, как владелец NFT (защита от чужих юзов)",
    "LOOKUP_NOMATCH_CHECK": "имя в ответе бота не совпало ни у кого — сколько первых всё равно проверить (0 = никого)",
    "LOOKUP_MAX_CANDIDATES": "сколько кандидатов из ответа бота поиска проверять (1–30)",
    "TRADE_BOT_USERNAME": "@username торгового бота (напр. iostherobot); кнопка «Написать» → оффер со скидкой",
    "SELLER_NAME_SIGNAL": "true — имя кириллицей («Дмитрий») считается русскоязычным продавцом (больше лотов)",
    "PHONE_MISMATCH_BLOCKS": "false — чужой телефон не отсеивает сразу, решает язык профиля (больше лотов)",
    "REQUIRE_FREE_MESSAGES": "продавцу можно написать бесплатно (true/false)",
    "MAX_SELLER_LEVEL": "макс. Level продавца (0 = не проверять)",
    "MAX_SELLER_NFTS": "макс. число NFT у продавца (0 = не проверять)",
    "SELLER_PHONE_PREFIXES": "какие префиксы телефона считать русскими (напр. 7)",
    "UNIQUE_GIFTS": "не слать один и тот же NFT дважды (true/false)",
    "UNIQUE_SELLERS": "не слать одного продавца дважды (true/false)",
    "MAX_SAME_COLLECTION_STREAK": "сколько лотов одной коллекции подряд можно (1 = не подряд)",
    "SELLER_REPEAT_HOURS": "через сколько часов можно повторить продавца (0 = никогда)",
    "CATALOG_INTERVAL": "как часто проверять счётчики всех коллекций, сек (быстрое обнаружение)",
    "COLD_POLL_INTERVAL": "страховочный обход коллекции, сек (реже = меньше FloodWait)",
    "TRACKER_INTERVAL": "пауза между чтениями горячей коллекции, сек",
    "PAGE_LIMIT": "сколько лотов читать за раз (1–100)",
    "SESSION_RPS": "запросов в секунду на ОДИН аккаунт (выше = быстрее, но риск FloodWait)",
    "GLOBAL_RPS": "потолок запросов/сек на все аккаунты вместе",
    "MAX_CONCURRENT_SESSIONS": "сколько аккаунтов работают параллельно",
    "LOW_PRIORITY_POLL_INTERVAL": "обход дешёвых коллекций в режиме slow, сек",
    "TEST_MODE": "тестовый режим: не слать в канал (true/false)",
    "ALLOW_ALL_USERS": "разрешить командовать ботом всем (true/false)",
    "FETCH_FULL_SELLER": "запрашивать профиль продавца для фильтров (true/false)",
    "TARGET_CHAT_ID": "общий канал/группа (куда идёт лот, не попавший ни в один маршрут)",
    "PRICE_ROUTES": "каналы/темы по цене: lo-hi:ID или ID/тема через запятую (пусто = один канал)",
    "BACKDROP_ROUTES": "каналы/темы по фону: Название:ID или ID/тема (напр. Black:-100X/67); в приоритете над ценой",
}
SETTING_GROUPS: list[tuple[str, list[str]]] = [
    ("🎯 Фильтр цены", ["COLLECTION_MIN_FLOOR", "MAX_ABOVE_FLOOR", "MIN_STARS", "MIN_TON", "ALLOW_TON_ONLY"]),
    ("👤 Фильтр продавца", ["REQUIRE_USERNAME", "RUSSIAN_ONLY", "UNKNOWN_SELLER_POLICY", "ALLOW_HIDDEN_OWNER",
                            "REQUIRE_FREE_MESSAGES", "MAX_SELLER_LEVEL", "MAX_SELLER_NFTS", "SELLER_PHONE_PREFIXES"]),
    ("🔁 Без повторов", ["UNIQUE_GIFTS", "UNIQUE_SELLERS", "MAX_SAME_COLLECTION_STREAK",
                         "SELLER_REPEAT_HOURS"]),
    ("⚡ Скорость", ["PRIORITY_FLOOR", "PRIORITY_SHARE", "PRIORITY_POLL_INTERVAL", "CATALOG_INTERVAL", "COLD_POLL_INTERVAL", "TRACKER_INTERVAL", "PAGE_LIMIT",
                    "SESSION_RPS", "GLOBAL_RPS", "MAX_CONCURRENT_SESSIONS", "LOW_PRIORITY_POLL_INTERVAL", "SYNC_MODE"]),
    ("🔗 Внешние боты", ["TRADE_BOT_USERNAME", "LOOKUP_BOT_USERNAME", "AUTO_LOOKUP", "LOOKUP_REQUIRED", "LOOKUP_CHECK_ONSALE", "LOOKUP_MAX_AGE", "GIFT_PAGE_OWNER", "LOOKUP_LANG_FILTER", "LOOKUP_SESSIONS",
                          "LOOKUP_DEDICATED", "LOOKUP_TIMEOUT", "LOOKUP_INTERVAL", "LOOKUP_CAPTCHA_WAIT", "LOOKUP_SKIP_OVER", "LOOKUP_RESOLVE_PER_HOUR", "LOOKUP_NAME_CHECK", "LOOKUP_NOMATCH_CHECK", "LOOKUP_MAX_CANDIDATES"]),
    ("⚙️ Прочее", ["START_FRESH", "COLLECTION_FLOOR_MODE", "TEST_MODE", "ALLOW_ALL_USERS", "FETCH_FULL_SELLER"]),
    ("📢 Общий и каналы по цене", ["TARGET_CHAT_ID", "PRICE_ROUTES", "ROUTE_BY"]),
    ("🎨 Каналы по фону", ["BACKDROP_ROUTES"]),
]


async def _answer_long(m: Message, text: str, reply_markup=None) -> None:
    """Telegram не принимает сообщения длиннее 4096 символов — режем по строкам."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > 3800:                    # одна очень длинная строка
            if cur.strip():
                chunks.append(cur)
            chunks.append(line[:3800])
            line, cur = line[3800:], ""
        if len(cur) + len(line) + 1 > 3800 and cur.strip():
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for i, chunk in enumerate(chunks):
        await m.answer(chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)


async def _edit_or_send(q: CallbackQuery, text: str) -> None:
    """Сообщение с кнопками могло устареть (48+ ч) — тогда отвечаем новым сообщением."""
    try:
        await q.message.edit_text(text)
    except Exception:
        try:
            await q.bot.send_message(q.from_user.id, text)
        except Exception:
            pass


def _filters_help() -> str:
    lines = ["<b>⚙️ Настройки трекера</b>",
             "Изменить: <code>/set КЛЮЧ ЗНАЧЕНИЕ</code>  (напр. <code>/set MAX_SELLER_LEVEL 10</code>)",
             "Сохраняется и переживает деплой.", ""]
    for title, keys in SETTING_GROUPS:
        lines.append(f"<b>{title}</b>")
        for k in keys:
            attr = RUNTIME_KEYS[k][0]
            val = getattr(settings, attr, "?")
            if k == "PRICE_ROUTES":
                val = (", ".join(f"{lo}-{hi if hi is not None else ''}→{chat}{('/'+str(th)) if th else ''}"
                                 for lo, hi, chat, th in val) or "не задано (один канал)")
            elif k == "BACKDROP_ROUTES":
                val = (", ".join(f"{name}→{chat}{('/'+str(th)) if th else ''}"
                                 for name, chat, th in val) or "не задано")
            raw = str(val)
            cur = html.escape(raw[:500] + ("…" if len(raw) > 500 else ""))
            desc = SETTING_DESC.get(k, "")
            lines.append(f"<code>{k}</code> = <b>{cur}</b>\n  <i>{desc}</i>")
        lines.append("")
    return "\n".join(lines).strip()


def _taken_text(button_text: str) -> str:
    who = button_text.split("·", 1)[1].strip() if "·" in button_text else ""
    who = who or button_text.replace("✅ Взял", "").replace("✅ Занял", "").strip()
    return "Данный лот занят" + (f"\n{who}" if who else "")


# ---------------------------------------------------------------------------- routers
def build_routers(app: "App") -> list[Router]:
    public = Router(name="public")
    admin = Router(name="admin")        # только админы: /remove_session /reload /set
    admin.message.filter(IsAdmin())
    users = Router(name="users")        # все пользователи, если ALLOW_ALL_USERS=true
    users.message.filter(IsAllowed())
    users.callback_query.filter(F.from_user.func(lambda u: is_allowed(u.id)))   # кнопки — тоже только своим

    _login_tasks: set[asyncio.Task] = set()
    pending: dict[int, dict[str, Any]] = {}   # user_id → {client, name, phone, hash, api_id, api_hash}
    claimed: set[tuple[int, int]] = set()     # (chat_id, message_id) уже занятых лотов — атомарность «Занять»
    claim_lock = asyncio.Lock()
    global _owner_ids_getter
    _owner_ids_getter = lambda: {r.owner_id for r in app.storage.list_sessions() if r.owner_id}  # noqa: E731

    # ------------------------------------------------------------ public
    @public.message(CommandStart())
    async def cmd_start(m: Message) -> None:
        # первый /start от любого НЕ-админа → разовое уведомление всем админам
        # защита от флуда: не больше 20 индивидуальных уведомлений в час; дальше тихо копим счётчик
        try:
            u = m.from_user
            uid = u.id if u else None
            if uid and uid not in all_admins():
                kv_key = f"start_seen:{uid}"
                if await app.db.kv_get(kv_key) is None:
                    await app.db.kv_set(kv_key, "1")
                    now = time.time()
                    bucket = getattr(app, "_start_notify_bucket", None)
                    if bucket is None:
                        bucket = app._start_notify_bucket = {"ts": [], "skipped": 0, "reported_at": 0.0}
                    # окно — последний час
                    bucket["ts"] = [t for t in bucket["ts"] if now - t < 3600]
                    if len(bucket["ts"]) < 20:
                        bucket["ts"].append(now)
                        name = html.escape((u.full_name or "").strip()) or "—"
                        uname = f"@{html.escape(u.username)}" if u.username else "—"
                        text = ("🆕 <b>Новый юзер запустил бота</b>\n"
                                f"ID: <code>{uid}</code>\n"
                                f"Имя: {name}\n"
                                f"Юзер: {uname}\n"
                                f'<a href="tg://user?id={uid}">профиль</a>')
                        for admin in all_admins():
                            try:
                                await app.bot.send_message(admin, text)
                            except Exception as e:
                                log.warning("notify admin %s about new /start user %s failed: %s", admin, uid, e)
                    else:
                        bucket["skipped"] += 1
                        # сводка — не чаще раза в 10 минут, чтобы не спамить
                        if now - bucket["reported_at"] > 600:
                            bucket["reported_at"] = now
                            n = bucket["skipped"]
                            bucket["skipped"] = 0
                            summary = f"🆕 Ещё {n} новых юзеров запустили бота за последний час (поток слишком плотный, индивидуальные уведомления остановлены)"
                            for admin in all_admins():
                                try:
                                    await app.bot.send_message(admin, summary)
                                except Exception as e:
                                    log.warning("notify admin %s with start-summary failed: %s", admin, e)
        except Exception as e:
            log.warning("new-user notification failed: %s", e)

        if m.from_user and is_allowed(m.from_user.id):
            await m.answer((HELP if is_manager(m.from_user.id) else USER_HELP)
                           + f"\n\nВаш Telegram ID: <code>{m.from_user.id}</code>", reply_markup=MAIN_KB)
            if not app.sessions.usable():
                await m.answer("⚠️ Нет ни одного авторизованного аккаунта — трекер не работает.\n"
                               "Нажмите /add_session: нужен только номер телефона и код.")
        elif getattr(app, "sub_welcome", None):
            await app.sub_welcome(m)           # обычные пользователи — приветствие и подписка
        else:
            await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id if m.from_user else '?'}</code>\n"
                           "Доступ только для администраторов.")

    @public.callback_query(F.data.startswith("take:"))
    async def cb_take(q: CallbackQuery) -> None:
        """Кнопка «Занять» под постом: первый нажавший забирает лот, лот приходит ему в личку."""
        msg = q.message
        kb = getattr(msg, "reply_markup", None)
        if not kb or not kb.inline_keyboard:
            await q.answer()
            return
        first = kb.inline_keyboard[0][0]
        if first.callback_data != q.data:          # старый пост, уже занят
            await q.answer("Данный лот занят", show_alert=True)
            return
        # атомарно: первый нажавший занимает лот, второй получает отказ
        key = (msg.chat.id, msg.message_id)
        async with claim_lock:
            if key in claimed:
                await q.answer("Данный лот занят", show_alert=True)
                return
            if len(claimed) > 5000:
                claimed.clear()   # старые лоты и так защищены кнопкой «Занят»
            claimed.add(key)
        u = q.from_user
        who = f"@{u.username}" if u.username else (u.full_name or str(u.id))
        original = msg.html_text or ""
        links = kb.inline_keyboard[1:]
        rows = [[InlineKeyboardButton(text="Отменить", callback_data=f"untake:{u.id}")]]

        async def release() -> None:
            async with claim_lock:
                claimed.discard(key)

        # 1) сначала лот — в личку тому, кто занял. Не дошло (человек не запускал бота) → лот не трогаем,
        #    иначе он пропал бы и из канала, и у человека
        try:
            dm = await app.bot.send_message(
                u.id, "Вы заняли лот:\n\n" + original,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=links) if links else None)
        except Exception:
            await release()
            try:
                me = await app.bot.me()
                name = f"@{me.username}" if me.username else "бота"
            except Exception:
                name = "бота"
            await q.answer(f"Сначала откройте {name} и нажмите «Старт», затем «Занять»", show_alert=True)
            return
        # 2) потом пост в канале: остаётся только отметка «занят»
        edited = False
        try:
            await msg.edit_text(f"<i>Данный лот занят</i>\n{html.escape(who)}",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                link_preview_options=LinkPreviewOptions(is_disabled=True))
            edited = True
        except Exception:
            try:
                await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                edited = True
            except Exception:
                pass
        if not edited:
            await release()
            try:
                await dm.delete()
            except Exception:
                pass
            await q.answer("Не получилось занять, нажмите ещё раз", show_alert=True)
            return
        # запоминаем пост, чтобы можно было отменить и вернуть лот в канал
        try:
            await app.db.kv_set(f"taken:{key[0]}:{key[1]}", json.dumps(
                {"uid": u.id, "html": original, "kb": kb.model_dump(mode="json", exclude_none=True),
                 "ts": int(time.time())}))
        except Exception as e:
            log.warning("Could not save taken lot: %s", e)
        await q.answer("Лот ваш, отправил его вам в личку")

    @public.callback_query(F.data == "taken")
    async def cb_taken(q: CallbackQuery) -> None:
        await q.answer("Данный лот занят", show_alert=True)

    @public.callback_query(F.data.startswith("untake:"))
    async def cb_untake(q: CallbackQuery) -> None:
        """«Отменить»: тот, кто занял лот, возвращает его в канал. Остальным — «Данный лот занят»."""
        msg = q.message
        if not isinstance(msg, Message):         # сообщение слишком старое — Telegram его не отдаёт
            await q.answer()
            return
        try:
            owner = int(q.data.split(":", 1)[1] or 0)
        except ValueError:
            await q.answer()
            return
        if q.from_user.id != owner and not is_manager(q.from_user.id):
            await q.answer("Данный лот занят", show_alert=True)
            return
        kv_key = f"taken:{msg.chat.id}:{msg.message_id}"
        raw = await app.db.kv_get(kv_key)
        if not raw:
            # бот перезапускался — исходного текста поста нет; убираем мёртвую кнопку
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await q.answer("Вернуть нельзя: бот перезапускался после того, как лот заняли", show_alert=True)
            return
        data = json.loads(raw)
        # превью гифта как в исходном посте (ссылка t.me/nft/... из кнопок)
        nft_url = next((b.get("url") for row in data["kb"].get("inline_keyboard", []) for b in row
                        if str(b.get("url") or "").startswith("https://t.me/nft/")), None)
        preview = (LinkPreviewOptions(url=nft_url, prefer_small_media=True) if nft_url
                   else LinkPreviewOptions(is_disabled=True))
        try:
            await msg.edit_text(data["html"], reply_markup=InlineKeyboardMarkup.model_validate(data["kb"]),
                                link_preview_options=preview)
        except Exception as e:
            log.warning("Untake failed: %s", e)
            await q.answer("Не получилось вернуть лот", show_alert=True)
            return
        await app.db.kv_set(kv_key, None)
        async with claim_lock:
            claimed.discard((msg.chat.id, msg.message_id))
        await q.answer("Вы отменили, лот снова свободен")

    @public.message(Command("claim"))
    async def cmd_claim(m: Message, command: CommandObject) -> None:
        global CLAIM_CODE
        await _delete(m)
        if not CLAIM_CODE or not command.args or command.args.strip() != CLAIM_CODE or not m.from_user:
            return
        claimed = _load_claimed() | {m.from_user.id}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ADMINS_FILE.write_text(json.dumps(sorted(claimed)), encoding="utf-8")
        CLAIM_CODE = None
        log.info("Admin claimed: %s", m.from_user.id)
        await m.answer("✅ Вы администратор.\n\n" + HELP)

    # ------------------------------------------------------------ admin: general
    @users.message(Command("cancel"))
    async def cmd_cancel(m: Message, state: FSMContext) -> None:
        await _cleanup_pending(m.from_user.id)
        await state.clear()
        await m.answer("Отменено.")

    async def _cleanup_pending(uid: int) -> None:
        p = pending.pop(uid, None)
        if p:
            try:
                await p["client"].disconnect()
            except Exception:
                pass

    @users.message(or_f(Command("pause", "resume", "on", "off", "close", "open", "reload", "set", "proxies", "proxy", "removed", "revive", "top", "find", "mute", "unmute", "why", "lot", "panel", "clear_captcha_cache", "flood", "routes", "reset_speeds", "sessions_speedup", "sessions_stats", "captcha_status", "sellers", "bots", "test_bot", "test_bots", "lookup_cache", "lookup_last", "bots_clear_dead", "bots_set_primary", "bots_add", "bots_remove", "lookup_reset", "lookup_debug", "lookup_forget", "lookup_memory", "help_lookup", "health", "disk_usage", "dashboard"),
                        F.text.in_({BTN_PAUSE, BTN_RESUME})))
    async def cmd_denied(m: Message) -> None:
        await m.answer("⛔ Эта команда доступна админу или тому, кто добавил аккаунт в трекер.\n"
                       f"Ваш Telegram ID: <code>{m.from_user.id}</code> — его можно вписать в ADMIN_IDS.")

    def _status_text() -> str:
        c = app.sessions.counts()
        coord, pipe = app.coordinator, app.pipeline
        running = coord.mode == "LIVE" and not coord.paused
        if coord.paused:
            head = "⏸ <b>Парсер выключен</b> · включить: /on"
        elif running:
            head = "🟢 <b>Трекер работает</b>"
        else:
            head = f"🟡 <b>Запуск: {coord.mode}</b>"
        if getattr(app, "users_closed", False):
            head += "\n🔒 Для пользователей закрыто (/open)"
        cycle = coord.avg_cycle_seconds()
        live, total = coord.sync_progress()
        eff = app.sessions.effective()
        slow = sum(1 for w in app.sessions.workers.values() if w.speed < 1.0)
        lat = pipe.latency
        sec = lambda v: f"{v:.1f} с" if v is not None else "—"     # noqa: E731
        lines = [
            _boot_note() + head, "",
            f"<b>👥 Аккаунты: {len(app.sessions.workers)}</b>",
            f"🟢 {c.get('ACTIVE', 0)} · ⏳ FloodWait {c.get('FLOOD_WAIT', 0)} · "
            f"🔴 {c.get('ERROR', 0) + c.get('AUTH_ERROR', 0)} · 🟡 {c.get('CONNECTING', 0)}",
            f"Мощность: {eff:.1f} из {len(app.sessions.workers)}" + (f" (замедлено {slow})" if slow else ""),
            "",
            "<b>📚 Рынок</b>",
            f"Коллекций в работе: {live}/{total}" + (f" · синхронизируются: {total - live}" if total > live else ""),
            (f"Дешёвых (реже): {len(coord.low_prio)}" if settings.collection_floor_mode == "slow"
             else f"Пропущено по флору (&lt;{settings.collection_min_floor}⭐): {len(coord.skipped)}"),
            f"Реакция на новый лот: {sec(coord.hot_latency)} · обход всех: "
            f"{f'{cycle:.0f} с' if cycle else '—'} (цель {settings.cold_poll_interval:g} с)",
        ]
        b = getattr(app, "bench", None)
        if b:
            lines.append(f"За минуту: {b['req']} запросов · FloodWait {b['flood']} · ошибок {b['err']}"
                         + (f" · пинг {b['ping']:.0f} мс" if b.get("ping") else ""))
        if lat:
            lines += ["", "<b>⏱ Задержка лота</b>",
                      " · ".join(f"{n} {lat[k]:.1f} с" for k, n in
                                 (("request", "запрос"), ("queue", "очередь"), ("check", "проверка"),
                                  ("send", "отправка")) if k in lat)]
            if "total" in lat:
                lines.append(f"<b>Итого: {lat['total']:.1f} с</b> от появления до поста")
            tot = list(pipe.totals)

            def _q(xs: list[float], p: float) -> float:
                xs = sorted(xs)
                return xs[min(len(xs) - 1, int(len(xs) * p))]
            for title, xs in (("дорогие", [t for t, e in tot if e]), ("остальные", [t for t, e in tot if not e])):
                if len(xs) >= 3:
                    lines.append(f"{title}: обычно {_q(xs, .5):.1f} с, 9 из 10 быстрее {_q(xs, .9):.1f} с "
                                 f"(последние {len(xs)})")
        lk_stats = getattr(getattr(app, "lookup", None), "stats", {}) or {}
        pend_cap = app.lookup.pending_captchas() if hasattr(app, "lookup") and app.lookup else 0
        lines += [
            "", "<b>🎁 Лоты</b>",
            f"Найдено: {pipe.stats['new']} · отправлено: {pipe.stats['sent']} · в очереди: {pipe.queue.qsize()}",
            f"Последний найден: {_ago(pipe.last_new_at)} · отправлен: {_ago(pipe.last_sent_at)}",
        ]
        if lk_stats:
            # Основная строка: что lookup нашёл
            lookups_tot = lk_stats.get('lookups', 0)
            lookups_part = f" · попыток {lookups_tot}" if lookups_tot else " · <b>попыток 0</b> ⚠️"
            lines.append(f"🔍 lookup: найдено {lk_stats.get('found', 0)} · не найдено {lk_stats.get('not_found', 0)} "
                         f"· из памяти {lk_stats.get('from_memory', 0)} · в очереди {app.lookup.queue_size()}"
                         f"{lookups_part}")
            # Диагностика пула ботов и сессий
            try:
                bot_pool = app.lookup._bot_pool()
                dead = {b for b in (app.lookup._bot_dead_until or {}) if
                        app.lookup._bot_dead_until[b] > __import__("time").monotonic()}
            except Exception:
                bot_pool, dead = [], set()
            if bot_pool or dead:
                live_str = ", ".join(f"@{b}" for b in bot_pool) or "— нет"
                dead_str = f" · 💀 dead: {', '.join('@' + b for b in sorted(dead))}" if dead else ""
                lines.append(f"🤖 боты поиска: {live_str}{dead_str}")
            # Расширенная диагностика "почему не ищет"
            no_answer = lk_stats.get('no_answer', 0)
            unres = lk_stats.get('bot_unresolved', 0)
            rate_lim = lk_stats.get('rate_limited', 0)
            resolve_lim = lk_stats.get('resolve_limit', 0)
            skipped = lk_stats.get('skipped', 0)
            expired = lk_stats.get('expired', 0)
            no_bot = lk_stats.get('no_bot_pool', 0)
            no_sess = lk_stats.get('no_session', 0)
            bypass = pipe.stats.get('owner_bot_dead_bypass', 0)
            extras = []
            if no_bot:
                extras.append(f"боты dead {no_bot}")
            if no_sess:
                extras.append(f"нет сессий {no_sess}")
            if no_answer:
                extras.append(f"бот молчал {no_answer}")
            if unres:
                extras.append(f"username не найден {unres}")
            if rate_lim:
                extras.append(f"rate-limit {rate_lim}")
            if resolve_lim:
                extras.append(f"resolve-limit {resolve_lim}")
            if skipped:
                extras.append(f"очередь полна {skipped}")
            if expired:
                extras.append(f"устарело {expired}")
            if bypass:
                extras.append(f"bypass dead-bot {bypass}")
            if extras:
                lines.append(f"   ⤷ {' · '.join(extras)}")
            # Капчи
            cap_rand = lk_stats.get("captcha_random", 0)
            rand_part = f" · рандом-решено {cap_rand}" if cap_rand else ""
            active_part = f" · активных сейчас {pend_cap}" if pend_cap else ""
            lines.append(f"🧩 капч: {lk_stats.get('captcha', 0)} · авто-решено {lk_stats.get('captcha_auto', 0)}"
                         f"{rand_part} · не смог {lk_stats.get('captcha_auto_fail', 0)}{active_part}")
        lines += [
            f"Повторы {pipe.stats.get('duplicates', 0)} · смена цены {coord.stats.get('price_moves', 0)} · "
            f"старые {coord.stats.get('silent', 0)} · устаревшие {pipe.stats.get('stale', 0)} · "
            f"автозамена аккаунта {pipe.stats.get('failover', 0)} · ошибки опроса {coord.stats.get('poll_errors', 0)}",
            f"Просмотрено лотов всего: {coord.stats['scanned']}",
            *([f"Скрытые продавцы: найдено {pipe.stats.get('hidden_owner_found', 0)}, "
               f"не раскрыты {pipe.stats.get('hidden_owner_unknown', 0)}"]
              if pipe.stats.get('hidden_owner_found') or pipe.stats.get('hidden_owner_unknown') else []),
        ]
        if settings.test_mode:
            lines += ["", "🧪 <b>Тестовый режим</b>: в канал ничего не уходит"]
        return "\n".join(lines)

    def _status_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="status:refresh"),
            InlineKeyboardButton(text="⚙️ Панель", callback_data="status:panel"),
        ]])

    @users.message(or_f(Command("status"), F.text == BTN_STATUS))
    async def cmd_status(m: Message) -> None:
        await _answer_long(m, _status_text(), reply_markup=_status_kb())

    @public.callback_query(F.data == "sess:refresh")
    async def cb_sessions_refresh(q: CallbackQuery) -> None:
        if not is_allowed(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        try:
            text, kb = _sessions_view(q.from_user.id)
            await q.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                      disable_web_page_preview=True)
            await q.answer("Обновлено")
        except Exception as e:
            if "message is not modified" in str(e):
                await q.answer("Без изменений")
            else:
                await q.answer(f"Ошибка: {type(e).__name__}", show_alert=True)

    @public.callback_query(F.data == "status:refresh")
    async def cb_status_refresh(q: CallbackQuery) -> None:
        if not is_allowed(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        try:
            await q.message.edit_text(_status_text(), reply_markup=_status_kb(),
                                      parse_mode="HTML", disable_web_page_preview=True)
            await q.answer("Обновлено")
        except Exception as e:
            if "message is not modified" in str(e):
                await q.answer("Без изменений")
            else:
                await q.answer(f"Ошибка: {type(e).__name__}", show_alert=True)

    @public.callback_query(F.data == "status:panel")
    async def cb_status_panel(q: CallbackQuery) -> None:
        if not is_allowed(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        text, kb = _panel()
        try:
            await q.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                      disable_web_page_preview=True)
            await q.answer()
        except Exception:
            await q.answer()

    def _boot_note() -> str:
        if not app.backup.restore_done:
            return "⏳ Бот только что запущен: восстанавливаю аккаунты из копии…\n\n"
        if app.booting:
            return (f"⏳ Аккаунты восстановлены ({app.backup.restored_count}), подключаю их "
                    f"(пауза до {settings.startup_delay:g} с после деплоя)…\n\n")
        return ""

    def _sessions_view(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
        c = app.sessions.counts()
        lines = [_boot_note() + 
            f"Total sessions: {len(app.sessions.workers)}",
            f"🟢 Active: {c.get('ACTIVE', 0)}",
            f"🔴 Error: {c.get('ERROR', 0) + c.get('AUTH_ERROR', 0)}",
            f"⏳ FloodWait: {c.get('FLOOD_WAIT', 0)}",
            f"🟡 Connecting: {c.get('CONNECTING', 0)}", "",
        ]
        owners = {r.name: r.owner_id for r in app.storage.list_sessions()}
        shards = app.coordinator.shard_counts()
        rows = []
        icons = {"ACTIVE": "🟢", "FLOOD_WAIT": "⏳", "CONNECTING": "🟡"}
        for name, w in sorted(app.sessions.workers.items())[:95]:
            ping = getattr(w, "ping_ms", None)
            speed = getattr(w, "speed", 1.0)
            risk = w.risk() if hasattr(w, "risk") else "ok"
            health = ("" if risk == "ok" else
                      (" ⚠️ риск, замедлен" if risk == "warn" else " 🟥 высокий риск, сильно замедлен")
                      + f" x{speed:.2f}")
            line = (f"{icons.get(w.status.value, '🔴')} <b>{html.escape(name)}</b> — {w.status.value}"
                    + (f" {ping:.0f}ms" if ping is not None else "")
                    + f", коллекций: {shards.get(name, 0)}{health}")
            if w.status.value not in ("ACTIVE", "CONNECTING"):
                line += f"\n    Reason: {html.escape((w.reason or '—')[:120])}"
            lines.append(line)
            if can_manage(uid, owners.get(name)) and len(f"rmsess_ok:{name}".encode()) <= 64:
                btn = InlineKeyboardButton(text=f"🗑 {name}", callback_data=f"rmsess:{name}")
                if rows and len(rows[-1]) < 3:
                    rows[-1].append(btn)
                else:
                    rows.append([btn])
        if len(app.sessions.workers) > 95:
            lines.append(f"… и ещё {len(app.sessions.workers) - 95}")
        rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="sess:refresh"),
                     InlineKeyboardButton(text="📊 Статус", callback_data="status:refresh")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    @users.message(or_f(Command("sessions"), F.text == BTN_SESSIONS))
    async def cmd_sessions(m: Message) -> None:
        text, kb = _sessions_view(m.from_user.id)
        # 45+ аккаунтов не влезают в одно сообщение (лимит Telegram 4096) — режем на части по строкам
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3800:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        if cur.strip():
            chunks.append(cur)
        for i, chunk in enumerate(chunks):
            await m.answer(chunk, reply_markup=kb if i == len(chunks) - 1 else None)

    async def _remove_session(uid: int, name: str) -> str:
        owners = {r.name: r.owner_id for r in app.storage.list_sessions()}
        if name not in owners and name not in app.sessions.workers:
            return f"❌ Не найдена: {html.escape(name[:64])}"
        if not can_manage(uid, owners.get(name)):
            return "⛔ Удалять можно только свои аккаунты (или если вы админ)."
        app.backup.bury(name, "удалён вручную")
        await app.sessions.remove(name)
        saved = await app.backup.save(allow_shrink=True)
        return (f"✅ {html.escape(name)} удалена" +
                ("" if saved else f"\n⚠️ Копия не обновлена: {html.escape(app.backup.last_error or '')}"))

    @users.callback_query(F.data.startswith("rmsess:"))
    async def cb_rmsess(q: CallbackQuery) -> None:
        name = q.data.split(":", 1)[1]
        if not isinstance(q.message, Message):
            await q.answer()
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Да, удалить {name}", callback_data=f"rmsess_ok:{name}"),
            InlineKeyboardButton(text="✖ Отмена", callback_data="rmsess_no")]])
        await q.message.answer(f"Удалить {html.escape(name)}? Аккаунт перестанет использоваться трекером.",
                               reply_markup=kb)
        await q.answer()

    @users.callback_query(F.data.startswith("rmsess_ok:"))
    async def cb_rmsess_ok(q: CallbackQuery) -> None:
        await q.answer()
        text = await _remove_session(q.from_user.id, q.data.split(":", 1)[1])
        await _edit_or_send(q, text)

    @users.callback_query(F.data == "rmsess_no")
    async def cb_rmsess_no(q: CallbackQuery) -> None:
        await q.answer()
        await _edit_or_send(q, "Отменено.")

    @users.message(Command("remove_session"))
    async def cmd_remove(m: Message, command: CommandObject) -> None:
        name = (command.args or "").strip()
        if not re.fullmatch(r"[\w\-]+", name or ""):
            text, kb = _sessions_view(m.from_user.id)
            await _answer_long(m, "Выберите аккаунт для удаления:\n\n" + text, reply_markup=kb)
            return
        await m.answer(await _remove_session(m.from_user.id, name))

    @admin.message(or_f(Command("pause", "off"), F.text == BTN_PAUSE))
    async def cmd_pause(m: Message) -> None:
        if not app.subs.ready.is_set():
            await m.answer("⏳ Бот ещё восстанавливает данные после запуска — повторите через минуту.")
            return
        await app.coordinator.set_paused(True)
        await app.backup.save()          # чтобы выключение сохранилось и после деплоя
        await m.answer("🔒 Парсер выключен. Лоты не ищутся, пользователям бот пишет «Парсер закрыт».\n"
                       "Включить: /on")

    @admin.message(Command("close"))
    async def cmd_close(m: Message) -> None:
        if not app.subs.ready.is_set():
            await m.answer("⏳ Бот ещё восстанавливает данные после запуска — повторите через минуту.")
            return
        app.users_closed = True
        app.backup.flags["users_closed"] = 1
        await app.backup.save()
        await m.answer("🔒 Для пользователей закрыто: бот пишет «Парсер закрыт», подписку не продаёт.\n"
                       "Лоты в канал продолжают идти. Открыть: /open · выключить сам парсинг: /off")

    @admin.message(Command("open"))
    async def cmd_open(m: Message) -> None:
        if not app.subs.ready.is_set():
            await m.answer("⏳ Бот ещё восстанавливает данные после запуска — повторите через минуту.")
            return
        app.users_closed = False
        app.backup.flags["users_closed"] = 0
        await app.backup.save()
        await m.answer("🟢 Для пользователей открыто.")

    @admin.message(or_f(Command("resume", "on"), F.text == BTN_RESUME))
    async def cmd_resume(m: Message) -> None:
        if not app.subs.ready.is_set():
            await m.answer("⏳ Бот ещё восстанавливает данные после запуска — повторите через минуту.")
            return
        await app.coordinator.set_paused(False)
        await app.backup.save()
        await m.answer("🟢 Парсер включён.")

    @admin.message(Command("proxies"), F.chat.type == "private")   # пароли прокси — только в личке
    async def cmd_proxies(m: Message, command: CommandObject) -> None:
        """/proxies — список; /proxies + прокси с новой строки — заменить пул и раздать аккаунтам."""
        arg = (command.args or "").strip().lower()
        if command.args and arg not in ("off", "clear", "нет", "-", "выкл", "очистить"):
            await _delete(m)                       # в сообщении логины/пароли прокси
        if not _proxies_on() and arg not in ("off", "clear", "нет", "-", "выкл", "очистить"):
            await m.answer("🌐 Прокси выключены: все аккаунты работают напрямую.\n"
                           "Чтобы включить: <code>USE_PROXIES=1</code> в .env и перезапуск, потом "
                           "<code>/proxies</code> со списком.")
            return
        if arg in ("off", "clear", "нет", "-", "выкл", "очистить"):
            app.storage.save_pool([])                  # очистить пул прокси
            cleared = 0
            for r in app.storage.list_sessions():
                if r.proxy:
                    app.storage.set_proxy(r.name, None)
                    w = app.sessions.workers.get(r.name)
                    if w is not None:
                        await w.change_proxy(None)
                    cleared += 1
            await app.backup.save()
            await m.answer(f"✅ Пул прокси очищен, прокси снят с {cleared} аккаунт(ов). "
                           "Все работают напрямую, новые аккаунты тоже без прокси.")
            return
        new = split_lines(command.args or "")
        if command.args and not new:
            await m.answer("Не распознал ни одного прокси. Формат: socks5://user:pass@host:port "
                           "или host:port:user:pass — по одному на строку. Очистить всё: <code>/proxies off</code>")
            return
        if new:
            await _delete(m)                       # в сообщении логины/пароли прокси
            app.storage.save_pool(new)
            moved = await _assign_proxies()
            await app.backup.save()
            await m.answer(f"✅ Пул прокси: {len(new)} шт. Аккаунтов переведено на прокси: {moved}.\n"
                           f"Лимит: {settings.accounts_per_proxy} аккаунта на один прокси.")
            return
        pool = app.storage.load_pool()
        usage: dict[str, list[str]] = {p: [] for p in pool}
        for r in app.storage.list_sessions():
            if r.proxy in usage:
                usage[r.proxy].append(r.name)
        lines = [f"<b>🌐 Прокси: {len(pool)}</b> (до {settings.accounts_per_proxy} аккаунтов на один)", ""]
        lines += [f"{html.escape(mask_proxy(p))} — {', '.join(u) or 'свободен'}" for p, u in usage.items()]
        no_proxy = [r.name for r in app.storage.list_sessions() if not r.proxy]
        if no_proxy:
            lines += ["", f"⚠️ Без прокси: {', '.join(no_proxy)}"]
        lines += ["", "Задать пул: отправьте <code>/proxies</code> и с новой строки список, по одному на строку:",
                  "<code>socks5://user:pass@1.2.3.4:1080</code> или <code>1.2.3.4:1080:user:pass</code>",
                  "Отдельному аккаунту: <code>/proxy session_001 socks5://…</code> или <code>/proxy session_001 off</code>"]
        await m.answer("\n".join(lines))

    async def _assign_proxies() -> int:
        """Аккаунтам без прокси (или с прокси, которого больше нет в пуле) выдать прокси из пула."""
        pool = set(app.storage.load_pool())
        moved = 0
        for r in app.storage.list_sessions():
            if r.proxy and r.proxy in pool:
                continue
            proxy = app.storage.pick_proxy(settings.accounts_per_proxy)
            if not proxy:
                break
            app.storage.set_proxy(r.name, proxy)
            w = app.sessions.workers.get(r.name)
            if w is not None:
                await w.change_proxy(proxy)
            moved += 1
        return moved

    @admin.message(Command("proxy"), F.chat.type == "private")
    async def cmd_proxy(m: Message, command: CommandObject) -> None:
        await _delete(m)                           # в сообщении может быть пароль прокси
        if not _proxies_on():
            await m.answer("Прокси выключены (USE_PROXIES=0 в .env). Включите USE_PROXIES=1 и перезапустите.")
            return
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in app.sessions.workers:
            await m.answer("Использование: <code>/proxy session_001 socks5://user:pass@host:port</code> "
                           "или <code>/proxy session_001 off</code>")
            return
        name, raw = parts
        await _delete(m)
        proxy = None if raw.strip().lower() in ("off", "none", "-", "нет") else normalize_proxy(raw)
        if raw.strip().lower() not in ("off", "none", "-", "нет") and not proxy:
            await m.answer("Не распознал прокси. Формат: socks5://user:pass@host:port")
            return
        w = app.sessions.workers.get(name)
        if w is None:
            await m.answer(f"❌ Аккаунт {html.escape(name[:64])} уже удалён")
            return
        app.storage.set_proxy(name, proxy)
        ok = await w.change_proxy(proxy)
        await app.backup.save()
        await m.answer(f"{'✅' if ok else '⚠️'} {html.escape(name)}: {html.escape(mask_proxy(proxy))}"
                       + ("" if ok else f"\nНе подключился: {html.escape(w.reason or '')}"))

    import_lock = asyncio.Lock()          # несколько файлов сразу — по очереди, не вперемешку

    @admin.message(F.document, F.chat.type == "private")
    async def on_backup_file(m: Message) -> None:
        """Админ прислал файл sessions.backup (например от старого бота) → вернуть из него аккаунты."""
        doc = m.document
        if not doc or not (doc.file_name or "").endswith(".backup"):
            return
        if not app.backup.restore_done:
            await m.answer("⏳ Бот ещё восстанавливает свою копию после запуска — пришлите файл через минуту.")
            return
        if (doc.file_size or 0) > 19 * 1024 * 1024:
            await m.answer("❌ Файл больше 19 МБ — Telegram не даёт боту его скачать.")
            return
        note = await m.answer("📥 Читаю резервную копию…" + (" (жду очереди)" if import_lock.locked() else ""))
        try:
            async with import_lock:
                buf = io.BytesIO()
                await app.bot.download(doc, destination=buf)
                n = await app.backup.import_file(buf.getvalue())
                added = await app.sessions.rescan()
                saved = await app.backup.save()
        except ValueError as e:
            await note.edit_text(f"❌ {html.escape(str(e))}")
            return
        except Exception as e:
            log.warning("Backup import failed: %s", e)
            await note.edit_text(f"❌ Не удалось импортировать: {html.escape(type(e).__name__)}")
            return
        await note.edit_text(
            f"✅ Из файла добавлено аккаунтов: <b>{n}</b> (подключено: {len(added)}).\n"
            "Подписки и история отправленных лотов тоже перенесены."
            + ("" if saved else "\n⚠️ Новая копия пока не сохранилась — бот повторит сам.")
            + "\nСостояние аккаунтов: /sessions")

    @admin.message(Command("lot"))
    async def cmd_lot(m: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        mt = re.search(r"(?:nft/)?([A-Za-z0-9]+-\d+)\s*$", arg)
        if not mt:
            await m.answer("Использование: <code>/lot https://t.me/nft/PlushPepe-123</code>")
            return
        slug = mt.group(1)
        rows = await app.db.listings_by_slug(slug)
        if not rows:
            await m.answer(f"Лот <code>{html.escape(slug)}</code> трекер не видел как новый.\n"
                           "Возможно: коллекция пропущена по флору (/find), лот выставлен до синхронизации "
                           "или это смена цены уже известного лота.")
            return
        names = {"SENT": "✅ отправлен", "FILTERED_PRICE": "💰 отсеян по цене", "FILTERED_SELLER": "👤 отсеян по продавцу",
                 "FILTERED_DUPLICATE": "🔁 повтор", "SEND_FAILED": "❌ не отправился", "ERROR": "⌛ устарел/ошибка",
                 "NEW": "⏳ в очереди", "SENDING": "📤 отправляется", "TEST_ONLY": "🧪 тест"}
        out = [f"🔎 <b>{html.escape(slug)}</b>"]
        for r in rows:
            price = f"{r['stars_price']}⭐" if r.get("stars_price") else (f"{r['ton_price']:g} TON" if r.get("ton_price") else "—")
            who = f"@{r['seller_username']}" if r.get("seller_username") else (r.get("seller_name") or "—")
            out.append(f"\n{(r.get('first_seen') or '')[:19].replace('T', ' ')} UTC · {html.escape(price)} · "
                       f"{html.escape(str(who))}\n{names.get(r['status'], html.escape(str(r['status'])))}"
                       + (f": {html.escape(str(r.get('reason') or '')[:200])}" if r.get("reason") else ""))
        await m.answer("\n".join(out))

    @admin.message(Command("raw"))
    async def cmd_raw(m: Message, command: CommandObject) -> None:
        """/raw <slug> — сырой ответ Telegram на GetUniqueStarGiftRequest(slug): какие поля
        владельца приходят и кто в res.users[]. Для диагностики скрытых владельцев."""
        arg = (command.args or "").strip()
        mt = re.search(r"(?:nft/)?([A-Za-z0-9]+-\d+)\s*$", arg)
        if not mt:
            await m.answer("Использование: <code>/raw PlushPepe-123</code> или <code>/raw https://t.me/nft/PlushPepe-123</code>")
            return
        slug = mt.group(1)
        try:
            from telethon.tl.functions.payments import GetUniqueStarGiftRequest
            res, _sess = await app.sessions.run(
                lambda c: c(GetUniqueStarGiftRequest(slug=slug)), priority=True, attempts=2, timeout=15)
        except Exception as e:
            await m.answer(f"❌ Запрос не прошёл: <code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:200])}</code>")
            return
        g = getattr(res, "gift", None)
        if g is None:
            await m.answer("Telegram ответил без gift.")
            return
        def _peer(p):
            t = type(p).__name__
            uid = getattr(p, "user_id", None) or getattr(p, "channel_id", None) or getattr(p, "chat_id", None)
            return f"{t}({uid})" if uid is not None else t if p is not None else "None"
        users = res.users or []
        chats = res.chats or []
        lines = [f"🔬 <b>raw {html.escape(slug)}</b>", "", "<b>gift fields:</b>",
                 f"owner_id: <code>{html.escape(_peer(getattr(g, 'owner_id', None)))}</code>",
                 f"host_id: <code>{html.escape(_peer(getattr(g, 'host_id', None)))}</code>",
                 f"released_by: <code>{html.escape(_peer(getattr(g, 'released_by', None)))}</code>",
                 f"theme_peer: <code>{html.escape(_peer(getattr(g, 'theme_peer', None)))}</code>",
                 f"owner_address: <code>{html.escape(str(getattr(g, 'owner_address', None) or '—'))}</code>",
                 f"gift_address: <code>{html.escape(str(getattr(g, 'gift_address', None) or '—'))}</code>",
                 f"owner_name: <code>{html.escape(str(getattr(g, 'owner_name', None) or '—'))}</code>",
                 "", f"<b>users[{len(users)}]:</b>"]
        for u in users[:10]:
            name = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x) or "—"
            uname = f"@{u.username}" if getattr(u, "username", None) else "—"
            lines.append(f"• <code>{getattr(u, 'id', '?')}</code> {html.escape(uname)} ({html.escape(name)})")
        if len(users) > 10:
            lines.append(f"… ещё {len(users) - 10}")
        if chats:
            lines.append(f"\n<b>chats[{len(chats)}]:</b> " + ", ".join(f"<code>{getattr(c, 'id', '?')}</code>" for c in chats[:10]))
        await m.answer("\n".join(lines))

    @admin.message(Command("why"))
    async def cmd_why(m: Message, command: CommandObject) -> None:
        try:
            hours = min(48.0, max(0.25, float((command.args or "1").replace(",", "."))))
        except ValueError:
            hours = 1.0
        try:
            text = await app.why_report(hours)
        except Exception as e:
            text = f"Не удалось собрать отчёт: {html.escape(str(e)[:200])}"
        try:
            rights = await app._chat_rights_report()
        except Exception:
            rights = []
        if rights:
            text += "\n\n<b>Права бота в группе:</b>\n" + "\n".join(rights)
        await m.answer(text)

    @admin.message(Command("top"))
    async def cmd_top(m: Message) -> None:
        """Самые активные коллекции за сутки: сколько новых лотов нашлось и сколько ушло в канал."""
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))
        rows = await app.db.top_collections(since)
        if not rows:
            await m.answer("За сутки новых лотов ещё не было.")
            return
        floors = {st.title: st.floor_stars for st in app.coordinator.states.values()}
        lines = ["<b>🔥 Топ коллекций за сутки</b>", "найдено → отправлено · флор", ""]
        for i, (name, n, sent) in enumerate(rows, 1):
            fl = floors.get(name)
            lines.append(f"{i}. {html.escape(name)} — {n} → {sent}" + (f" · {fl:,}⭐".replace(",", " ") if fl else ""))
        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("sellers"))
    async def cmd_sellers(m: Message, command: CommandObject) -> None:
        """Топ продавцов за сутки: кто больше всех выставляет лотов, сколько ушло в канал.
        /sellers — все; /sellers 48 — за 48 часов."""
        try:
            hours = min(168, max(1, int((command.args or "24").strip())))
        except ValueError:
            hours = 24
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600))
        rows = await app.db.top_sellers(since, limit=25)
        if not rows:
            await m.answer(f"За {hours}ч лоты не выставлялись.")
            return
        lines = [f"<b>👥 Топ продавцов за {hours}ч</b>", "ключ · юз · найдено → отправлено", ""]
        for i, (key, uname, n, sent) in enumerate(rows, 1):
            u = f"@{uname}" if uname else "—"
            lines.append(f"{i}. <code>{html.escape(key[:40])}</code> · {html.escape(u)} — {n} → {sent}")
        await _answer_long(m, "\n".join(lines))

    def _match_collections(q: str) -> list[str]:
        titles = sorted({st.title for st in app.coordinator.states.values() if st.title})
        exact = [t for t in titles if t.lower() == q]
        return exact or [t for t in titles if q in t.lower()]

    @admin.message(Command("mute", "unmute"))
    async def cmd_mute(m: Message, command: CommandObject) -> None:
        """/mute название — не присылать лоты этой коллекции (опрос продолжается); /unmute — вернуть."""
        unmute = (m.text or "").lstrip("/").lower().startswith("unmute")
        q = (command.args or "").strip().lower()[:64]
        muted = set(app.backup.flags.get("muted") or [])
        if not q:
            lst = ", ".join(sorted(muted)) or "нет"
            await m.answer(f"🔇 Выключенные коллекции: {html.escape(lst)}\n\n"
                           "Выключить: <code>/mute название</code> · включить: <code>/unmute название</code>")
            return
        found = [t for t in sorted(muted) if q in t] if unmute else _match_collections(q)
        if not found:
            await m.answer(f"Не нашёл «{html.escape(q)}». Список: /collections" if not unmute
                           else f"«{html.escape(q)}» не выключена.")
            return
        if len(found) > 1 and not any(t.lower() == q for t in found):
            await m.answer("Подходит несколько, уточните:\n" + "\n".join(html.escape(t) for t in found[:20]))
            return
        name = next((t for t in found if t.lower() == q), found[0]).lower()
        if unmute:
            muted.discard(name)
        else:
            muted.add(name)
        app.backup.flags["muted"] = sorted(muted)
        await app.backup.save()
        await m.answer((f"🔊 Коллекция «{html.escape(name)}» снова присылает лоты." if unmute else
                        f"🔇 Коллекция «{html.escape(name)}» выключена: лоты из неё не приходят. "
                        f"Вернуть: <code>/unmute {html.escape(name)}</code>"))

    @admin.message(Command("find"))
    async def cmd_find(m: Message, command: CommandObject) -> None:
        """/find Plush Pepe — что трекер знает о коллекции: в работе ли, флор, как часто опрашивается."""
        q = (command.args or "").strip().lower()
        coord = app.coordinator
        if not q:
            await m.answer("Использование: <code>/find название коллекции</code>, например <code>/find pepe</code>")
            return
        found = [st for st in coord.states.values() if q in (st.title or "").lower()][:5]
        if not found:
            await m.answer(f"Коллекция «{html.escape(q[:64])}» не найдена. Список: /collections")
            return
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))
        now = time.monotonic()
        out = []
        for st in found:
            if st.gift_id in coord.skipped:
                state = f"🚫 пропущена (флор ниже {settings.collection_min_floor}⭐)"
            elif coord._is_live(st):
                state = "🟢 в работе"
            else:
                state = "🟡 синхронизируется"
            prio = "⭐ дорогая (опрашивается чаще)" if coord._is_priority(st.gift_id) else "обычная"
            last = coord._last_poll_mono.get(st.gift_id)
            gap = coord._poll_gap.get(st.gift_id)
            c = await app.db.collection_counts(st.gift_id, since)
            out.append("\n".join([
                f"<b>{html.escape(st.title)}</b>",
                f"{state} · {prio}",
                f"Флор: {st.floor_stars or '—'}⭐ · лотов на рынке: {st.availability_resale or 0}",
                f"Последний опрос: {f'{now - last:.0f} с назад' if last else '—'}"
                + (f" · обычно раз в {gap:.1f} с" if gap and gap < 10 else (f" · обычно раз в {gap:.0f} с" if gap else "")),
                f"За сутки: найдено {sum(c.values())}, отправлено {c.get('SENT', 0)}, "
                f"отсеяно по продавцу {c.get('FILTERED_SELLER', 0)}",
            ]))
        await _answer_long(m, "\n\n".join(out))

    @admin.message(Command("removed"))
    async def cmd_removed(m: Message) -> None:
        g = sorted(app.backup.graveyard.values(), key=lambda x: x.get("ts", 0), reverse=True)
        if not g:
            await m.answer("Удалённых аккаунтов нет (хранятся 14 дней).")
            return
        now = time.time()
        lines = [f"🗑 Удалённые аккаунты ({len(g)}), хранятся 14 дней:\n"]
        for x in g:
            ago = int((now - x.get("ts", now)) // 3600)
            lines.append(f"• <code>{html.escape(x['name'])}</code> — {ago} ч назад — "
                         f"{html.escape((x.get('reason') or '—')[:90])}")
        lines.append("\nВернуть: /revive ИМЯ или /revive all (по-настоящему мёртвые снова удалятся сами)")
        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("revive"))
    async def cmd_revive(m: Message, command: CommandObject) -> None:
        if app.booting:
            await m.answer("⏳ Идёт запуск — повторите через минуту.")
            return
        arg = (command.args or "").strip()
        names = list(app.backup.graveyard) if arg.lower() == "all" else [arg]
        if not arg or not names or not all(n in app.backup.graveyard for n in names):
            await m.answer("Использование: /revive ИМЯ или /revive all. Список — /removed")
            return
        await m.answer(f"⏳ Возвращаю {len(names)} акк. и проверяю подключение...")
        ok, dead, wait = [], [], []
        for n in names:
            try:
                target = app.backup.unbury(n)
            except Exception as e:
                log.warning("Revive %s failed: %s", n, e)
                continue
            rec = next((r for r in app.storage.list_sessions() if r.name == target), None) if target else None
            if rec is None:
                continue
            w = await app.sessions.add_record(rec)
            st = w.status.value
            (ok if st == "ACTIVE" else dead if st == "AUTH_ERROR" else wait).append(target)
        await app.backup.save()
        await _answer_long(m, f"✅ Работают: {len(ok)}" + (f" ({', '.join(ok)})" if ok else "") +
                       f"\n⏳ Переподключаются (FloodWait/сеть): {len(wait)}" +
                       (f" ({', '.join(wait)})" if wait else "") +
                       f"\n❌ Действительно мёртвые (удалятся снова): {len(dead)}" +
                       (f" ({', '.join(dead)})" if dead else ""))

    @admin.message(Command("captcha_status"))
    async def cmd_captcha_status(m: Message) -> None:
        """Диагностика решателя капч: включён ли OCR, Vision API, сколько решено авто/рандомом.
        Помогает понять, почему `авто-решено 0` — либо tesseract не поставлен, либо капчи не идут."""
        lk = app.lookup
        auto = getattr(lk, "_auto", None)
        lines = ["🧩 <b>Статус решателя капч</b>", ""]
        if auto is None:
            lines.append("❌ Решатель не инициализирован.")
        else:
            lines.append(f"• Включён: {'✅ да' if auto.enabled else '⬜ нет (CAPTCHA_AUTO_SOLVE=false)'}")
            lines.append(f"• OCR (tesseract + Pillow): {'✅ есть' if auto._ocr_available else '❌ нет'}")
            lines.append(f"• Vision API: {'✅ есть' if auto._vision.enabled else '⬜ нет'}")
            lines.append(f"• Рандом-фоллбек: {'✅ да' if getattr(lk, '_random_fallback', False) else '⬜ нет'}")
            try:
                from tracker.captcha_solver import captcha_cache_stats
                cs = captcha_cache_stats()
                lines.append(f"• Кеш решённых: {cs['size']} / {cs['max']}")
            except Exception:
                pass
        st = lk.stats if lk else {}
        lines += ["", "<b>Статистика за запуск</b>"]
        lines.append(f"Капч всего: {st.get('captcha', 0)}")
        lines.append(f"✅ Авто-решено (OCR/Vision): {st.get('captcha_auto', 0)}")
        lines.append(f"🎲 Рандом-решено: {st.get('captcha_random', 0)}")
        lines.append(f"❌ Не смог (ушло админу): {st.get('captcha_auto_fail', 0)}")
        loop_give_up = st.get("captcha_loop_give_up", 0)
        if loop_give_up:
            lines.append(f"🌀 Зациклил капчу (5 подряд): {loop_give_up}")
        if auto and not auto._ocr_available:
            lines += ["",
                      "⚠️ OCR недоступен — проверьте, что в Docker-образе есть "
                      "<code>tesseract-ocr</code> и <code>pytesseract</code>. "
                      "В текущем Dockerfile они должны быть — пересоберите образ."]
        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("lookup_last"))
    async def cmd_lookup_last(m: Message) -> None:
        """Последние ~50 lookup-операций с результатом. Удобно когда надо понять:
        'вижу что lookups > 0, а find = 0 — что именно происходит на каждом лоте?'
        """
        import time as _t
        lk = app.lookup
        buf = getattr(lk, "_recent_lookups", None)
        if buf is None or not buf:
            await m.answer("Пока не было ни одного lookup. Либо трекер только запустился, "
                           "либо не было hidden-owner лотов.")
            return
        now = _t.time()
        # покажем последние 20
        items = list(buf)[-20:][::-1]
        icons = {"found": "✅", "from_memory": "💾", "not_found": "❌", "no_candidates": "🫥",
                 "too_many": "👥", "no_answer": "🔕", "rate_limited": "🚫", "neg_cached": "📦",
                 "trusted": "🤝", "captcha": "🧩"}
        lines = [f"🔍 <b>Последние lookup ({len(items)} из {len(buf)})</b>", ""]
        for ts, name, result, extra in items:
            age = now - ts
            if age < 60:
                age_s = f"{int(age)}с"
            elif age < 3600:
                age_s = f"{int(age / 60)}мин"
            else:
                age_s = f"{age / 3600:.1f}ч"
            icon = icons.get(result, "·")
            extra_fmt = f" — <i>{html.escape(extra)}</i>" if extra else ""
            lines.append(f"{icon} <code>{html.escape(name[:30])}</code> → {html.escape(result)}{extra_fmt} "
                         f"<i>({age_s})</i>")
        lines += ["", "<i>Легенда: ✅ найден · 💾 из памяти · ❌ не найден · 🫥 нет кандидатов · 👥 слишком много · 🔕 бот молчал · 🚫 rate-limit · 📦 негативный кеш</i>"]
        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("lookup_memory"))
    async def cmd_lookup_memory(m: Message) -> None:
        """Сколько имён владельцев хранится в памяти БД (kv 'owner:*') + несколько примеров.

        Эта память переживает рестарты по дизайну — follow-up lookup одного и того же продавца
        идёт мгновенно без запроса к funstat, экономит квоту."""
        try:
            n = await app.db.kv_count_prefix("owner:")
        except Exception as e:
            await m.answer(f"❌ {html.escape(str(e))}")
            return
        lines = [f"💾 <b>Память владельцев:</b> <b>{n}</b> имён в БД",
                 "", "<i>При новом лоте с таким продавцом lookup пропускается — используется "
                 "сохранённый user_id. Экономит funstat-квоту.</i>"]
        if n > 0:
            try:
                sample = await app.db.kv_sample_prefix("owner:", 10)
                if sample:
                    import json as _json
                    lines += ["", "<b>Примеры (10):</b>"]
                    for k, v in sample:
                        name = k[len("owner:"):][:30]
                        try:
                            items = _json.loads(v) if v else []
                            first = items[0] if items else {}
                            info = f"u{first.get('id')} @{first.get('u') or '—'}"
                            if len(items) > 1:
                                info += f" (+{len(items) - 1})"
                        except Exception:
                            info = "?"
                        lines.append(f"  <code>{html.escape(name)}</code> → {html.escape(info)}")
            except Exception as e:
                lines.append(f"\n(не удалось выбрать примеры: {html.escape(str(e)[:80])})")
        lines += ["", "Удалить ошибочную запись: <code>/lookup_forget &lt;имя&gt;</code>",
                  "Посмотреть одну запись: <code>/lookup_debug &lt;имя&gt;</code>"]
        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("lookup_forget"))
    async def cmd_lookup_forget(m: Message) -> None:
        """'/lookup_forget Иван Петров' — удалить из памяти БД запись найденного владельца.

        Полезно если funstat когда-то вернул не того человека, и теперь каждый новый лот
        с этим именем идёт в канал с неверным владельцем. Удалит из kv owner:<name>,
        следующий lookup пойдёт по новой через бота."""
        arg = (m.text or "").split(maxsplit=1)
        if len(arg) < 2:
            await m.answer("Нужно имя: <code>/lookup_forget Иван Петров</code>")
            return
        name = arg[1].strip()
        if len(name) < 2:
            await m.answer("Имя слишком короткое.")
            return
        import tracker.lookup as L
        key = f"owner:{L._norm_name(name)}"
        raw = await app.db.kv_get(key)
        if not raw:
            await m.answer(f"ℹ️ В памяти БД нет записи для <code>{html.escape(name)}</code>.")
            return
        # Показать что было, перед удалением
        try:
            import json as _json
            items = _json.loads(raw) if raw else []
        except Exception:
            items = []
        await app.db.kv_set(key, None)
        # Также снять из негативного кеша, чтобы следующий лот сразу пошёл к боту
        try:
            app.lookup._neg_cache.pop(L._norm_name(name), None)
            app.lookup._cache.pop(L._norm_name(name), None)
        except Exception:
            pass
        reply = [f"🧹 Удалена запись для <code>{html.escape(name)}</code>.",
                 "", "Было:"]
        for i in items[:5]:
            reply.append(f"  u{i.get('id')} @{i.get('u') or '—'}")
        if len(items) > 5:
            reply.append(f"  ... и ещё {len(items) - 5}")
        reply.append("")
        reply.append("Следующий лот с этим именем пойдёт в lookup по-новой.")
        await m.answer("\n".join(reply))

    @admin.message(Command("lookup_debug"))
    async def cmd_lookup_debug(m: Message) -> None:
        """'/lookup_debug Иван Петров' — показать пошагово что будет с таким именем:
        - в памяти БД (найденный ранее владелец)
        - в положительном кеше (кандидаты от funstat, час назад)
        - в негативном кеше (бот сказал 'не знаю', не долбим час)
        - inflight (уже спрашивают с другого лота — ждём)
        - пул ботов для него
        - пул сессий для него
        Если ничего — делает реальный lookup.
        """
        arg = (m.text or "").split(maxsplit=1)
        if len(arg) < 2:
            await m.answer("Нужно имя: <code>/lookup_debug Иван Петров</code>")
            return
        name = arg[1].strip()
        if len(name) < 2:
            await m.answer("Имя слишком короткое.")
            return
        import tracker.lookup as L
        import time as _t
        lk = app.lookup
        key = L._norm_name(name)
        lines = [f"🔍 <b>Debug lookup для</b> <code>{html.escape(name)}</code>", "",
                 f"Нормализованный ключ: <code>{html.escape(key)}</code>"]

        # 1) память (БД)
        try:
            known = await lk._known(name)
            if known:
                lines.append(f"\n<b>💾 В памяти БД: {len(known)} запись(ей)</b>")
                for k in known[:3]:
                    lines.append(f"  u{k.user_id} @{k.username or '—'}")
                lines.append("  → lookup вернёт <b>from_memory</b> без обращения к боту")
            else:
                lines.append("\n💾 В памяти БД: нет")
        except Exception as e:
            lines.append(f"\n💾 Память: ошибка {html.escape(str(e)[:80])}")

        # 2) позитивный кеш
        now = _t.monotonic()
        cached = lk._cache.get(key)
        if cached:
            age = now - cached[0]
            if age < 3600:
                lines.append(f"\n<b>✅ Положительный кеш (TTL {3600 - int(age)}с осталось): "
                             f"{len(cached[1])} кандидат(ов)</b>")
                for c in cached[1][:5]:
                    lines.append(f"  {c.key()} — {html.escape(c.name or '—')}")
            else:
                lines.append(f"\n✅ Кеш был, но протух ({int(age)}с назад)")
        else:
            lines.append("\n✅ Положительный кеш: пусто")

        # 3) негативный кеш
        neg = lk._neg_cache.get(key)
        if neg:
            age = now - neg
            if age < 3600:
                lines.append(f"\n<b>❌ Негативный кеш: 'не найдено' {int(age)}с назад "
                             f"(ещё {3600 - int(age)}с не будем долбить)</b>")
                lines.append("  → lookup вернёт <b>None</b> без обращения к боту")
            else:
                lines.append(f"\n❌ Негативный кеш был, но протух ({int(age)}с назад)")
        else:
            lines.append("\n❌ Негативный кеш: пусто")

        # 4) inflight
        if key in lk._inflight:
            lines.append("\n<b>⏳ Inflight: уже идёт запрос с другого лота — новый будет ждать его результат</b>")

        # 5) пул ботов
        bots = lk._bot_pool()
        if bots:
            lines.append(f"\n<b>🤖 Пул ботов ({len(bots)}): {', '.join('@' + b for b in bots)}</b>")
        else:
            lines.append("\n⚠️ <b>Пул ботов ПУСТ</b> — lookup не стартует (боты dead / не настроены)")

        # 6) пул сессий
        sessions = lk._pick_sessions()
        if sessions:
            lines.append(f"\n<b>💼 Пул сессий ({len(sessions)}): {', '.join(sessions[:5])}"
                         + (f" ... и ещё {len(sessions) - 5}" if len(sessions) > 5 else "") + "</b>")
        else:
            lines.append("\n⚠️ <b>Пул сессий ПУСТ</b> — все залипли или не LOOKUP_SESSIONS")

        if not bots or not sessions:
            lines.append("\n<i>Реальный lookup не выполняется из-за пустых пулов.</i>")
        else:
            lines.append("\n<i>Чтобы запустить реальный lookup: <code>/test_bot</code> или просто дождаться "
                         "лота с таким именем.</i>")

        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("lookup_reset"))
    async def cmd_lookup_reset(m: Message) -> None:
        """Полный сброс lookup-состояния: _neg_cache, _bot_dead_until, _bot_unresolvable,
        _bot_consec_unres, _bot_dead_notified. НЕ трогает память найденных владельцев (БД).

        Используется чтобы 'с чистого листа' проверить работает ли lookup. Например:
        после смены LOOKUP_BOT_USERNAME, после ручного восстановления funstat и т.п."""
        lk = app.lookup
        before = {
            "neg_cache": len(lk._neg_cache),
            "bot_dead": len(lk._bot_dead_until or {}),
            "unresolvable": len(lk._bot_unresolvable or {}),
            "consec": len(lk._bot_consec_unres or {}),
        }
        # сбросить dead-marks (через _clear_bot_unresolved — заодно почистит persistent)
        for k in list(lk._bot_dead_until or {}):
            lk._clear_bot_unresolved(k)
        lk._neg_cache.clear()
        lk._bot_unresolvable.clear()
        lk._bot_consec_unres.clear()
        lk._bot_dead_notified.clear()
        # captcha_recent тоже сбросим, чтобы адаптивный interval вернулся к базовому
        if hasattr(lk, "_captcha_recent"):
            lk._captcha_recent = []
        lines = ["🧹 <b>Lookup полностью сброшен</b>", "",
                 f"Было в кешах:",
                 f"  негативный: <b>{before['neg_cache']}</b> имён",
                 f"  dead-ботов: <b>{before['bot_dead']}</b>",
                 f"  unresolvable: <b>{before['unresolvable']}</b> (сессия, бот)",
                 f"  подряд-fails: <b>{before['consec']}</b> ботов",
                 "", "Сейчас — пустые. Следующие lookup пойдут 'с нуля'."]
        await m.answer("\n".join(lines))

    @admin.message(Command("lookup_cache"))
    async def cmd_lookup_cache(m: Message) -> None:
        """Диагностика кешей lookup:
        - sizeof cache hit (положительный кеш кандидатов по имени)
        - sizeof neg_cache (имена, которые funstat 'не знает', на час)
        - inflight (запросы к боту прямо сейчас — у продавца несколько лотов, не долбим)
        - pending (лотов ждёт свою очередь к боту)
        - active captchas
        - unresolvable-записи (сессия → когда последний раз провалилась)
        - dead-marks (бот → когда снимется)
        """
        import time as _t
        lk = app.lookup
        lines = ["🧠 <b>Кеши lookup</b>", ""]
        lines.append(f"• положительный кеш кандидатов: <b>{len(lk._cache)}</b> имён (TTL 1 ч)")
        lines.append(f"• негативный кеш 'не найдено': <b>{len(lk._neg_cache)}</b> имён (TTL 1 ч)")
        lines.append(f"• inflight (ждут общего ответа): <b>{len(lk._inflight)}</b>")
        lines.append(f"• pending (в очереди к боту): <b>{lk._pending}</b> / макс {lk._pending} + "
                     f"{getattr(lk, '_MAX_PENDING', 5000) - lk._pending if hasattr(lk, '_MAX_PENDING') else '—'}")
        lines.append(f"• активных капч: <b>{len(lk._captcha)}</b>")
        # unresolvable
        now = _t.monotonic()
        unres_live = [(k, now - v) for k, v in (lk._bot_unresolvable or {}).items()
                      if now - v < 300]
        if unres_live:
            lines += ["", f"<b>⚠️ unresolvable сессии ({len(unres_live)}):</b>"]
            for (sess, bot), age in unres_live[:10]:
                lines.append(f"  <code>{html.escape(sess)}</code> + @{html.escape(bot)} — {age:.0f}с назад")
            if len(unres_live) > 10:
                lines.append(f"  <i>…и ещё {len(unres_live) - 10}</i>")
        dead_live = {k: v for k, v in (lk._bot_dead_until or {}).items() if v > now}
        if dead_live:
            lines += ["", f"<b>💀 dead-боты ({len(dead_live)}):</b>"]
            for k, until in dead_live.items():
                left_min = (until - now) / 60
                lines.append(f"  @{html.escape(k)} — ещё <b>{left_min:.1f}</b> мин")
        unres_counters = {k: v for k, v in (lk._bot_consec_unres or {}).items() if v > 0}
        if unres_counters:
            lines += ["", "<b>Подряд unresolvable (ещё не dead):</b>"]
            for k, n in unres_counters.items():
                lines.append(f"  @{html.escape(k)} — <b>{n}</b> фейл(ов) подряд")
        lines += ["", "<i>Кеши живут в RAM, при рестарте стираются (кроме dead-mark'ов — они в БД)</i>"]
        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("bots_add"))
    async def cmd_bots_add(m: Message) -> None:
        """'/bots_add infostat' — добавить запасного lookup-бота в LOOKUP_BOTS.
        Если уже есть — без изменений. Если это primary — предупредим."""
        arg = (m.text or "").split(maxsplit=1)
        if len(arg) < 2:
            await m.answer("Нужно имя: <code>/bots_add infostat</code>")
            return
        new_bot = arg[1].lstrip("@").strip()
        if not new_bot or len(new_bot) < 3:
            await m.answer("Имя слишком короткое.")
            return
        primary = (settings.lookup_bot_username or "").strip()
        if new_bot.lower() == primary.lower():
            await m.answer(f"⚠️ @{html.escape(new_bot)} уже основной бот, зачем в запасные?")
            return
        extras = list(settings.lookup_bots or [])
        if new_bot.lower() in {b.lower() for b in extras}:
            await m.answer(f"ℹ️ @{html.escape(new_bot)} уже в запасных.")
            return
        extras.append(new_bot)
        try:
            set_runtime_value("LOOKUP_BOTS", ",".join(extras))
        except Exception as e:
            await m.answer(f"❌ Не удалось сохранить: {html.escape(str(e))}")
            return
        await app.backup.save()
        pool = app.lookup._bot_pool()
        await m.answer(f"✅ @{html.escape(new_bot)} добавлен в запасные. Пул сейчас:\n"
                       + "\n".join(f"  {i+1}. @{html.escape(b)}" for i, b in enumerate(pool)))

    @admin.message(Command("bots_remove"))
    async def cmd_bots_remove(m: Message) -> None:
        """'/bots_remove infostat' — удалить запасного из LOOKUP_BOTS.
        Нельзя удалить primary (используй /bots_set_primary для смены)."""
        arg = (m.text or "").split(maxsplit=1)
        if len(arg) < 2:
            await m.answer("Нужно имя: <code>/bots_remove infostat</code>")
            return
        target = arg[1].lstrip("@").strip().lower()
        primary = (settings.lookup_bot_username or "").strip().lower()
        if target == primary:
            await m.answer(f"❌ @{html.escape(target)} сейчас ОСНОВНОЙ бот, нельзя удалить из запасных.\n"
                           f"Сначала смени основной: <code>/bots_set_primary &lt;другой&gt;</code>")
            return
        extras = list(settings.lookup_bots or [])
        new_extras = [b for b in extras if b.lower() != target]
        if len(new_extras) == len(extras):
            await m.answer(f"ℹ️ @{html.escape(target)} и так не в запасных.")
            return
        try:
            set_runtime_value("LOOKUP_BOTS", ",".join(new_extras))
        except Exception as e:
            await m.answer(f"❌ Не удалось сохранить: {html.escape(str(e))}")
            return
        await app.backup.save()
        pool = app.lookup._bot_pool()
        await m.answer(f"✅ @{html.escape(target)} удалён из запасных. Пул сейчас:\n"
                       + ("\n".join(f"  {i+1}. @{html.escape(b)}" for i, b in enumerate(pool))
                          if pool else "  (пусто)"))

    @admin.message(Command("bots_set_primary"))
    async def cmd_bots_set_primary(m: Message) -> None:
        """'/bots_set_primary infostat' — быстрая смена основного lookup-бота.
        Эквивалентно '/set LOOKUP_BOT_USERNAME infostat' + сброс dead-mark этого бота +
        автоматическое добавление старого primary в LOOKUP_BOTS (если его там не было)
        — он будет запасным, а не потерян."""
        arg = (m.text or "").split(maxsplit=1)
        if len(arg) < 2:
            await m.answer("Нужно имя: <code>/bots_set_primary infostat</code>")
            return
        new_bot = arg[1].lstrip("@").strip()
        if not new_bot or len(new_bot) < 3:
            await m.answer("Имя слишком короткое.")
            return
        old_primary = (settings.lookup_bot_username or "").strip()
        # 1) добавить старый primary в LOOKUP_BOTS (если его там не было)
        extras = list(settings.lookup_bots or [])
        if old_primary and old_primary.lower() not in {b.lower() for b in extras} \
                and old_primary.lower() != new_bot.lower():
            extras.insert(0, old_primary)
            try:
                set_runtime_value("LOOKUP_BOTS", ",".join(extras))
            except Exception as e:
                log.warning("couldn't add %s to LOOKUP_BOTS: %s", old_primary, e)
        # 2) установить новый primary
        try:
            set_runtime_value("LOOKUP_BOT_USERNAME", new_bot)
        except Exception as e:
            await m.answer(f"❌ Не удалось установить: {html.escape(str(e))}")
            return
        # 3) снять dead-mark с нового primary (если был)
        if hasattr(app.lookup, "_clear_bot_unresolved"):
            app.lookup._clear_bot_unresolved(new_bot)
        await app.backup.save()
        reply = [f"✅ Основной бот → @{html.escape(new_bot)}"]
        if old_primary:
            reply.append(f"Бывший @{html.escape(old_primary)} → добавлен в запасные")
        reply.append("")
        reply.append("Текущий пул: " + ", ".join(f"@{b}" for b in app.lookup._bot_pool()))
        await m.answer("\n".join(reply))

    @admin.message(Command("bots_clear_dead"))
    async def cmd_bots_clear_dead(m: Message) -> None:
        """Вручную снять dead-mark с бота (если знаешь что он восстановился раньше 15 мин).
        '/bots_clear_dead funstat' снимает с конкретного, без аргумента — со ВСЕХ помеченных."""
        lk = app.lookup
        arg = (m.text or "").split(maxsplit=1)
        target = arg[1].lstrip("@").strip().lower() if len(arg) > 1 else None
        dead = dict(getattr(lk, "_bot_dead_until", {}) or {})
        if not dead:
            await m.answer("ℹ️ Нет ботов с dead-mark.")
            return
        if target and target not in dead:
            await m.answer(f"ℹ️ @{html.escape(target)} не помечен dead. Текущие: "
                           + ", ".join(f"@{b}" for b in dead.keys()))
            return
        cleared = []
        for k in list(dead.keys()):
            if target is None or k == target:
                lk._clear_bot_unresolved(k)
                cleared.append(k)
        await m.answer(f"✅ Снят dead-mark с: {', '.join('@' + b for b in cleared)}\n\n"
                       "Следующие lookup-операции попытаются использовать этих ботов.\n"
                       "Если они всё ещё не отвечают — dead-mark будет поставлен снова после "
                       "≥3 подряд unresolvable-фейлов.")

    @admin.message(Command("disk_usage"))
    async def cmd_disk_usage(m: Message) -> None:
        """Показать размер основных файлов трекера. На Render с 1GB диском полезно
        знать растёт ли что-то неконтролируемо."""
        import os as _os
        from pathlib import Path as _P
        items = [
            ("БД трекера", settings.path(settings.database_path)),
            ("Папка sessions", settings.path(settings.sessions_dir)),
            ("Папка data", settings.path("data")),
            ("Папка logs", settings.path("logs")),
        ]
        # Captcha cache — через env var
        cc_path = _os.environ.get("CAPTCHA_CACHE_PATH") or "/var/data/captcha_cache.json"
        items.append(("Кеш капч", _P(cc_path)))

        def _size(p: _P) -> int:
            if not p.exists():
                return 0
            if p.is_file():
                try:
                    return p.stat().st_size
                except OSError:
                    return 0
            total = 0
            try:
                for root, _, files in _os.walk(p):
                    for f in files:
                        try:
                            total += _os.path.getsize(_os.path.join(root, f))
                        except OSError:
                            pass
            except OSError:
                pass
            return total

        def _fmt(n: int) -> str:
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024:
                    return f"{n:.1f} {unit}"
                n /= 1024
            return f"{n:.1f} TB"

        total = 0
        lines = ["💾 <b>Размер файлов трекера</b>", ""]
        for name, p in items:
            s = _size(p)
            total += s
            exists = "" if p.exists() else " (не существует)"
            lines.append(f"  {name}: <b>{_fmt(s)}</b> — <code>{html.escape(str(p))}</code>{exists}")
        lines.append("")
        lines.append(f"🧮 <b>Всего:</b> {_fmt(total)}")
        # Free disk space
        try:
            import shutil as _sh
            db_dir = settings.path(settings.database_path).parent
            du = _sh.disk_usage(str(db_dir))
            lines.append(f"🧊 Свободно на диске: <b>{_fmt(du.free)}</b> из {_fmt(du.total)} "
                         f"(занято {int(100 * (du.total - du.free) / du.total)}%)")
        except Exception as e:
            lines.append(f"<i>disk_usage: {html.escape(str(e)[:80])}</i>")
        await m.answer("\n".join(lines))

    @admin.message(Command("health"))
    async def cmd_health(m: Message) -> None:
        """Статус HTTP health endpoint'а (если включён через HEALTH_PORT). Показывает ссылки
        на /health /live /stats /metrics для копипаста в мониторинг."""
        import os as _os
        port_raw = (_os.environ.get("HEALTH_PORT") or "").strip()
        if not port_raw or port_raw == "0":
            await m.answer(
                "⬜ <b>HTTP health endpoint выключен.</b>\n\n"
                "Чтобы включить, задай <code>HEALTH_PORT=8080</code> в .env / Render envVars и "
                "перезапусти.\n\n"
                "После включения будут доступны:\n"
                "  • <code>GET /health</code>, <code>/live</code> — 200 OK для liveness-check\n"
                "  • <code>GET /stats</code> — JSON сводка (mode, sessions, lookup)\n"
                "  • <code>GET /metrics</code> — Prometheus text exposition\n\n"
                "На Render задай в Health Check Path: <code>/health</code> — Render автоматически "
                "перезапустит worker при зависании.")
            return
        runner = getattr(app, "_health_runner", None)
        running = runner is not None
        state = "🟢 ЗАПУЩЕН" if running else "🔴 НЕ ЗАПУЩЕН (ошибка при старте, смотри логи)"
        await m.answer(
            f"🏥 <b>HTTP health endpoint</b>: {state}\n\n"
            f"Порт: <b>{port_raw}</b>\n\n"
            "Эндпоинты:\n"
            f"  • <code>http://localhost:{port_raw}/health</code> — liveness\n"
            f"  • <code>http://localhost:{port_raw}/stats</code> — JSON сводка\n"
            f"  • <code>http://localhost:{port_raw}/metrics</code> — Prometheus\n\n"
            "На Render: в настройках сервиса → Health Check Path: <code>/health</code>")

    @admin.message(Command("dashboard"))
    async def cmd_dashboard(m: Message) -> None:
        """Единая быстрая сводка на одной странице: все критичные метрики + inline-кнопки
        для перехода к детальным командам. Делает не больше одной БД-читки."""
        import time as _t
        ws = list(app.sessions.workers.values())
        counts = app.sessions.counts()
        active = counts.get("ACTIVE", 0)
        flood = counts.get("FLOOD_WAIT", 0)
        err = counts.get("ERROR", 0) + counts.get("AUTH_ERROR", 0)
        eff = app.sessions.effective()
        fpm = app.sessions.flood_rate_per_minute()
        p = app.pipeline.stats
        c = app.coordinator.stats
        lk = getattr(app.lookup, "stats", {}) or {}
        live, total = app.coordinator.sync_progress()
        # last activity
        now = _t.time()
        last_sent = getattr(app.pipeline, "last_sent_at", None)
        last_new = app.pipeline.last_new_at
        sent_ago = f"{int(now - last_sent)}с" if last_sent else "—"
        new_ago = f"{int(now - last_new)}с" if last_new else "—"
        # lookup health
        bots_pool = []
        dead_bots = []
        try:
            bots_pool = app.lookup._bot_pool()
            now_m = _t.monotonic()
            dead_bots = [b for b, v in (app.lookup._bot_dead_until or {}).items() if v > now_m]
        except Exception:
            pass
        # health emoji
        if app.coordinator.paused:
            mood = "⏸"
        elif not active:
            mood = "🚨"
        elif fpm >= 15 or active < len(ws) * 0.4:
            mood = "⚠️"
        elif dead_bots:
            mood = "⚠️"
        else:
            mood = "🟢"

        lines = [f"{mood} <b>Дашборд</b>  <i>{__import__('datetime').datetime.now().strftime('%H:%M:%S')}</i>",
                 ""]

        # Sessions
        lines.append(f"👥 <b>Аккаунты</b>: 🟢{active} ⏳{flood} 🔴{err} из {len(ws)} · "
                     f"мощность {eff:.1f}")
        if fpm >= 5:
            lines.append(f"  🌩 FloodWait: <b>{fpm}/мин</b>")

        # Collections
        lines.append(f"📚 <b>Коллекции</b>: {live}/{total} в LIVE")

        # Pipeline
        lines.append(f"🎁 <b>Лоты</b>: найдено <b>{p.get('new', 0)}</b> · "
                     f"отправлено <b>{p.get('sent', 0)}</b> · очередь {app.pipeline.queue.qsize()}")
        lines.append(f"   ⤷ последний найден <i>{new_ago} назад</i>, отправлен <i>{sent_ago} назад</i>")

        # Lookup
        if bots_pool or dead_bots:
            bot_str = ", ".join(f"@{b}" for b in bots_pool[:3])
            if len(bots_pool) > 3:
                bot_str += f" +{len(bots_pool) - 3}"
            if dead_bots:
                bot_str += " · 💀 " + ", ".join(f"@{b}" for b in dead_bots)
            lines.append(f"🔍 <b>Lookup</b>: {bot_str}")
            lines.append(f"   ⤷ попыток {lk.get('lookups', 0)} · найдено {lk.get('found', 0)} · "
                         f"из памяти {lk.get('from_memory', 0)} · не найдено {lk.get('not_found', 0)}")
            captcha = lk.get('captcha', 0)
            if captcha:
                captcha_solved = lk.get('captcha_auto', 0) + lk.get('captcha_random', 0)
                lines.append(f"   ⤷ 🧩 капч {captcha} (решено {captcha_solved})")
        elif not getattr(app.lookup, "any_bot_configured", lambda: False)():
            lines.append("🔍 <b>Lookup</b>: ⚠️ бот не настроен — hidden-owner лоты отсеиваются")

        # Warnings
        warnings = []
        if active == 0 and len(ws) > 0:
            warnings.append("🚨 Нет рабочих аккаунтов — лоты не ищутся")
        if dead_bots and len(bots_pool) == 0:
            warnings.append("🚨 Все lookup-боты dead — hidden-owner идут без фильтра")
        if last_sent and now - last_sent > 1800:
            warnings.append(f"⚠️ Последний пост {int((now - last_sent) / 60)} мин назад")
        if app.coordinator.paused:
            warnings.append("⏸ Парсер на паузе — включить: /on")
        if warnings:
            lines += [""] + warnings

        # Buttons for quick nav
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="dash:refresh"),
             InlineKeyboardButton(text="📊 /stat", callback_data="status:refresh")],
            [InlineKeyboardButton(text="🤖 Пул ботов", callback_data="bots:refresh"),
             InlineKeyboardButton(text="📋 Last", callback_data="lookup:last")],
            [InlineKeyboardButton(text="👥 Sessions", callback_data="dash:sessions"),
             InlineKeyboardButton(text="🧩 Кеши", callback_data="lookup:cache")],
            [InlineKeyboardButton(text="🎛 Панель", callback_data="dash:panel"),
             InlineKeyboardButton(text="❓ Why", callback_data="dash:why")],
        ])
        await m.answer("\n".join(lines), reply_markup=kb)

    @admin.callback_query(F.data.startswith("dash:"))
    async def cb_dash(q: CallbackQuery) -> None:
        if not is_manager(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        action = q.data.split(":", 1)[1]
        await q.answer()
        if q.message is None:
            return
        try:
            mm = q.message.model_copy(update={"from_user": q.from_user})
        except Exception:
            mm = q.message
        try:
            if action == "refresh":
                await cmd_dashboard(mm)
            elif action == "sessions":
                await cmd_sessions_stats(mm)
            elif action == "panel":
                await cmd_panel(mm)
            elif action == "why":
                # /why 1 час
                try:
                    report = await app.why_report(1.0)
                    await _answer_long(mm, report)
                except Exception as e:
                    await mm.answer(f"❌ {html.escape(str(e))}")
        except Exception as e:
            log.debug("cb_dash %s failed: %s", action, e)

    @admin.message(Command("help_lookup"))
    async def cmd_help_lookup(m: Message) -> None:
        """Компактный гайд по всем командам управления lookup в одном сообщении."""
        text = (
            "🔍 <b>Lookup — команды и флоу</b>\n\n"
            "<b>Статус и диагностика</b>\n"
            "<code>/bots</code> — пул ботов (+ inline-кнопки)\n"
            "<code>/lookup_cache</code> — кеши, inflight, dead-marks\n"
            "<code>/lookup_last</code> — последние 20 lookup-операций\n"
            "<code>/lookup_debug Иван</code> — пошагово что произойдёт с таким именем\n"
            "<code>/stat</code> — общая сводка с lookup-блоком\n\n"
            "<b>Тестирование</b>\n"
            "<code>/test_bots</code> — пинг всех ботов (бесплатно)\n"
            "<code>/test_bot @funstat Иван</code> — реальный запрос с ответом\n\n"
            "<b>Управление ботами</b>\n"
            "<code>/bots_set_primary @infostat</code> — сменить основной\n"
            "<code>/bots_add @findtg</code> — добавить запасного\n"
            "<code>/bots_remove @old</code> — убрать запасного\n"
            "<code>/bots_clear_dead [@bot]</code> — снять dead-mark\n\n"
            "<b>Обслуживание</b>\n"
            "<code>/lookup_reset</code> — полный сброс кешей\n"
            "<code>/lookup_forget Иван</code> — удалить ложный владелец\n"
            "<code>/clear_captcha_cache</code> — сброс кеша решённых капч\n"
            "<code>/captcha_status</code> — статус OCR/рандом-солвера\n\n"
            "<b>Настройки через /set</b>\n"
            "• <code>LOOKUP_BOT_USERNAME</code> — основной бот\n"
            "• <code>LOOKUP_BOTS</code> — запасные через запятую\n"
            "• <code>LOOKUP_CMDS</code> — per-bot команды\n"
            "• <code>LOOKUP_CMD_TEMPLATE</code> — общий шаблон\n"
            "• <code>LOOKUP_SESSIONS</code> — выделенные сессии\n"
            "• <code>LOOKUP_TIMEOUT</code>, <code>LOOKUP_INTERVAL</code>, "
            "<code>LOOKUP_REQUIRED</code>\n\n"
            "<b>Типичный флоу при проблемах</b>\n"
            "1. <code>/bots</code> — какие боты активные, какие dead\n"
            "2. <code>/lookup_last</code> — причина отказа каждой операции\n"
            "3. <code>/test_bots</code> — реально живые ли боты\n"
            "4. Если funstat мёртв:\n"
            "   <code>/bots_set_primary infostat</code>\n"
            "5. Если ложные владельцы в памяти:\n"
            "   <code>/lookup_debug Иван</code> → <code>/lookup_forget Иван</code>\n"
            "6. Если нужен полный сброс:\n"
            "   <code>/lookup_reset</code>"
        )
        await _answer_long(m, text)

    @admin.message(Command("test_bots"))
    async def cmd_test_bots(m: Message) -> None:
        """Пинг-тест ВСЕХ настроенных lookup-ботов (primary + LOOKUP_BOTS) через liveness-check.
        За 1 команду видно какие боты живы, какие снесли, и какие в dead-blacklist.
        Не тратит funstat-квоту — использует get_input_entity (dirt cheap)."""
        import time as _t
        lk = app.lookup
        if not hasattr(lk, "check_bots_liveness"):
            await m.answer("Эта версия lookup не поддерживает liveness-check.")
            return
        await m.answer("⏳ Пингую всех настроенных ботов...")
        t0 = _t.monotonic()
        try:
            res = await asyncio.wait_for(lk.check_bots_liveness(), timeout=60)
        except asyncio.TimeoutError:
            await m.answer("⚠️ Пинг занял больше 60 сек — вероятно сессии в FloodWait.")
            return
        except Exception as e:
            await m.answer(f"❌ Ошибка: <code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:200])}</code>")
            return
        dt = _t.monotonic() - t0
        if not res:
            await m.answer("ℹ️ Не настроено ни одного бота.\n\nДобавь:\n<code>/set LOOKUP_BOT_USERNAME funstat</code>")
            return
        alive = [b for b, ok in res.items() if ok]
        dead = [b for b, ok in res.items() if not ok]
        lines = [f"🤖 <b>Проверка ботов</b> ({dt:.1f}с, {len(res)} бот(ов))", ""]
        for b in alive:
            lines.append(f"✅ @{html.escape(b)} — отвечает")
        for b in dead:
            lines.append(f"💀 @{html.escape(b)} — DEAD (помечен на 15 мин)")
        if not alive:
            lines += ["", "⚠️ <b>Ни одного живого бота!</b> hidden-owner лоты пойдут с пометкой без фильтра."]
        elif not dead:
            lines += ["", "✅ Все боты в порядке."]
        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("test_bot"))
    async def cmd_test_bot(m: Message) -> None:
        """Ручной тест lookup-бота: '/test_bot [@username] [имя_для_поиска]'.
        Без аргументов: тест основного бота с именем 'Alex'.
        Используется чтобы убедиться что бот отвечает / корректно парсятся кандидаты.
        Пример: /test_bot @funstat Иван"""
        args = (m.text or "").split()[1:]
        bot_name = (args[0] if args else (settings.lookup_bot_username or "")).lstrip("@").strip()
        test_name = " ".join(args[1:]) if len(args) > 1 else "Alex"
        if not bot_name:
            await m.answer("Нужен бот: <code>/test_bot @funstat [имя]</code>")
            return
        if not test_name or len(test_name) < 2:
            await m.answer("Нужно имя для поиска (от 2 букв): <code>/test_bot @funstat Иван</code>")
            return
        await m.answer(f"⏳ Пишу @{html.escape(bot_name)} <code>{html.escape(test_name)}</code>...")
        import time as _t
        t0 = _t.monotonic()
        try:
            order = app.lookup._pick_sessions()
            if not order:
                await m.answer("❌ Нет рабочих сессий для lookup.")
                return
            session = order[0]
            # Прямой вызов — пробежимся по нормальному пути
            cands = await asyncio.wait_for(
                app.lookup._ask_bot(session, bot_name, test_name, prio=-1e12),
                timeout=max(30.0, settings.lookup_timeout + settings.lookup_captcha_wait + 10)
            )
            dt = _t.monotonic() - t0
            if cands is None:
                await m.answer(f"⚠️ Бот не ответил за {dt:.1f}с (таймаут / блокировка / капча не прошла). "
                               f"Сессия: <code>{html.escape(session)}</code>")
                return
            if not cands:
                await m.answer(f"⚠️ Бот ответил за {dt:.1f}с, но кандидатов нет (имя {test_name!r}).\n"
                               f"Сессия: <code>{html.escape(session)}</code>")
                return
            lines = [f"✅ Бот @{html.escape(bot_name)} ответил за {dt:.1f}с — <b>{len(cands)}</b> кандидат(ов):"]
            for i, c in enumerate(cands[:10], 1):
                tag = f"@{html.escape(c.username)}" if c.username else f"id<code>{c.user_id}</code>"
                nm = f" — {html.escape(c.name)}" if c.name else ""
                lines.append(f"{i}. {tag}{nm}")
            if len(cands) > 10:
                lines.append(f"<i>…и ещё {len(cands) - 10}</i>")
            await _answer_long(m, "\n".join(lines))
        except asyncio.TimeoutError:
            await m.answer(f"⚠️ Таймаут {_t.monotonic() - t0:.1f}с.")
        except Exception as e:
            await m.answer(f"❌ Ошибка: <code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:300])}</code>")

    @admin.message(Command("bots"))
    async def cmd_bots(m: Message) -> None:
        """Диагностика lookup-ботов: кто настроен, кто жив, кто помечен dead.

        При снесённом funstat сразу видно: 'основной @funstat 💀 dead еще 12 мин · fallback @infostat ✅'
        """
        import time as _t
        lk = app.lookup
        lines = ["🤖 <b>Lookup-боты</b>", ""]
        primary = (settings.lookup_bot_username or "").strip()
        extras = list(settings.lookup_bots or [])
        if not primary and not extras:
            lines.append("❌ Ни одного lookup-бота не настроено.")
            lines.append("")
            lines.append("Установи: <code>/set LOOKUP_BOT_USERNAME funstat</code>")
            lines.append("И запасные: <code>/set LOOKUP_BOTS infostat,findtg</code>")
            await _answer_long(m, "\n".join(lines))
            return

        dead_map = dict(getattr(lk, "_bot_dead_until", {}) or {})
        unres_map = getattr(lk, "_bot_consec_unres", {}) or {}
        pool = lk._bot_pool() if lk else []
        now = _t.monotonic()

        def _bot_line(b: str, role: str) -> str:
            k = b.lower()
            deadline = dead_map.get(k, 0.0)
            is_dead = deadline > now
            cmd_tpl = (settings.lookup_cmds or {}).get(k) or settings.lookup_cmd_template or "/human {name}"
            if is_dead:
                left = int(deadline - now)
                left_h = left // 60
                status = f"💀 <b>dead</b> ещё {left_h} мин"
            elif unres_map.get(k, 0):
                status = f"⚠️ unresolvable {unres_map[k]} подряд"
            elif b in pool:
                status = "✅ готов"
            else:
                status = "⬜ не в пуле"
            return f"{role} @{html.escape(b)} — {status} · <code>{html.escape(cmd_tpl)}</code>"

        if primary:
            lines.append(_bot_line(primary, "<b>Основной:</b>"))
        for i, b in enumerate(extras):
            if b.lower() == primary.lower():
                continue
            lines.append(_bot_line(b, f"<b>Запас #{i+1}:</b>"))

        lines.append("")
        lines.append(f"Активных в пуле: <b>{len(pool)}</b> из {len([p for p in [primary] + extras if p])}")
        if lk and lk.all_bots_dead():
            lines.append("⚠️ <b>Все боты dead</b> — hidden-owner лоты идут с пометкой без фильтра.")

        st = lk.stats if lk else {}
        if st.get("bot_unresolved") or st.get("bot_dead_marked"):
            lines += ["", "<b>Диагностика за запуск</b>"]
            lines.append(f"unresolvable фейлов: {st.get('bot_unresolved', 0)}")
            lines.append(f"пометок dead: {st.get('bot_dead_marked', 0)}")
        lines += ["", "Сменить основной: <code>/set LOOKUP_BOT_USERNAME &lt;@username&gt;</code>",
                  "Список запасных: <code>/set LOOKUP_BOTS bot1,bot2,bot3</code>",
                  "Команды per-bot: <code>/set LOOKUP_CMDS funstat:/human,infostat:/find</code>"]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏓 Пинг всех", callback_data="bots:ping"),
             InlineKeyboardButton(text="🔄 Обновить", callback_data="bots:refresh")],
            [InlineKeyboardButton(text="🧹 Снять dead-mark", callback_data="bots:cleardead"),
             InlineKeyboardButton(text="📊 /stat", callback_data="status:refresh")],
            [InlineKeyboardButton(text="📋 Последние lookup", callback_data="lookup:last"),
             InlineKeyboardButton(text="🧠 Кеши", callback_data="lookup:cache")],
        ])
        await _answer_long(m, "\n".join(lines), reply_markup=kb)

    @admin.callback_query(F.data.startswith("bots:"))
    async def cb_bots(q: CallbackQuery) -> None:
        """Inline-кнопки под /bots. Прямо вызывают нужные команды через существующие handlers.
        Message в aiogram 3 — pydantic, но text можно подменить через model_copy для одноразового
        вызова handler'а (чище чем править q.message in-place)."""
        if not is_manager(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        action = q.data.split(":", 1)[1]
        await q.answer({"ping": "Пингую всех ботов…",
                        "refresh": "Обновляю…",
                        "cleardead": "Сбрасываю dead-mark…"}.get(action, ""))
        if q.message is None:
            return
        cmd_text = {"ping": "/test_bots", "refresh": "/bots", "cleardead": "/bots_clear_dead"}.get(action)
        if not cmd_text:
            return
        try:
            m = q.message.model_copy(update={"text": cmd_text, "from_user": q.from_user})
        except Exception:
            m = q.message
        try:
            if action == "ping":
                await cmd_test_bots(m)
            elif action == "refresh":
                await cmd_bots(m)
            elif action == "cleardead":
                await cmd_bots_clear_dead(m)
        except Exception as e:
            log.debug("cb_bots action %s failed: %s", action, e)

    @admin.callback_query(F.data.startswith("lookup:"))
    async def cb_lookup_panel(q: CallbackQuery) -> None:
        if not is_manager(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        action = q.data.split(":", 1)[1]
        await q.answer()
        if q.message is None:
            return
        try:
            m = q.message.model_copy(update={"from_user": q.from_user})
        except Exception:
            m = q.message
        try:
            if action == "last":
                await cmd_lookup_last(m)
            elif action == "cache":
                await cmd_lookup_cache(m)
            elif action == "help":
                await cmd_help_lookup(m)
        except Exception as e:
            log.debug("cb_lookup action %s failed: %s", action, e)

    @admin.message(Command("reset_speeds"))
    async def cmd_reset_speeds(m: Message) -> None:
        """Сброс всех замедлений аккаунтов обратно до 1.0 (как после деплоя). Используй только
        когда уверен что шторм FloodWait закончился и можно разогнать. Иначе снова словишь."""
        reset = 0
        for w in app.sessions.workers.values():
            if getattr(w, "speed", 1.0) < 1.0:
                w.speed = 1.0
                w.recent_floods = []
                reset += 1
        # также снять адаптивный throttle на limiter
        app.sessions.limiter.set_rate(app.sessions._base_rps)
        app.sessions._throttle_until = 0.0
        await m.answer(f"⚡ Сброшено замедление у {reset} аккаунтов. GLOBAL_RPS → {app.sessions._base_rps:.1f}.\n"
                       "⚠️ Если шторм FloodWait ещё идёт — снова поймаешь.")

    @admin.message(Command("sessions_stats"))
    async def cmd_sessions_stats(m: Message) -> None:
        """Распределение сессий по speed (histogram) + топ-5 самых замедленных.
        Даёт быстрое понимание 'флот работает на 90% или сдох'."""
        import time as _t
        ws = list(app.sessions.workers.values())
        if not ws:
            await m.answer("Нет аккаунтов.")
            return
        # Histogram по speed
        buckets = {"1.0 (норма)": 0, "0.7–1.0": 0, "0.5–0.7": 0, "0.3–0.5": 0,
                   "0.15–0.3": 0, "<0.15 (почти мёртвый)": 0}
        now = _t.monotonic()
        for w in ws:
            s = getattr(w, "speed", 1.0)
            if s >= 1.0:
                buckets["1.0 (норма)"] += 1
            elif s >= 0.7:
                buckets["0.7–1.0"] += 1
            elif s >= 0.5:
                buckets["0.5–0.7"] += 1
            elif s >= 0.3:
                buckets["0.3–0.5"] += 1
            elif s >= 0.15:
                buckets["0.15–0.3"] += 1
            else:
                buckets["<0.15 (почти мёртвый)"] += 1

        lines = [f"👥 <b>Распределение скорости аккаунтов</b> ({len(ws)} шт.)", ""]
        for name, n in buckets.items():
            if n > 0:
                bar = "█" * min(20, n) + ("" if n <= 20 else f" +{n-20}")
                lines.append(f"<code>{name:30}</code> {bar}  <b>{n}</b>")

        # Effective capacity
        eff = app.sessions.effective()
        lines.append("")
        lines.append(f"🎯 Эффективная мощность: <b>{eff:.1f}</b> из {len(ws)} "
                     f"(<b>{100 * eff / max(1, len(ws)):.0f}%</b>)")

        # Топ-5 самых замедленных
        slow = sorted(ws, key=lambda w: getattr(w, "speed", 1.0))[:5]
        slow = [w for w in slow if getattr(w, "speed", 1.0) < 1.0]
        if slow:
            lines += ["", "<b>🐢 Топ-5 самых замедленных:</b>"]
            for w in slow:
                n_flood = sum(1 for t in getattr(w, "recent_floods", []) if now - t < 3600)
                lines.append(f"  <code>{html.escape(w.name)}</code>: speed <b>{w.speed:.2f}</b>, "
                             f"FloodWait за час: {n_flood}")

        # Советы
        hints = []
        if buckets["<0.15 (почти мёртвый)"] + buckets["0.15–0.3"] > len(ws) * 0.3:
            hints.append("⚠️ Больше 30% аккаунтов сильно замедлены — рассмотри /sessions_speedup или /proxies")
        fpm = app.sessions.flood_rate_per_minute()
        if fpm >= 10:
            hints.append(f"🌩 Шторм FloodWait сейчас: {fpm}/мин — трекер автоматически замедлил GLOBAL_RPS")
        if hints:
            lines += [""] + hints

        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("sessions_speedup"))
    async def cmd_sessions_speedup(m: Message) -> None:
        """Мягкий разгон: speed < 0.5 → 0.5 (не до 1.0 как reset_speeds). Безопаснее, если
        не уверен что шторм закончился: даёт ускорение, но оставляет страховочный запас.

        reset_speeds → 1.0 (риск шторма), sessions_speedup → 0.5 (ускорение ×2-3, безопасно).
        """
        upped = 0
        for w in app.sessions.workers.values():
            cur = getattr(w, "speed", 1.0)
            if cur < 0.5:
                w.speed = 0.5
                # НЕ очищаем recent_floods — сохраняем историю шторма, но не даём ему давить скорость
                upped += 1
        await m.answer(f"⚡ Мягкий разгон: {upped} аккаунт(ов) со speed &lt;0.5 → 0.5.\n"
                       "<i>recent_floods сохранены — трекер продолжает отслеживать шторм.</i>\n\n"
                       "Хочешь полный разгон (speed → 1.0, риск шторма): /reset_speeds")

    @admin.message(Command("routes"))
    async def cmd_routes(m: Message, command: CommandObject) -> None:
        """Показать текущие маршруты по цене (PRICE_ROUTES) и фонам (BACKDROP_ROUTES) в удобной форме.
        Меняется через /set PRICE_ROUTES 200-1000:chat_id/topic_id,1001-2500:chat_id/topic_id,..."""
        lines = ["<b>💰 Маршруты по цене (PRICE_ROUTES)</b>"]
        pr = settings.price_routes or []
        if not pr:
            lines.append("<i>не заданы — все лоты идут в TARGET_CHAT_ID</i>")
        else:
            for lo, hi, chat, thread in sorted(pr):
                rng = f"{lo}-{hi or '∞'}⭐"
                where = f"<code>{chat}</code>" + (f" / topic {thread}" if thread else "")
                lines.append(f"  {rng} → {where}")
        lines += ["", "<b>🖼 Маршруты по фону (BACKDROP_ROUTES)</b>"]
        br = settings.backdrop_routes or []
        if not br:
            lines.append("<i>не заданы</i>")
        else:
            for name, chat, thread in br:
                where = f"<code>{chat}</code>" + (f" / topic {thread}" if thread else "")
                lines.append(f"  «{html.escape(name)}» → {where}")
        lines += [
            "",
            "<b>Как менять:</b>",
            "<code>/set PRICE_ROUTES 200-1000:-1001234/5,1001-2500:-1001234/6</code>",
            "<code>/set BACKDROP_ROUTES rare:-1001234/7</code>",
            "<i>hi пустой = без верхней границы</i>",
        ]
        await _answer_long(m, "\n".join(lines))

    @admin.message(Command("flood"))
    async def cmd_flood(m: Message) -> None:
        """Диагностика FloodWait: за последний час по каждому аккаунту — сколько ловил, когда,
        какой speed сейчас. Помогает увидеть, какие сессии «горячие» и нужны ли прокси."""
        import time as _t
        now = _t.monotonic()
        rows = []
        total_h = 0
        for name, w in sorted(app.sessions.workers.items()):
            floods = getattr(w, "recent_floods", [])
            n = sum(1 for t in floods if now - t < 3600)
            total_h += n
            if n == 0 and w.speed >= 0.99:
                continue        # здоровые — пропускаем
            last_ago = int(now - floods[-1]) if floods else None
            last_s = f"{last_ago}с назад" if last_ago is not None and last_ago < 3600 else "—"
            status = w.status.value
            if w.status.value == "FLOOD_WAIT":
                remain = max(0, int(w.flood_until - now))
                status += f" ({remain}с)"
            rows.append(f"<code>{html.escape(name)}</code> · {status} · speed x{w.speed:.2f} · "
                        f"за час {n} · последний {last_s}")
        n_min = app.sessions.flood_rate_per_minute()
        base = app.sessions._base_rps
        cur = app.sessions.limiter.rate
        header = [
            f"🌩 <b>FloodWait за час: {total_h}</b> · за минуту: {n_min}",
            f"GLOBAL_RPS: {cur:.1f} из {base:.1f}" + (" (адаптивно замедлены)" if cur < base else ""),
            "",
        ]
        if not rows:
            header.append("Нет проблемных аккаунтов — все работают на полной скорости.")
        else:
            header.append("<b>Проблемные:</b>")
        await _answer_long(m, "\n".join(header + rows[:40]
                                        + ([f"… ещё {len(rows) - 40}"] if len(rows) > 40 else [])))

    @admin.message(Command("clear_captcha_cache"))
    async def cmd_clear_captcha_cache(m: Message) -> None:
        """Сбросить кеш решённых капч (если нажрался ложных ответов)."""
        try:
            from tracker.captcha_solver import _CAPTCHA_CACHE, _save_cache
            n = len(_CAPTCHA_CACHE)
            _CAPTCHA_CACHE.clear()
            # пометить dirty и сохранить
            import tracker.captcha_solver as _cs
            _cs._cache_dirty = True
            _save_cache()
            await m.answer(f"🧩 Кеш решённых капч очищен ({n} записей удалено).")
        except Exception as e:
            await m.answer(f"❌ Не удалось: {html.escape(str(e))}")

    @admin.message(Command("reload"))
    async def cmd_reload(m: Message) -> None:
        if app.booting:
            # во время STARTUP_DELAY старая копия бота ещё может работать: подключать аккаунты рано
            await m.answer("⏳ Идёт запуск — повторите через минуту.")
            return
        reload_settings()
        app.sessions.limiter.set_rate(settings.global_rps)
        app.sessions._base_rps = float(settings.global_rps)
        app.formatter.template_path = settings.path(settings.template_path)
        app.formatter.reload()
        app.seller_filter.reload()
        added = await app.sessions.rescan()
        await m.answer(f"🔄 Настройки, шаблон и списки продавцов перечитаны.\n"
                       f"Новых sessions: {len(added)}" + (f" ({', '.join(added)})" if added else ""))

    @users.message(or_f(Command("collections"), F.text == BTN_COLLECTIONS))
    async def cmd_collections(m: Message) -> None:
        """Какие коллекции отслеживаются, а какие пропущены по флору."""
        coord = app.coordinator
        states = sorted(coord.states.values(), key=lambda st: -(st.floor_stars or 0))
        if not states:
            await m.answer("Каталог коллекций ещё не загружен — подождите минуту после запуска.")
            return
        tracked = [st for st in states if st.gift_id not in coord.skipped]
        skipped = [st for st in states if st.gift_id in coord.skipped]

        def row(st) -> str:
            mark = ("🐢" if st.gift_id in coord.low_prio else "🟢") if coord._is_live(st) else "🟡"
            floor = f"{st.floor_stars}⭐" if st.floor_stars else "—"
            return f"{mark} {html.escape(st.title)} — флор {floor}, лотов {st.availability_resale or 0}"

        lines = [f"<b>📚 Отслеживается: {len(tracked)}</b>",
                 f"🟢 в работе, 🐢 дешёвая (флор ниже {settings.collection_min_floor}⭐, проверяется реже), "
                 "🟡 ещё синхронизируется", ""]
        lines += [row(st) for st in tracked] or ["—"]
        lines += ["", f"<b>🚫 Пропущено: {len(skipped)}</b> (флор ниже порога)"]
        cheap = [f"{html.escape(st.title)} ({st.floor_stars or '—'}⭐)" for st in skipped[:40]]
        lines.append(", ".join(cheap) + (f" и ещё {len(skipped) - 40}" if len(skipped) > 40 else ""))
        lines += ["", "Изменить порог: <code>/set COLLECTION_MIN_FLOOR 3000</code>"]
        text = "\n".join(lines)
        await _answer_long(m, text)

    @users.message(or_f(Command("last"), F.text == BTN_LAST))
    async def cmd_last(m: Message) -> None:
        """Последние найденные новые лоты и почему они (не) отправлены."""
        rows = await app.db.recent_listings(15)
        if not rows:
            await m.answer("Новых лотов пока не найдено. Проверьте /status — сколько коллекций уже в LIVE.")
            return
        icons = {"SENT": "✅", "FILTERED_PRICE": "💲", "FILTERED_SELLER": "👤", "FILTERED_DUPLICATE": "🔁",
                 "SEND_FAILED": "❌", "NEW": "⏳", "TEST_ONLY": "🧪", "ERROR": "⌛", "SENDING": "📤"}
        lines = ["<b>Последние новые лоты</b> (✅ отправлен, 💲 цена, 👤 продавец, 🔁 повтор, ⌛ устарел):", ""]
        for r in rows:
            price = f"{r['stars_price']}⭐" if r["stars_price"] is not None else "TON"
            reason = _human_reason(r["status"], r["reason"] or "", r["seller_filter_result"])
            lines.append(f"{icons.get(r['status'], '•')} {html.escape(r['gift_name'] or '?')} #{r['gift_num'] or ''} "
                         f"— {price}" + (f" — {html.escape(reason[:60])}" if r["status"] != "SENT" else ""))
        await _answer_long(m, "\n".join(lines))

    async def _stats_text() -> str:
        counts = await app.db.status_counts()
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))
        day = await app.db.counts_since(since)
        known = await app.db.count_market_items()
        p, c = app.pipeline.stats, app.coordinator.stats
        lines = ["<b>📊 Статистика</b>", "",
                 f"Известных лотов на рынке: {known}",
                 f"Опросов: {c['polls']} | ошибок: {c['poll_errors']}",
                 f"Старых лотов (тихо): {c['silent']} | смен цены: {c['price_moves']}", "",
                 "<b>С запуска:</b>",
                 f"Новых: {p['new']} | отправлено: {p['sent']}",
                 f"Отсеяно по цене: {p['filtered_price']} | по продавцу: {p['filtered_seller']}",
                 f"Повтор NFT/продавца: {p['filtered_repeat']} | дубликатов: {p['duplicates']}",
                 f"Ошибок отправки: {p['failed']}", "",
                 "<b>Всего в БД:</b>"]
        lines += [f"{_ST.get(k, k)}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])] or ["—"]
        rej = await app.db.seller_reject_counts()
        if rej:
            names = _REJ
            lines += ["", "<b>Почему отсеяны продавцы:</b>"]
            lines += [f"{names.get(k, k)}: {v}" for k, v in sorted(rej.items(), key=lambda x: -x[1])]
        lines += ["", "<b>За 24 часа:</b>"]
        lines += [f"{_ST.get(k, k)}: {v}" for k, v in sorted(day.items(), key=lambda x: -x[1])] or ["—"]
        return "\n".join(lines)

    def _stats_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="stats:refresh"),
            InlineKeyboardButton(text="📊 Статус", callback_data="status:refresh"),
        ]])

    @users.message(or_f(Command("stats"), F.text == BTN_STATS))
    async def cmd_stats(m: Message) -> None:
        await _answer_long(m, await _stats_text(), reply_markup=_stats_kb())

    @public.callback_query(F.data == "stats:refresh")
    async def cb_stats_refresh(q: CallbackQuery) -> None:
        if not is_allowed(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        try:
            await q.message.edit_text(await _stats_text(), reply_markup=_stats_kb(),
                                      parse_mode="HTML", disable_web_page_preview=True)
            await q.answer("Обновлено")
        except Exception as e:
            if "message is not modified" in str(e):
                await q.answer("Без изменений")
            else:
                await q.answer(f"Ошибка: {type(e).__name__}", show_alert=True)

    @users.message(or_f(Command("settings"), Command("filters")))
    async def cmd_settings(m: Message) -> None:
        await _answer_long(m, _filters_help())

    # ---------------- /panel: фильтры кнопками (без набора /set)
    _TOGGLES = (("RUSSIAN_ONLY", "russian_only", "Только русские продавцы"),
                ("SELLER_NAME_SIGNAL", "seller_name_signal", "Имя кириллицей = русский"),
                ("PHONE_MISMATCH_BLOCKS", "phone_mismatch_blocks", "Чужой телефон — отсеять"),
                ("UNIQUE_SELLERS", "unique_sellers", "Продавец не повторяется"),
                ("UNIQUE_GIFTS", "unique_gifts", "NFT не повторяется"),
                ("ALLOW_TON_ONLY", "allow_ton_only", "Лоты только за TON"),
                ("START_FRESH", "start_fresh", "После запуска — только новые лоты"),
                ("REQUIRE_USERNAME", "require_username", "Только продавцы с @юзом"),
                ("ALLOW_HIDDEN_OWNER", "allow_hidden_owner", "Лоты со скрытым владельцем"),
                ("AUTO_LOOKUP", "auto_lookup", "Автопоиск владельца (бот поиска)"),
                ("LOOKUP_REQUIRED", "lookup_required", "Скрытый владелец — только с найденным юзом"),
                ("LOOKUP_CHECK_ONSALE", "lookup_check_onsale", "После поиска проверить, что лот ещё продаётся"),
                ("LOOKUP_LANG_FILTER", "lookup_lang_filter", "Найденного владельца проверять по языку"),
                ("LOOKUP_TRUST_NAME_MATCH", "lookup_trust_name_match", "Верить funstat по имени (без проверки NFT)"))
    _REPEAT_STEPS = (0, 6, 24, 72)

    def _panel() -> tuple[str, InlineKeyboardMarkup]:
        rows = [[InlineKeyboardButton(text=f"{'✅' if getattr(settings, attr) else '⬜'} {title}",
                                      callback_data=f"flt:{key}")] for key, attr, title in _TOGGLES]
        rh = settings.seller_repeat_hours
        rows.append([InlineKeyboardButton(
            text="🔁 Повтор продавца: " + ("никогда" if not rh else f"через {rh:g} ч"), callback_data="flt:REPEAT")])
        rows.append([InlineKeyboardButton(
            text=f"📚 Коллекция подряд: {'до ' + str(settings.max_same_collection_streak) if settings.max_same_collection_streak else 'без ограничений'}",
            callback_data="flt:STREAK")])
        lb = settings.lookup_bot_username or ""
        rows.append([InlineKeyboardButton(
            text=f"🔍 Бот поиска: @{lb}" if lb else "🔍 Добавить бота поиска (funstat)",
            callback_data="flt:LOOKUPBOT")])
        accs = settings.lookup_sessions
        rows.append([InlineKeyboardButton(
            text="👤 Аккаунты для поиска: " + (", ".join(accs) if accs else "авто (первый рабочий)"),
            callback_data="flt:LKACC")])
        tb = settings.trade_bot_username or ""
        rows.append([InlineKeyboardButton(
            text=f"💰 Торговый бот: @{tb}" if tb else "💰 Добавить торгового бота (оффер)",
            callback_data="flt:TRADEBOT")])
        rows.append([InlineKeyboardButton(text="⚡ Скорость трекера", callback_data="flt:SPEED")])
        # быстрый переход к /bots (статус пула + inline-кнопки) и /help_lookup (все команды)
        rows.append([InlineKeyboardButton(text="🤖 Пул lookup-ботов", callback_data="bots:refresh"),
                     InlineKeyboardButton(text="📖 Гайд", callback_data="lookup:help")])
        rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="flt:REFRESH"),
                     InlineKeyboardButton(text="📊 Статус", callback_data="status:refresh")])
        # короткая строка о текущих настройках скорости — чтоб сразу видеть
        speed_line = (f"⚡ Скорость: маркет {settings.catalog_interval:g}s · "
                      f"трекер {settings.tracker_interval:g}s · "
                      f"funstat {settings.lookup_interval:g}s · "
                      f"капча-ждать {settings.lookup_captcha_wait:g}s")
        # мини-статус: сколько аккаунтов живо, сколько капч, какой темп
        c = app.sessions.counts()
        pipe_stats = app.pipeline.stats
        lk_stats = getattr(app.lookup, "stats", {}) if hasattr(app, "lookup") else {}
        # адаптивный throttle: если шторм FloodWait активен — покажем
        try:
            fpm = app.sessions.flood_rate_per_minute()
            base = app.sessions._base_rps
            cur = app.sessions.limiter.rate
            throttle_bar = f"\n⚠️ Адаптивный throttle: {fpm} FloodWait/мин, глобальный RPS {cur:.0f}/{base:.0f}" if cur < base else ""
        except Exception:
            throttle_bar = ""
        mini_status = (f"👥 Аккаунты: 🟢{c.get('ACTIVE', 0)} ⏳{c.get('FLOOD_WAIT', 0)} 🔴{c.get('ERROR', 0) + c.get('AUTH_ERROR', 0)}  "
                       f"| 🎁 найдено {pipe_stats.get('new', 0)} / отправлено {pipe_stats.get('sent', 0)}  "
                       f"| 🧩 капч: {lk_stats.get('captcha', 0)} (авто {lk_stats.get('captcha_auto', 0)})"
                       + throttle_bar)
        text = ("🎛 <b>Фильтры лотов</b>\nНажмите, чтобы переключить — применяется сразу и переживает деплой.\n"
                f"Мин. цена: {settings.min_stars}⭐ · флор коллекции от {settings.collection_min_floor}⭐ "
                "(менять: /set MIN_STARS …)\n"
                f"{speed_line}\n"
                f"{mini_status}\n"
                "Почему лоты отсеиваются: /why")
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    _LOOKUP_TIMEOUTS = (15, 25, 40, 60)

    def _captcha_cache_line() -> str:
        """Статус кеша решённых капч и распределения по слоям матчера."""
        try:
            from tracker.captcha_solver import captcha_cache_stats
            s = captcha_cache_stats()
            line = f"\nКеш решённых капч: {s['size']}/{s['max']}"
            lh = s.get("layer_hits") or {}
            if lh:
                parts = ", ".join(f"{k}:{v}" for k, v in sorted(lh.items(), key=lambda x: -x[1]))
                line += f"\nСлои матчера: {parts}"
            return line
        except Exception:
            return ""

    def _lookup_panel() -> tuple[str, InlineKeyboardMarkup]:
        """Подменю: какие аккаунты пишут боту поиска («/human имя») и как."""
        chosen = set(settings.lookup_sessions)
        rows = []
        for name in sorted(app.sessions.workers):
            w = app.sessions.workers[name]
            mark = "✅" if name in chosen else "⬜"
            alive = "" if w.is_usable() else " ⚠️"
            rows.append([InlineKeyboardButton(text=f"{mark} {name}{alive}", callback_data=f"lka:{name}"[:64])])
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if settings.lookup_dedicated else '⬜'} Только поиск (не опрашивают маркет)",
            callback_data="lka:~DED")])
        rows.append([InlineKeyboardButton(text=f"⏱ Ждать ответ бота: {settings.lookup_timeout:g} с",
                                          callback_data="lka:~TO")])
        rows.append([InlineKeyboardButton(text=f"🐢 Пауза между запросами: {settings.lookup_interval:g} с",
                                          callback_data="lka:~IV")])
        rows.append([InlineKeyboardButton(
            text=f"👥 Скип, если в списке от: {settings.lookup_skip_over or 'выкл'}", callback_data="lka:~SK")])
        lk = app.lookup.stats
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="lka:~BACK")])
        lb = settings.lookup_bot_username
        # авто-солвер капчи: OCR слова на картинке → эмодзи на кнопке
        try:
            auto = getattr(app.lookup, "_auto", None)
            ocr_ok = bool(auto and getattr(auto, "_ocr_available", False))
            api_ok = bool(auto and getattr(auto, "_vision", None) and auto._vision.enabled)
            if ocr_ok and api_ok:
                auto_line = "🤖 Авто-решатель капчи: <b>OCR + Vision API</b> (жмёт сам)"
            elif ocr_ok:
                auto_line = "🤖 Авто-решатель капчи: <b>OCR</b> (жмёт сам)"
            elif api_ok:
                auto_line = "🤖 Авто-решатель капчи: <b>Vision API</b> (жмёт сам)"
            else:
                auto_line = "🤖 Авто-решатель капчи: <b>выключен</b> — капча придёт вам"
        except Exception:
            auto_line = ""

        text = ("👤 <b>Аккаунты для бота поиска</b>\n"
                f"Бот поиска: {('@' + html.escape(lb)) if lb else '<i>не задан — /panel → 🔍</i>'}\n"
                f"Автопоиск: {'включён' if settings.auto_lookup else 'выключен'}\n"
                f"{auto_line}\n\n"
                "<b>Как работает:</b>\n"
                "• Трекер видит скрытого продавца → спрашивает у бота поиска, кто это.\n"
                "• <b>Отмеченные ✅ аккаунты</b> пишут боту поиска по очереди "
                "(если один упёрся в лимит/капчу — пишет следующий).\n"
                "• <b>Ничего не отмечено</b> — пишет первый рабочий аккаунт трекера "
                "(быстро, но тот же аккаунт тогда реже успевает парсить маркет).\n"
                "• <b>«Только поиск»</b> — отмеченные аккаунты НЕ парсят маркет, "
                "работают исключительно на поиск. Остальные — только парсят маркет. "
                "Это самый быстрый режим, если sessions ≥ 2.\n"
                "• ⚠️ — аккаунт сейчас не работает.\n"
                "• Добавить аккаунт: /add_session\n\n"
                "<b>Капча:</b> если включён OCR (по умолчанию) — трекер сам распознаёт слово "
                "на картинке и жмёт нужную кнопку (⭐/🦊/🐶). Если не распознал — капча "
                "придёт сюда с кнопками.\n\n"
                f"<b>Статистика поиска:</b>\n"
                f"Поисков: {lk.get('lookups', 0)} · найдено: <b>{lk.get('found', 0)}</b> "
                f"(на доверии: {lk.get('trusted_match', 0)}, "
                f"из памяти: {lk.get('from_memory', 0)})\n"
                f"Отправлено с override фильтров: "
                f"{app.pipeline.stats.get('owner_override', 0)} + "
                f"trusted: {app.pipeline.stats.get('owner_trusted_sent', 0)}\n"
                f"Не найдено: {lk.get('not_found', 0)} — из них:\n"
                f"  • funstat не вернул кандидатов: <b>{lk.get('no_candidates', 0)}</b>\n"
                f"  • funstat вернул, но NFT не виден в профиле: <b>{lk.get('nft_hidden', 0)}</b>\n"
                f"  • имя не совпало: {lk.get('name_mismatch', 0)}\n"
                f"  • скип по негативному кешу (повторный): <b>{lk.get('neg_cached', 0)}</b>\n"
                f"Без ответа: {lk.get('no_answer', 0)} · "
                f"скип (много людей): {lk.get('too_many', 0)} · "
                f"в очереди: {len(app.pipeline._lookup_tasks)} · "
                f"сейчас в капче: {app.lookup.pending_captchas()}\n"
                f"Капч: {lk.get('captcha', 0)} (авто-решено: <b>{lk.get('captcha_auto', 0)}</b>"
                + (f" ~{lk['captcha_auto_avg_sec']:.1f}s/капча" if lk.get('captcha_auto_avg_sec') else "")
                + (f", рандом: <b>{lk['captcha_random']}</b>" if lk.get('captcha_random') else "")
                + f", не смог: {lk.get('captcha_auto_fail', 0)})\n"
                f"Lookup timeouts: {lk.get('lookup_timeout', 0)}"
                + _captcha_cache_line())
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    @public.callback_query(F.data.startswith("cap:"))
    async def cb_lookup_captcha(q: CallbackQuery) -> None:
        """Админ проходит капчу бота поиска за аккаунт трекера."""
        if not is_manager(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        try:
            _, token, idx = q.data.rsplit(":", 2) if q.data.count(":") >= 2 else (None, None, None)
            res = await app.lookup.solve_captcha(token, int(idx))
        except (TypeError, ValueError):
            res = "Неизвестная кнопка"
        await q.answer(res)
        # ❌ если funstat сказал что ответ неверный (res начинается с "Нажал, но funstat сказал")
        # или если это "уже неактуальна" / "Неизвестная кнопка" — на успех не похоже; оставляем кнопки
        # на сообщении, чтобы админ мог нажать другую (капча ещё активна).
        success = res == "Нажал, продолжаю поиск"
        try:
            cap = q.message.caption if q.message.caption is not None else q.message.text
            # Отрезаем прошлые ❌/✅ строки — иначе на каждом клике caption растёт и упирается в лимит
            cap_lines = cap.split("\n\n") if cap else [""]
            base = "\n\n".join(l for l in cap_lines if not (l.startswith("✅ ") or l.startswith("❌ ")))
            icon = "✅" if success else "❌"
            new_text = f"{base}\n\n{icon} {res}"[:1024]
            kwargs = {"caption" if q.message.caption is not None else "text": new_text}
            if success:
                kwargs["reply_markup"] = None     # оставляем кнопки на неудаче
            await (q.message.edit_caption if q.message.caption is not None else q.message.edit_text)(**kwargs)
        except Exception:
            pass

    @public.callback_query(F.data.startswith("lka:"))
    async def cb_lookup_accounts(q: CallbackQuery) -> None:
        if not is_manager(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        key = q.data.split(":", 1)[1]
        if key == "~BACK":
            text, kb = _panel()
            try:
                await q.message.edit_text(text, reply_markup=kb)
            except Exception:
                pass
            await q.answer()
            return
        try:
            if key == "~DED":
                set_runtime_value("LOOKUP_DEDICATED", "false" if settings.lookup_dedicated else "true")
            elif key == "~SK":
                steps = (10, 20, 30, 50, 0)
                cur = settings.lookup_skip_over
                nxt = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else 20
                set_runtime_value("LOOKUP_SKIP_OVER", str(nxt))
            elif key == "~IV":
                steps = (3, 6, 10, 20)
                cur = settings.lookup_interval
                nxt = next((x for x in steps if x > cur), steps[0])
                set_runtime_value("LOOKUP_INTERVAL", str(nxt))
            elif key == "~TO":
                steps = _LOOKUP_TIMEOUTS
                cur = int(settings.lookup_timeout)
                nxt = next((x for x in steps if x > cur), steps[0])
                set_runtime_value("LOOKUP_TIMEOUT", str(nxt))
            else:
                if key not in app.sessions.workers and key not in settings.lookup_sessions:
                    await q.answer("Такого аккаунта нет")
                    return
                cur = list(settings.lookup_sessions)
                if key in cur:
                    cur.remove(key)
                else:
                    cur.append(key)
                set_runtime_value("LOOKUP_SESSIONS", ",".join(cur) or "-")
        except (KeyError, ValueError):
            await q.answer("Не удалось сохранить")
            return
        try:
            await app.backup.save()
        except Exception:
            pass
        text, kb = _lookup_panel()
        try:
            await q.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await q.answer("Сохранено")

    @admin.message(Command("lookup"))
    async def cmd_lookup(m: Message, command: CommandObject) -> None:
        """/lookup <имя> — вручную спросить бота поиска и показать, какие юзы достал трекер."""
        name = (command.args or "").strip()
        if not name:
            await m.answer("Пример: <code>/lookup Иван Иванов</code> — трекер напишет боту поиска "
                           "<code>/human Иван Иванов</code> и покажет, каких людей он достал из ответа.")
            return
        wait = await m.answer(f"🔍 Спрашиваю бота поиска: <code>/human {html.escape(name)}</code> …")
        session, cands, err = await app.lookup.debug(name)
        lines = [f"🔍 <b>/human {html.escape(name)}</b>"
                 + (f" (аккаунт {html.escape(session)})" if session else "")]
        if err:
            lines.append(f"❌ {html.escape(err)}")
        elif not cands:
            lines.append("Бот ответил, но людей в ответе нет.")
        else:
            lines.append(f"Найдено в ответе: {len(cands)}")
            for i, c in enumerate(cands[:30], 1):
                who = f"@{html.escape(c.username)}" if c.username else f"ID <code>{c.user_id}</code>"
                lines.append(f"{i:02d}. {who}")
            if settings.lookup_skip_over and len(cands) >= settings.lookup_skip_over:
                lines.append(f"\n⚠️ {len(cands)} ≥ {settings.lookup_skip_over} — такой лот будет пропущен")
        try:
            await wait.edit_text("\n".join(lines))
        except Exception:
            await m.answer("\n".join(lines))

    @admin.message(Command("panel"))
    async def cmd_panel(m: Message) -> None:
        text, kb = _panel()
        await m.answer(text, reply_markup=kb)

    @public.callback_query(F.data.startswith("flt:"))
    async def cb_filter(q: CallbackQuery, state: FSMContext) -> None:
        if not is_manager(q.from_user.id):
            await q.answer("Только для админов", show_alert=True)
            return
        key = q.data.split(":", 1)[1]
        if key == "REFRESH":
            # Перерисовать /panel целиком — обновятся мини-статус и состояния всех переключателей
            text, kb = _panel()
            try:
                await q.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                          disable_web_page_preview=True)
                await q.answer("Обновлено")
            except Exception as e:
                if "message is not modified" in str(e):
                    await q.answer("Без изменений")
                else:
                    await q.answer(f"Ошибка: {type(e).__name__}", show_alert=True)
            return
        if key == "LOOKUPBOT":
            cur = settings.lookup_bot_username or ""
            msg = ("🔍 <b>Бот поиска по имени</b>\n\n"
                   f"Сейчас: {('@' + cur) if cur else '<i>не задан</i>'}\n\n"
                   "Пришлите @username бота (например: <code>funstat</code> или <code>@funstat</code>) — "
                   "для лотов со скрытым продавцом трекер сам напишет ему <code>/human &lt;имя&gt;</code>, "
                   "найдёт @юз владельца (у кого этот NFT открыт в профиле) и только потом отправит лот.\n"
                   "Какие аккаунты пишут боту — /panel → «👤 Аккаунты для поиска».\n\n"
                   "Отправьте «<code>-</code>» или <code>off</code>, чтобы убрать кнопку. /cancel — отмена.")
            await state.set_state(SetLookupBot.waiting)
            await q.message.answer(msg)
            await q.answer()
            return
        if key == "LKACC":
            text, kb = _lookup_panel()
            try:
                await q.message.edit_text(text, reply_markup=kb)
            except Exception:
                await q.message.answer(text, reply_markup=kb)
            await q.answer()
            return
        if key == "SPEED":
            msg = (
                "⚡ <b>Скорость трекера — как работает и что крутить</b>\n\n"
                "<b>Как лоты попадают в канал:</b>\n"
                "1. Трекер смотрит счётчики всех коллекций "
                f"каждые <b>{settings.catalog_interval:g} сек</b> (CATALOG_INTERVAL).\n"
                "2. У коллекции изменился счётчик — читает её верх маркета. "
                f"«Горячие» коллекции (от {settings.priority_floor}⭐) читает отдельно каждые "
                f"<b>{settings.priority_poll_interval:g} сек</b> (PRIORITY_POLL_INTERVAL).\n"
                "3. Если продавец скрыт — пишет боту funstat "
                f"(пауза между запросами с одного аккаунта <b>{settings.lookup_interval:g} сек</b>, "
                f"ждёт ответа <b>{settings.lookup_timeout:g} сек</b>).\n"
                "4. funstat выдал капчу → OCR сам распознаёт слово на картинке и жмёт нужную кнопку "
                f"(ждём максимум <b>{settings.lookup_captcha_wait:g} сек</b> на fallback).\n"
                "5. Лоты старше <b>" + f"{settings.lookup_max_age}" + " сек</b> с момента обнаружения не отправляются "
                "(LOOKUP_MAX_AGE).\n\n"
                "<b>Текущие настройки (дефолт — ускоренный):</b>\n"
                f"• CATALOG_INTERVAL = <b>{settings.catalog_interval:g}</b> сек — опрос всех коллекций\n"
                f"• TRACKER_INTERVAL = <b>{settings.tracker_interval:g}</b> сек — пауза между проходами\n"
                f"• PRIORITY_POLL_INTERVAL = <b>{settings.priority_poll_interval:g}</b> сек — опрос дорогих коллекций\n"
                f"• COLD_POLL_INTERVAL = <b>{settings.cold_poll_interval:g}</b> сек — страховочный опрос пустых\n"
                f"• PAGE_LIMIT = <b>{settings.page_limit}</b> — лотов за запрос\n"
                f"• LOOKUP_INTERVAL = <b>{settings.lookup_interval:g}</b> сек — пауза в funstat\n"
                f"• LOOKUP_TIMEOUT = <b>{settings.lookup_timeout:g}</b> сек — ждать ответ funstat\n"
                f"• LOOKUP_CAPTCHA_WAIT = <b>{settings.lookup_captcha_wait:g}</b> сек — ждать капчу\n"
                f"• LOOKUP_MAX_AGE = <b>{settings.lookup_max_age}</b> сек — максимальный возраст лота\n\n"
                "<b>Хочешь ещё быстрее?</b> Выстави в env на Render:\n"
                "<code>CATALOG_INTERVAL=0.3</code>\n"
                "<code>TRACKER_INTERVAL=0.3</code>\n"
                "<code>PRIORITY_POLL_INTERVAL=1.0</code>\n"
                "<b>Минус:</b> выше шанс поймать FloodWait от Telegram → "
                "аккаунт отдыхает ~20 минут, лоты задержатся.\n\n"
                "<b>Главный ускоритель — больше аккаунтов.</b> Каждый новый аккаунт делит нагрузку.\n"
                "Поменять любую: <code>/set CATALOG_INTERVAL 0.3</code>"
            )
            try:
                await q.message.answer(msg)
            except Exception:
                pass
            await q.answer()
            return
        if key == "TRADEBOT":
            cur = settings.trade_bot_username or ""
            msg = ("💰 <b>Торговый бот для оффера</b>\n\n"
                   f"Сейчас: {('@' + cur) if cur else '<i>не задан</i>'}\n\n"
                   "Пришлите @username бота (например: <code>iostherobot</code>). "
                   "Кнопка «Написать» под постом откроет ЛС продавца с уже введённым оффером:\n"
                   "<code>@&lt;бот&gt; &lt;ссылка&gt; &lt;цена со скидкой&gt;</code>\n\n"
                   "Скидка от цены лота:\n"
                   "⭐ ≤1000 → −30 · 1001-1800 → −50 · 1801-2500 → −60 · 2501-5000 → −100 · "
                   "5001-10000 → −140 · 10001+ → −215\n"
                   "💎 TON &lt;48 → −1 · ≥48 → −2\n\n"
                   "Отправьте «<code>-</code>» или <code>off</code>, чтобы вернуть обычное «Написать». /cancel — отмена.")
            await state.set_state(SetTradeBot.waiting)
            await q.message.answer(msg)
            await q.answer()
            return
        try:
            if key == "REPEAT":
                cur = settings.seller_repeat_hours
                steps = _REPEAT_STEPS
                nxt = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else 0
                set_runtime_value("SELLER_REPEAT_HOURS", str(nxt))
            elif key == "STREAK":
                set_runtime_value("MAX_SAME_COLLECTION_STREAK", "0" if settings.max_same_collection_streak else "1")
            else:
                attr = next(a for k, a, _ in _TOGGLES if k == key)
                set_runtime_value(key, "false" if getattr(settings, attr) else "true")
        except (StopIteration, KeyError, ValueError):
            await q.answer("Неизвестная настройка")
            return
        try:
            await app.backup.save()
        except Exception:
            pass
        text, kb = _panel()
        try:
            await q.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await q.answer("Сохранено")

    async def _accept_bot_handle(m: Message, state: FSMContext, key: str,
                                 clear_msg: str, ok_msg_fmt: str) -> None:
        raw = (m.text or "").strip()
        if raw.lower() in ("/cancel", "cancel", "отмена"):
            await state.clear()
            await m.answer("Отменено.")
            return
        if raw in ("-", "off", "очистить", "clear", "0", "нет"):
            set_runtime_value(key, "")
            try:
                await app.backup.save()
            except Exception:
                pass
            await state.clear()
            await m.answer(clear_msg)
            return
        uname = raw.lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]", uname):
            await m.answer("Неверный @username. 5–32 символа: латиница, цифры, _ "
                           "(не начинается с цифры, не заканчивается на _). Попробуйте снова или /cancel.")
            return
        try:
            set_runtime_value(key, uname)
            await app.backup.save()
        except Exception as e:
            await state.clear()
            await m.answer(f"❌ Не удалось сохранить: {html.escape(str(e))}")
            return
        await state.clear()
        await m.answer(ok_msg_fmt.format(uname=uname))

    @admin.message(SetLookupBot.waiting, F.text)
    async def cmd_set_lookup_bot(m: Message, state: FSMContext) -> None:
        await _accept_bot_handle(m, state, "LOOKUP_BOT_USERNAME",
                                 "✅ Бот поиска убран — автопоиск скрытых владельцев выключен.",
                                 "✅ Бот поиска: @{uname}. Скрытых владельцев трекер теперь ищет через него сам.")

    @admin.message(SetTradeBot.waiting, F.text)
    async def cmd_set_trade_bot(m: Message, state: FSMContext) -> None:
        await _accept_bot_handle(m, state, "TRADE_BOT_USERNAME",
                                 "✅ Торговый бот убран — «Написать» откроет ЛС продавца без оффера.",
                                 "✅ Торговый бот: @{uname}. «Написать» откроет ЛС продавца с готовым оффером через @{uname}.")

    @admin.message(Command("proxies", "proxy"))
    async def cmd_proxy_group(m: Message) -> None:
        await _delete(m)                     # в сообщении мог быть пароль прокси — убираем из группы
        await m.answer("🔒 Прокси (там пароли) — только в личке с ботом.")

    @admin.message(Command("set"))
    async def cmd_set(m: Message, command: CommandObject) -> None:
        if not app.subs.ready.is_set():
            await m.answer("⏳ Бот ещё восстанавливает данные после запуска — повторите через минуту.")
            return
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2:
            await _answer_long(m, _filters_help())   # /set без аргументов — весь список
            return
        try:
            value = set_runtime_value(parts[0], parts[1])
        except KeyError:
            await m.answer(f"Неизвестный ключ. Доступно: {', '.join(RUNTIME_KEYS)}")
            return
        except (ValueError, TypeError):
            await m.answer("Некорректное значение.")
            return
        if parts[0].upper() == "GLOBAL_RPS":
            app.sessions.limiter.set_rate(settings.global_rps)
            # обновляем «базовый» RPS для adaptive_throttle, иначе шторм вернёт старое значение
            app.sessions._base_rps = float(settings.global_rps)
        if parts[0].upper() in ("COLLECTION_MIN_FLOOR", "COLLECTION_FLOOR_MODE") and not app.coordinator.paused:
            try:
                await app.coordinator.refresh_catalog()   # применить порог сразу, а не через цикл
            except Exception:
                pass
        await app.backup.save()                           # чтобы настройка пережила деплой
        shown = str(value)
        await m.answer(f"✅ {parts[0].upper()} = {html.escape(shown[:300] + ('…' if len(shown) > 300 else ''))} (сохранено)")

    @admin.message(Command("test"))
    async def cmd_test(m: Message, command: CommandObject) -> None:
        await m.answer("🧪 Запускаю проверку…")
        report, sample = await app.run_check()
        await m.answer(report)
        if sample:
            await m.answer("Пример сообщения (в канал НЕ отправлено):")
            await app.sender.send(m.chat.id, sample)
        if (command.args or "").strip() == "channel" and m.from_user.id in all_admins():
            res = await app.sender.send(settings.target_chat_id, "🧪 Тестовое сообщение трекера — бот может постить.")
            await m.answer("✅ В канал отправлено" if res.ok else f"❌ Канал: {html.escape(res.error or '')}")

    # ------------------------------------------------------------ admin: /add_session
    @users.message(or_f(Command("add_session"), F.text == BTN_ADD), F.chat.type == "private")
    async def add_start(m: Message, state: FSMContext) -> None:
        await _cleanup_pending(m.from_user.id)
        await state.clear()
        # API_ID / API_HASH берутся из .env — пользователю нужен только номер и код.
        # Свои API можно задать так: /add_session api
        if (m.text or "").strip().endswith(" api") or not (settings.api_id and settings.api_hash):
            await state.set_state(AddSession.api_id)
            await m.answer("➕ <b>Новая session</b>\n\nОтправьте <b>API ID</b> (my.telegram.org)\n"
                           "или <code>-</code>, чтобы использовать API_ID из .env.\n/cancel — отмена")
            return
        await state.update_data(api_id=settings.api_id, api_hash=settings.api_hash)
        await state.set_state(AddSession.phone)
        await m.answer("➕ <b>Добавление аккаунта</b>\n\n"
                       "Шаг 1/3. Отправьте <b>номер телефона</b> в формате +79991234567\n/cancel — отмена")

    @users.message(AddSession.api_id, F.text)
    async def add_api_id(m: Message, state: FSMContext) -> None:
        t = m.text.strip()
        if t == "-":
            if not settings.api_id:
                await m.answer("В .env нет API_ID. Отправьте число.")
                return
            api_id = settings.api_id
        elif t.isascii() and t.isdigit() and len(t) < 12:
            api_id = int(t)
        else:
            await m.answer("API ID — это число. Попробуйте ещё раз.")
            return
        await state.update_data(api_id=api_id)
        await state.set_state(AddSession.api_hash)
        await m.answer("Отправьте <b>API HASH</b> или <code>-</code> (из .env).\n"
                       "Сообщение будет сразу удалено.")

    @users.message(AddSession.api_hash, F.text)
    async def add_api_hash(m: Message, state: FSMContext) -> None:
        t = m.text.strip()
        await _delete(m)
        if t == "-":
            if not settings.api_hash:
                await m.answer("В .env нет API_HASH. Отправьте его.")
                return
            api_hash = settings.api_hash
        elif re.fullmatch(r"[0-9a-fA-F]{32}", t):
            api_hash = t
        else:
            await m.answer("API HASH — 32 hex-символа. Попробуйте ещё раз.")
            return
        await state.update_data(api_hash=api_hash)
        await state.set_state(AddSession.phone)
        await m.answer("Отправьте <b>номер телефона</b> аккаунта в формате +79991234567")

    @users.message(AddSession.phone, F.text)
    async def add_phone(m: Message, state: FSMContext) -> None:
        phone = re.sub(r"[^\d+]", "", m.text)
        await _delete(m)
        if not re.fullmatch(r"\+?\d{8,15}", phone):
            await m.answer("Некорректный номер. Пример: +79991234567")
            return
        if not app.subs.ready.is_set():
            # пока аккаунты не восстановлены из копии, имя session_00N может совпасть с ещё не
            # восстановленным аккаунтом — и тот потерялся бы
            await m.answer("⏳ Бот ещё восстанавливает аккаунты после запуска — отправьте номер через минуту.")
            return
        await _cleanup_pending(m.from_user.id)       # повторная отправка номера — старый вход закрываем
        data = await state.get_data()
        await m.answer("⏳ Подключаюсь к Telegram и запрашиваю код…")
        proxy = app.storage.pick_proxy(settings.accounts_per_proxy)   # вход сразу через прокси аккаунта

        def _mk(px: str | None) -> TelegramClient:
            # Вход идёт в памяти (StringSession): файл аккаунта появится только после успешного входа.
            # Иначе недоделанный .session подхватывали /reload и резервная копия.
            return TelegramClient(StringSession(), data["api_id"], data["api_hash"], proxy=parse_proxy(px),
                                  connection_retries=1, timeout=10,
                                  device_model="GiftTracker", system_version="1.0", app_version="1.0")

        # Вход (код) всегда напрямую — через прокси это медленно и часто висит.
        # Прокси сохраняется за аккаунтом и используется потом для работы трекера.
        client = _mk(None)
        try:
            await asyncio.wait_for(client.connect(), 15)
            sent = await asyncio.wait_for(client.send_code_request(phone), 30)
        except errors.ApiIdInvalidError:
            await client.disconnect()
            await state.clear()
            await m.answer("❌ Неверные API ID / API HASH. Начните заново: /add_session")
            return
        except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError) as e:
            await client.disconnect()
            await m.answer(f"❌ {type(e).__name__}. Отправьте другой номер или /cancel")
            return
        except errors.FloodWaitError as e:
            await client.disconnect()
            await state.clear()
            await m.answer(f"❌ Telegram просит подождать {e.seconds} сек. Попробуйте позже.")
            return
        except Exception as e:
            await client.disconnect()
            await state.clear()
            log.warning("add_session: send_code failed: %r", e)
            await m.answer(f"❌ Ошибка: {html.escape(type(e).__name__)} {html.escape(str(e)[:200])}\n"
                           "Попробуйте ещё раз: /add_session")
            return
        uid0 = m.from_user.id
        stamp = time.monotonic()
        pending[uid0] = {"client": client, "phone": phone, "ts": stamp,
                         "hash": sent.phone_code_hash, "api_id": data["api_id"],
                         "api_hash": data["api_hash"], "proxy": proxy}

        async def _expire_login() -> None:
            await asyncio.sleep(600)               # бросили вход на полпути — через 10 минут закрываем
            if pending.get(uid0, {}).get("ts") == stamp:
                await _cleanup_pending(uid0)
        _bg = asyncio.get_running_loop().create_task(_expire_login())
        _login_tasks.add(_bg)                       # держим ссылку — иначе задачу может собрать GC
        _bg.add_done_callback(_login_tasks.discard)
        await state.set_state(AddSession.code)
        await m.answer("Шаг 2/3. Отправьте <b>код входа</b>, который пришёл в Telegram.\n\n"
                       "⚠️ Вводите код <b>с пробелами или дефисами</b>, например <code>1 2 3 4 5</code> — "
                       "иначе Telegram сочтёт, что код переслали, и заблокирует вход.")

    async def _finish(m: Message, state: FSMContext) -> None:
        p = pending.pop(m.from_user.id, None)
        if p is None:                          # двойная отправка кода/пароля — второй вызов без данных
            return
        client: TelegramClient = p["client"]
        dup = None
        try:
            me = await client.get_me()
            string = client.session.save()
            # тот же аккаунт уже в трекере → вторая копия удвоила бы запросы и FloodWait
            dup = next((w.name for w in app.sessions.workers.values()
                        if me is not None and w.me_id and w.me_id == getattr(me, "id", None)), None)
            if dup:
                try:
                    await client.log_out()       # не оставлять лишний вход в «Устройствах»
                except Exception:
                    pass
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        await state.clear()
        uid = m.from_user.id
        if dup:
            await m.answer(f"ℹ️ Этот аккаунт уже добавлен как <b>{html.escape(dup)}</b>.")
            return
        taken = set(app.backup.backup_names or set()) | set(app.backup.graveyard)
        name = app.storage.next_name()
        while name in taken:                   # имя не должно совпасть с аккаунтом из копии / удалённых
            n = int(name.rsplit("_", 1)[1]) + 1
            name = f"session_{n:03d}"
        string_to_session_file(string, app.storage.session_path(name))
        p["name"] = name
        app.storage.save_meta(p["name"], p["api_id"], p["api_hash"], p["phone"], owner_id=uid,
                              proxy=p.get("proxy") or "")
        rec = SessionRecord(p["name"], app.storage.session_path(p["name"]), p["api_id"], p["api_hash"], uid,
                            p.get("proxy"))
        w = await app.sessions.add_record(rec)
        w.speed = 0.3        # новый аккаунт «прогревается»: сначала медленно, потом сам разгоняется
        log.info("Session %s added via bot (account id %s, by user %s)", p["name"], getattr(me, "id", "?"), uid)
        saved = await app.backup.save(chat_hint=m.chat.id)
        await m.answer(f"✅ Аккаунт <b>{p['name']}</b> добавлен\n"
                       f"Аккаунт ID: <code>{getattr(me, 'id', '?')}</code>\n"
                       f"Статус: {w.status.value}\n"
                       f"🌐 Прокси: {html.escape(mask_proxy(p.get('proxy')))}"
                       + ("" if p.get("proxy") else " (добавьте прокси: /proxies)") + "\n"
                       + ("💾 Сохранён — после деплоя восстановится сам, добавлять заново не нужно."
                          if saved else f"⚠️ Копию сохранить не удалось: {html.escape(app.backup.last_error or '')}"))

    @users.message(AddSession.code, F.text)
    async def add_code(m: Message, state: FSMContext) -> None:
        code = re.sub(r"\D", "", m.text)
        if not code:
            await m.answer("Код — это цифры из сообщения Telegram, например <code>1 2 3 4 5</code>. Отправьте ещё раз.")
            return
        await _delete(m)
        p = pending.get(m.from_user.id)
        if not p:
            await state.clear()
            await m.answer("Сессия входа потеряна. /add_session")
            return
        try:
            await p["client"].sign_in(phone=p["phone"], code=code, phone_code_hash=p["hash"])
        except errors.SessionPasswordNeededError:
            await state.set_state(AddSession.password)
            await m.answer("Шаг 3/3. Включена 2FA. Отправьте <b>пароль</b> (сообщение будет удалено).")
            return
        except errors.PhoneCodeInvalidError:
            await m.answer("❌ Неверный код. Отправьте ещё раз (с пробелами) или /cancel")
            return
        except errors.PhoneCodeExpiredError:
            await _cleanup_pending(m.from_user.id)
            await state.clear()
            await m.answer("❌ Код истёк (возможно, Telegram заметил пересылку кода). Начните заново: /add_session")
            return
        except Exception as e:
            await _cleanup_pending(m.from_user.id)
            await state.clear()
            await m.answer(f"❌ Ошибка входа: {html.escape(type(e).__name__)}")
            return
        await _finish(m, state)

    @users.message(AddSession.password, F.text)
    async def add_password(m: Message, state: FSMContext) -> None:
        pw = m.text
        await _delete(m)
        p = pending.get(m.from_user.id)
        if not p:
            await state.clear()
            await m.answer("Сессия входа потеряна. /add_session")
            return
        try:
            await p["client"].sign_in(password=pw)
        except errors.PasswordHashInvalidError:
            await m.answer("❌ Неверный пароль. Попробуйте ещё раз или /cancel")
            return
        except Exception as e:
            await _cleanup_pending(m.from_user.id)
            await state.clear()
            await m.answer(f"❌ Ошибка входа: {html.escape(type(e).__name__)}")
            return
        finally:
            pw = None  # noqa: F841
        await _finish(m, state)

    return [public, admin, users]
