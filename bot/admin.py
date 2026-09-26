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

from config import DATA_DIR, RUNTIME_KEYS, reload_settings, set_runtime_value, settings
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
    return user_id in all_admins() or (owner_id is not None and owner_id == user_id)


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


def _ago(ts: float | None) -> str:
    if not ts:
        return "—"
    d = int(time.time() - ts)
    if d < 60:
        return f"{d} sec ago"
    if d < 3600:
        return f"{d // 60} min ago"
    return f"{d // 3600} h ago"


async def _delete(m: Message) -> None:
    try:
        await m.delete()
    except Exception:
        pass


HELP = (
    "<b>Gift Marketplace Live Tracker</b>\n\n"
    "/status — состояние трекера\n"
    "/sessions — состояние sessions\n"
    "/add_session — добавить Telegram-аккаунт\n"
    "/remove_session — удалить аккаунт (кнопками)\n"
    "/stats — статистика листингов\n"
    "/last — последние найденные лоты и почему не отправлены\n"
    "/collections — какие коллекции отслеживаются\n"
    "/settings или /filters — все настраиваемые фильтры со значениями\n"
    "/set &lt;KEY&gt; &lt;VALUE&gt; — изменить фильтр (без аргументов — весь список)\n"
    "/pause — остановить парсинг (аккаунты не делают запросов)\n"
    "/proxies — прокси для аккаунтов (свой IP каждому)\n"
    "/resume — продолжить парсинг\n"
    "/reload — перечитать .env, шаблон, списки продавцов, новые *.session\n"
    "/test — проверка API/фильтров/БД/формата (без рассылки); /test channel — тестовый пост в канал\n"
    "/cancel — отменить текущий диалог"
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
    "MAX_ABOVE_FLOOR": "не слать лоты дороже флора коллекции больше чем на N% (0 = не проверять)",
    "ALLOW_TON_ONLY": "слать лоты, которые продаются только за TON (true/false)",
    "RUSSIAN_ONLY": "только русские продавцы (true/false)",
    "UNKNOWN_SELLER_POLICY": "продавец без явных данных: block (отсеять) | allow (пропускать)",
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
    ("👤 Фильтр продавца", ["RUSSIAN_ONLY", "UNKNOWN_SELLER_POLICY", "REQUIRE_FREE_MESSAGES",
                            "MAX_SELLER_LEVEL", "MAX_SELLER_NFTS", "SELLER_PHONE_PREFIXES"]),
    ("🔁 Без повторов", ["UNIQUE_GIFTS", "UNIQUE_SELLERS", "MAX_SAME_COLLECTION_STREAK",
                         "SELLER_REPEAT_HOURS"]),
    ("⚡ Скорость", ["CATALOG_INTERVAL", "COLD_POLL_INTERVAL", "TRACKER_INTERVAL", "PAGE_LIMIT",
                    "SESSION_RPS", "GLOBAL_RPS", "MAX_CONCURRENT_SESSIONS", "LOW_PRIORITY_POLL_INTERVAL"]),
    ("⚙️ Прочее", ["COLLECTION_FLOOR_MODE", "TEST_MODE", "ALLOW_ALL_USERS", "FETCH_FULL_SELLER"]),
    ("📢 Общий и каналы по цене", ["TARGET_CHAT_ID", "PRICE_ROUTES"]),
    ("🎨 Каналы по фону", ["BACKDROP_ROUTES"]),
]


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
            cur = html.escape(str(val))
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

    pending: dict[int, dict[str, Any]] = {}   # user_id → {client, name, phone, hash, api_id, api_hash}
    claimed: set[tuple[int, int]] = set()     # (chat_id, message_id) уже занятых лотов — атомарность «Занять»
    claim_lock = asyncio.Lock()
    global _owner_ids_getter
    _owner_ids_getter = lambda: {r.owner_id for r in app.storage.list_sessions() if r.owner_id}  # noqa: E731

    # ------------------------------------------------------------ public
    @public.message(CommandStart())
    async def cmd_start(m: Message) -> None:
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
            await q.answer(_taken_text(first.text), show_alert=True)
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
        rows = [[InlineKeyboardButton(text=f"Занят · {who}", callback_data="taken")]]
        try:
            # в канале от лота остаётся только отметка «занят» — сам лот уходит занявшему в личку
            await msg.edit_text(f"<i>Данный лот занят</i>\n{html.escape(who)}",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception:
            try:
                await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            except Exception:
                pass
        # лот — в личку тому, кто занял
        try:
            await app.bot.send_message(u.id, "Вы заняли лот:\n\n" + original,
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=links) if links else None)
            await q.answer("Лот ваш, отправил его вам в личку")
        except Exception:
            await q.answer("Лот ваш. Чтобы лоты приходили в личку, напишите боту /start", show_alert=True)

    @public.callback_query(F.data == "taken")
    async def cb_taken(q: CallbackQuery) -> None:
        kb = getattr(q.message, "reply_markup", None)
        text = kb.inline_keyboard[0][0].text if kb and kb.inline_keyboard else ""
        await q.answer(_taken_text(text), show_alert=True)

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
            app.storage.discard(p["name"])

    @users.message(or_f(Command("pause", "resume", "reload", "set", "proxies", "proxy"), F.text.in_({BTN_PAUSE, BTN_RESUME})))
    async def cmd_denied(m: Message) -> None:
        await m.answer("⛔ Эта команда доступна админу или тому, кто добавил аккаунт в трекер.\n"
                       f"Ваш Telegram ID: <code>{m.from_user.id}</code> — его можно вписать в ADMIN_IDS.")

    @users.message(or_f(Command("status"), F.text == BTN_STATUS))
    async def cmd_status(m: Message) -> None:
        c = app.sessions.counts()
        coord, pipe = app.coordinator, app.pipeline
        running = coord.mode == "LIVE" and not coord.paused
        icon = "⏸" if coord.paused else "🟢" if running else ("🟡" if coord.mode in ("INITIALIZING", "INITIAL_SYNC", "RECOVERY") else "🔴")
        cycle = coord.avg_cycle_seconds()
        text = _boot_note() + (
            f"{icon} TRACKER: {'⏸ PAUSED (/resume)' if coord.paused else ('RUNNING' if running else coord.mode)}\n"
            f"👥 Sessions: {len(app.sessions.workers)}\n"
            f"🟢 Active: {c.get('ACTIVE', 0)}\n"
            f"🔴 Errors: {c.get('ERROR', 0) + c.get('AUTH_ERROR', 0)}\n"
            f"⏳ FloodWait: {c.get('FLOOD_WAIT', 0)}\n"
            f"🟡 Connecting: {c.get('CONNECTING', 0)}\n"
            f"📚 Collections LIVE: {coord.sync_progress()[0]}/{coord.sync_progress()[1]} "
            f"(остальные в тихой синхронизации)\n"
            + (f"🐢 Из них дешёвых (флор ниже {settings.collection_min_floor}⭐, реже): {len(coord.low_prio)}\n"
               if settings.collection_floor_mode == "slow" else
               f"🚫 Пропущено коллекций (флор ниже {settings.collection_min_floor}⭐): {len(coord.skipped)}\n") +
            f"⚡ Реакция на новый лот: {f'{coord.hot_latency:.1f}s' if coord.hot_latency is not None else '—'} "
            f"(+ до {settings.catalog_interval:g}s на проверку каталога)\n"
            f"🔁 Страховочный обход: {f'{cycle:.0f}s' if cycle else '—'} (цель {settings.cold_poll_interval:g}s)\n"
            f"📦 Scanned: {coord.stats['scanned']}\n"
            f"🎁 New listings: {pipe.stats['new']}\n"
            f"📨 Sent: {pipe.stats['sent']}\n"
            f"📥 Queue: {pipe.queue.qsize()}\n"
            f"⏱ Last listing: {_ago(pipe.last_new_at)}\n"
            f"⏱ Last sent: {_ago(pipe.last_sent_at)}\n"
            f"🧪 TEST_MODE: {'ON' if settings.test_mode else 'OFF'}"
        )
        await m.answer(text)

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
        for name, w in sorted(app.sessions.workers.items())[:60]:
            line = (f"{icons.get(w.status.value, '🔴')} <b>{html.escape(name)}</b> — {w.status.value}, "
                    f"коллекций: {shards.get(name, 0)}, 🌐 {html.escape(mask_proxy(getattr(w, 'proxy', None)))}")
            if w.status.value not in ("ACTIVE", "CONNECTING"):
                line += f"\n    Reason: {html.escape(w.reason or '—')}"
            lines.append(line)
            if can_manage(uid, owners.get(name)):
                rows.append([InlineKeyboardButton(text=f"🗑 Удалить {name}", callback_data=f"rmsess:{name}")])
        if len(app.sessions.workers) > 60:
            lines.append(f"… и ещё {len(app.sessions.workers) - 60}")
        return "\n".join(lines), (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

    @users.message(or_f(Command("sessions"), F.text == BTN_SESSIONS))
    async def cmd_sessions(m: Message) -> None:
        text, kb = _sessions_view(m.from_user.id)
        await m.answer(text, reply_markup=kb)

    async def _remove_session(uid: int, name: str) -> str:
        owners = {r.name: r.owner_id for r in app.storage.list_sessions()}
        if name not in owners and name not in app.sessions.workers:
            return f"❌ Не найдена: {html.escape(name)}"
        if not can_manage(uid, owners.get(name)):
            return "⛔ Удалять можно только свои аккаунты (или если вы админ)."
        await app.sessions.remove(name)
        saved = await app.backup.save(allow_shrink=True)
        return (f"✅ {html.escape(name)} удалена" +
                ("" if saved else f"\n⚠️ Копия не обновлена: {html.escape(app.backup.last_error or '')}"))

    @users.callback_query(F.data.startswith("rmsess:"))
    async def cb_rmsess(q: CallbackQuery) -> None:
        name = q.data.split(":", 1)[1]
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Да, удалить {name}", callback_data=f"rmsess_ok:{name}"),
            InlineKeyboardButton(text="✖ Отмена", callback_data="rmsess_no")]])
        await q.message.answer(f"Удалить {html.escape(name)}? Аккаунт перестанет использоваться трекером.",
                               reply_markup=kb)
        await q.answer()

    @users.callback_query(F.data.startswith("rmsess_ok:"))
    async def cb_rmsess_ok(q: CallbackQuery) -> None:
        text = await _remove_session(q.from_user.id, q.data.split(":", 1)[1])
        await q.message.edit_text(text)
        await q.answer()

    @users.callback_query(F.data == "rmsess_no")
    async def cb_rmsess_no(q: CallbackQuery) -> None:
        await q.message.edit_text("Отменено.")
        await q.answer()

    @users.message(Command("remove_session"))
    async def cmd_remove(m: Message, command: CommandObject) -> None:
        name = (command.args or "").strip()
        if not re.fullmatch(r"[\w\-]+", name or ""):
            text, kb = _sessions_view(m.from_user.id)
            await m.answer("Выберите аккаунт для удаления:\n\n" + text, reply_markup=kb)
            return
        await m.answer(await _remove_session(m.from_user.id, name))

    @admin.message(or_f(Command("pause"), F.text == BTN_PAUSE))
    async def cmd_pause(m: Message) -> None:
        await app.coordinator.set_paused(True)
        await app.backup.save()          # чтобы пауза сохранилась и после деплоя
        await m.answer("⏸ Парсинг остановлен: трекер не делает запросов к Telegram, аккаунты отдыхают.\n"
                       "Продолжить: /resume")

    @admin.message(or_f(Command("resume"), F.text == BTN_RESUME))
    async def cmd_resume(m: Message) -> None:
        await app.coordinator.set_paused(False)
        await app.backup.save()
        await m.answer("▶️ Парсинг продолжен.")

    @admin.message(Command("proxies"))
    async def cmd_proxies(m: Message, command: CommandObject) -> None:
        """/proxies — список; /proxies + прокси с новой строки — заменить пул и раздать аккаунтам."""
        arg = (command.args or "").strip().lower()
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

    @admin.message(Command("proxy"))
    async def cmd_proxy(m: Message, command: CommandObject) -> None:
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
        app.storage.set_proxy(name, proxy)
        ok = await app.sessions.workers[name].change_proxy(proxy)
        await app.backup.save()
        await m.answer(f"{'✅' if ok else '⚠️'} {html.escape(name)}: {html.escape(mask_proxy(proxy))}"
                       + ("" if ok else f"\nНе подключился: {html.escape(app.sessions.workers[name].reason or '')}"))

    @admin.message(Command("reload"))
    async def cmd_reload(m: Message) -> None:
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
        for i in range(0, len(text), 3900):          # лимит длины сообщения Telegram
            await m.answer(text[i:i + 3900])

    @users.message(or_f(Command("last"), F.text == BTN_LAST))
    async def cmd_last(m: Message) -> None:
        """Последние найденные новые лоты и почему они (не) отправлены."""
        rows = await app.db.recent_listings(15)
        if not rows:
            await m.answer("Новых лотов пока не найдено. Проверьте /status — сколько коллекций уже в LIVE.")
            return
        icons = {"SENT": "✅", "FILTERED_PRICE": "💲", "FILTERED_SELLER": "👤", "FILTERED_DUPLICATE": "🔁",
                 "SEND_FAILED": "❌", "NEW": "⏳", "TEST_ONLY": "🧪", "ERROR": "⚠️"}
        lines = ["<b>Последние новые лоты</b> (✅ отправлен, 💲 цена, 👤 продавец, 🔁 повтор):", ""]
        for r in rows:
            price = f"{r['stars_price']}⭐" if r["stars_price"] is not None else "TON"
            reason = r["reason"] or ""
            if r["status"] == "FILTERED_SELLER" and r["seller_filter_result"]:
                reason = r["seller_filter_result"]
            lines.append(f"{icons.get(r['status'], '•')} {html.escape(r['gift_name'] or '?')} #{r['gift_num'] or ''} "
                         f"— {price}" + (f" — {html.escape(reason[:60])}" if r["status"] != "SENT" else ""))
        await m.answer("\n".join(lines))

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
        lines += [f"{k}: {v}" for k, v in sorted(counts.items())] or ["—"]
        rej = await app.db.seller_reject_counts()
        if rej:
            names = {"PAID_MESSAGES": "платные сообщения / только Premium", "TOO_MANY_NFTS": "много NFT",
                     "LEVEL_TOO_HIGH": "Level выше лимита",
                     "UNKNOWN_BLOCKED": "не русский (нет данных)", "PHONE_MISMATCH": "номер не +7",
                     "BLACKLIST": "blacklist"}
            lines += ["", "<b>Почему отсеяны продавцы:</b>"]
            lines += [f"{names.get(k, k)}: {v}" for k, v in sorted(rej.items(), key=lambda x: -x[1])]
        lines += ["", "<b>За 24 часа:</b>"]
        lines += [f"{k}: {v}" for k, v in sorted(day.items())] or ["—"]
        await m.answer("\n".join(lines))

    @users.message(or_f(Command("settings"), Command("filters")))
    async def cmd_settings(m: Message) -> None:
        await m.answer(_filters_help())

    @admin.message(Command("set"))
    async def cmd_set(m: Message, command: CommandObject) -> None:
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2:
            await m.answer(_filters_help())          # /set без аргументов — весь список
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
        if parts[0].upper() in ("COLLECTION_MIN_FLOOR", "COLLECTION_FLOOR_MODE"):
            try:
                await app.coordinator.refresh_catalog()   # применить порог сразу, а не через цикл
            except Exception:
                pass
        await app.backup.save()                           # чтобы настройка пережила деплой
        await m.answer(f"✅ {parts[0].upper()} = {html.escape(str(value))} (сохранено)")

    @users.message(Command("test"))
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
        elif t.isdigit():
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
        data = await state.get_data()
        await m.answer("⏳ Подключаюсь к Telegram и запрашиваю код…")
        name = app.storage.next_name()
        proxy = app.storage.pick_proxy(settings.accounts_per_proxy)   # вход сразу через прокси аккаунта

        def _mk(px: str | None) -> TelegramClient:
            return TelegramClient(str(app.storage.session_path(name).with_suffix("")),
                                  data["api_id"], data["api_hash"], proxy=parse_proxy(px),
                                  connection_retries=1, timeout=10,
                                  device_model="GiftTracker", system_version="1.0", app_version="1.0")

        # Вход (код) всегда напрямую — через прокси это медленно и часто висит.
        # Прокси сохраняется за аккаунтом и используется потом для работы трекера.
        client = _mk(None)
        try:
            await asyncio.wait_for(client.connect(), 15)
            sent = await asyncio.wait_for(client.send_code_request(phone), 30)
        except errors.ApiIdInvalidError:
            await client.disconnect(); app.storage.discard(name)
            await state.clear()
            await m.answer("❌ Неверные API ID / API HASH. Начните заново: /add_session")
            return
        except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError) as e:
            await client.disconnect(); app.storage.discard(name)
            await m.answer(f"❌ {type(e).__name__}. Отправьте другой номер или /cancel")
            return
        except errors.FloodWaitError as e:
            await client.disconnect(); app.storage.discard(name)
            await state.clear()
            await m.answer(f"❌ Telegram просит подождать {e.seconds} сек. Попробуйте позже.")
            return
        except Exception as e:
            await client.disconnect(); app.storage.discard(name)
            await state.clear()
            log.warning("add_session: send_code failed: %r", e)
            await m.answer(f"❌ Ошибка: {html.escape(type(e).__name__)} {html.escape(str(e)[:200])}\n"
                           "Попробуйте ещё раз: /add_session")
            return
        pending[m.from_user.id] = {"client": client, "name": name, "phone": phone,
                                   "hash": sent.phone_code_hash, "api_id": data["api_id"],
                                   "api_hash": data["api_hash"], "proxy": proxy}
        await state.set_state(AddSession.code)
        await m.answer("Шаг 2/3. Отправьте <b>код входа</b>, который пришёл в Telegram.\n\n"
                       "⚠️ Вводите код <b>с пробелами или дефисами</b>, например <code>1 2 3 4 5</code> — "
                       "иначе Telegram сочтёт, что код переслали, и заблокирует вход.")

    async def _finish(m: Message, state: FSMContext) -> None:
        p = pending.pop(m.from_user.id, None)
        if p is None:                          # двойная отправка кода/пароля — второй вызов без данных
            return
        client: TelegramClient = p["client"]
        me = await client.get_me()
        await client.disconnect()
        uid = m.from_user.id
        app.storage.save_meta(p["name"], p["api_id"], p["api_hash"], p["phone"], owner_id=uid,
                              proxy=p.get("proxy") or "")
        await state.clear()
        if not all_admins():
            save_admins({uid})           # первый, кто добавил аккаунт, становится админом
        rec = SessionRecord(p["name"], app.storage.session_path(p["name"]), p["api_id"], p["api_hash"], uid,
                            p.get("proxy"))
        w = await app.sessions.add_record(rec)
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
