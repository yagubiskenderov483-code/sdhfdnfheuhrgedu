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


_REJ = {"PAID_MESSAGES": "платные сообщения / только Premium", "TOO_MANY_NFTS": "много NFT",
        "LEVEL_TOO_HIGH": "Level выше лимита", "UNKNOWN_BLOCKED": "нет признаков русского",
        "PHONE_MISMATCH": "номер не +7/+375/+380", "FOREIGN_TEXT": "профиль на чужом языке",
        "LOOKUP_FAILED": "профиль не прочитался (аккаунты заняты)", "BLACKLIST": "blacklist",
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
    "/top — самые активные коллекции за сутки · /find имя — всё о коллекции\n"
    "/mute имя · /unmute имя — выключить / включить лоты коллекции\n"
    "/collections — какие коллекции отслеживаются\n\n"
    "<b>Управление</b>\n"
    "/off · /on — выключить / включить парсер\n"
    "/close · /open — закрыть / открыть для пользователей (лоты идут)\n"
    "/settings — все настройки · /set КЛЮЧ ЗНАЧЕНИЕ — изменить\n\n"
    "<b>Аккаунты</b>\n"
    "/sessions — список, пинг, риск · /add_session — добавить\n"
    "/removed — удалённые за 14 дней · /revive ИМЯ|all — вернуть\n"
    "/proxies — прокси для аккаунтов\n\n"
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
    ("👤 Фильтр продавца", ["RUSSIAN_ONLY", "UNKNOWN_SELLER_POLICY", "ALLOW_HIDDEN_OWNER",
                            "REQUIRE_FREE_MESSAGES", "MAX_SELLER_LEVEL", "MAX_SELLER_NFTS", "SELLER_PHONE_PREFIXES"]),
    ("🔁 Без повторов", ["UNIQUE_GIFTS", "UNIQUE_SELLERS", "MAX_SAME_COLLECTION_STREAK",
                         "SELLER_REPEAT_HOURS"]),
    ("⚡ Скорость", ["PRIORITY_FLOOR", "PRIORITY_SHARE", "PRIORITY_POLL_INTERVAL", "CATALOG_INTERVAL", "COLD_POLL_INTERVAL", "TRACKER_INTERVAL", "PAGE_LIMIT",
                    "SESSION_RPS", "GLOBAL_RPS", "MAX_CONCURRENT_SESSIONS", "LOW_PRIORITY_POLL_INTERVAL", "SYNC_MODE"]),
    ("⚙️ Прочее", ["COLLECTION_FLOOR_MODE", "TEST_MODE", "ALLOW_ALL_USERS", "FETCH_FULL_SELLER"]),
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

    @users.message(or_f(Command("pause", "resume", "on", "off", "close", "open", "reload", "set", "proxies", "proxy", "removed", "revive", "top", "find", "mute", "unmute", "why", "lot", "panel"),
                        F.text.in_({BTN_PAUSE, BTN_RESUME})))
    async def cmd_denied(m: Message) -> None:
        await m.answer("⛔ Эта команда доступна админу или тому, кто добавил аккаунт в трекер.\n"
                       f"Ваш Telegram ID: <code>{m.from_user.id}</code> — его можно вписать в ADMIN_IDS.")

    @users.message(or_f(Command("status"), F.text == BTN_STATUS))
    async def cmd_status(m: Message) -> None:
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
        lines += [
            "", "<b>🎁 Лоты</b>",
            f"Найдено: {pipe.stats['new']} · отправлено: {pipe.stats['sent']} · в очереди: {pipe.queue.qsize()}",
            f"Последний найден: {_ago(pipe.last_new_at)} · отправлен: {_ago(pipe.last_sent_at)}",
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
        text = "\n".join(lines)
        await _answer_long(m, text)

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
        return "\n".join(lines), (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

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

    @admin.message(Command("reload"))
    async def cmd_reload(m: Message) -> None:
        if app.booting:
            # во время STARTUP_DELAY старая копия бота ещё может работать: подключать аккаунты рано
            await m.answer("⏳ Идёт запуск — повторите через минуту.")
            return
        reload_settings()
        app.sessions.limiter.set_rate(settings.global_rps)
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

    @users.message(or_f(Command("stats"), F.text == BTN_STATS))
    async def cmd_stats(m: Message) -> None:
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
        await _answer_long(m, "\n".join(lines))

    @users.message(or_f(Command("settings"), Command("filters")))
    async def cmd_settings(m: Message) -> None:
        await _answer_long(m, _filters_help())

    # ---------------- /panel: фильтры кнопками (без набора /set)
    _TOGGLES = (("RUSSIAN_ONLY", "russian_only", "Только русские продавцы"),
                ("SELLER_NAME_SIGNAL", "seller_name_signal", "Имя кириллицей = русский"),
                ("PHONE_MISMATCH_BLOCKS", "phone_mismatch_blocks", "Чужой телефон — отсеять"),
                ("UNIQUE_SELLERS", "unique_sellers", "Продавец не повторяется"),
                ("UNIQUE_GIFTS", "unique_gifts", "NFT не повторяется"),
                ("ALLOW_TON_ONLY", "allow_ton_only", "Лоты только за TON"))
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
        text = ("🎛 <b>Фильтры лотов</b>\nНажмите, чтобы переключить — применяется сразу и переживает деплой.\n"
                f"Мин. цена: {settings.min_stars}⭐ · флор коллекции от {settings.collection_min_floor}⭐ "
                "(менять: /set MIN_STARS …)\nПочему лоты отсеиваются: /why")
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

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
        if key == "LOOKUPBOT":
            cur = settings.lookup_bot_username or ""
            msg = ("🔍 <b>Бот поиска по имени</b>\n\n"
                   f"Сейчас: {('@' + cur) if cur else '<i>не задан</i>'}\n\n"
                   "Пришлите @username бота (например: <code>funstat</code> или <code>@funstat</code>) — "
                   "в постах со скрытым продавцом появится кнопка «🔍 Клик», открывающая этого бота "
                   "с уже введённой командой <code>/human &lt;имя&gt;</code>.\n\n"
                   "Отправьте «<code>-</code>» или <code>off</code>, чтобы убрать кнопку. /cancel — отмена.")
            await state.set_state(SetLookupBot.waiting)
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

    @admin.message(SetLookupBot.waiting, F.text)
    async def cmd_set_lookup_bot(m: Message, state: FSMContext) -> None:
        raw = (m.text or "").strip()
        if raw.lower() in ("/cancel", "cancel", "отмена"):
            await state.clear()
            await m.answer("Отменено.")
            return
        if raw in ("-", "off", "очистить", "clear", "0", "нет"):
            set_runtime_value("LOOKUP_BOT_USERNAME", "")
            try:
                await app.backup.save()
            except Exception:
                pass
            await state.clear()
            await m.answer("✅ Бот поиска убран — кнопка «🔍 Клик» больше не будет появляться.")
            return
        uname = raw.lstrip("@").strip()
        # @username Telegram: 5–32 символа, [A-Za-z0-9_], не начинается с цифры, не оканчивается на _
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]", uname):
            await m.answer("Неверный @username. Должно быть 5–32 символа: латиница, цифры, _ "
                           "(не начинается с цифры, не заканчивается на _). Попробуйте снова или /cancel.")
            return
        try:
            set_runtime_value("LOOKUP_BOT_USERNAME", uname)
            await app.backup.save()
        except Exception as e:
            await state.clear()
            await m.answer(f"❌ Не удалось сохранить: {html.escape(str(e))}")
            return
        await state.clear()
        await m.answer(f"✅ Бот поиска: @{uname}. В постах со скрытым продавцом появится кнопка «🔍 Клик».")

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
