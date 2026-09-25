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

import html
import json
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient, errors

from config import DATA_DIR, RUNTIME_KEYS, reload_settings, set_runtime_value, settings
from sessions.storage import SessionRecord
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


class IsAdmin(Filter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in all_admins())


def is_allowed(user_id: int | None) -> bool:
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
    "/settings — текущие настройки\n"
    "/set &lt;KEY&gt; &lt;VALUE&gt; — изменить настройку на лету\n"
    "/pause — остановить парсинг (аккаунты не делают запросов)\n"
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
    "/settings — текущие настройки\n"
    "/test — проверка работы (пример сообщения придёт вам)\n"
    "/cancel — отменить текущий диалог"
)


# ---------------------------------------------------------------------------- routers
def build_routers(app: "App") -> list[Router]:
    public = Router(name="public")
    admin = Router(name="admin")        # только админы: /remove_session /reload /set
    admin.message.filter(IsAdmin())
    users = Router(name="users")        # все пользователи, если ALLOW_ALL_USERS=true
    users.message.filter(IsAllowed())

    pending: dict[int, dict[str, Any]] = {}   # user_id → {client, name, phone, hash, api_id, api_hash}

    # ------------------------------------------------------------ public
    @public.message(CommandStart())
    async def cmd_start(m: Message) -> None:
        if m.from_user and is_allowed(m.from_user.id):
            await m.answer((HELP if m.from_user.id in all_admins() else USER_HELP)
                           + f"\n\nВаш Telegram ID: <code>{m.from_user.id}</code>")
            if not app.sessions.usable():
                await m.answer("⚠️ Нет ни одного авторизованного аккаунта — трекер не работает.\n"
                               "Нажмите /add_session: нужен только номер телефона и код.")
        else:
            await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id if m.from_user else '?'}</code>\n"
                           "Доступ только для администраторов.")

    @public.callback_query(F.data.startswith("take:"))
    async def cb_take(q: CallbackQuery) -> None:
        """Кнопка «✓ Занять» под постом: первый нажавший забирает лот."""
        msg = q.message
        kb = getattr(msg, "reply_markup", None)
        if not kb or not kb.inline_keyboard:
            await q.answer()
            return
        first = kb.inline_keyboard[0][0]
        if not first.text.startswith("✓"):
            await q.answer(f"Лот уже {first.text.lstrip('✅ ').lower()}", show_alert=True)
            return
        u = q.from_user
        who = f"@{u.username}" if u.username else (u.full_name or str(u.id))
        rows = [[InlineKeyboardButton(text=f"✅ Занял {who}", callback_data="taken")]] + kb.inline_keyboard[1:]
        try:
            await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            pass
        await q.answer("Лот ваш ✅")

    @public.callback_query(F.data == "taken")
    async def cb_taken(q: CallbackQuery) -> None:
        await q.answer("Лот уже занят", show_alert=True)

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

    @users.message(Command("status"))
    async def cmd_status(m: Message) -> None:
        c = app.sessions.counts()
        coord, pipe = app.coordinator, app.pipeline
        running = coord.mode == "LIVE" and not coord.paused
        icon = "🟢" if running else ("🟡" if coord.mode in ("INITIALIZING", "INITIAL_SYNC", "RECOVERY") else "🔴")
        cycle = coord.avg_cycle_seconds()
        text = (
            f"{icon} TRACKER: {'⏸ PAUSED (/resume)' if coord.paused else ('RUNNING' if running else coord.mode)}\n"
            f"👥 Sessions: {len(app.sessions.workers)}\n"
            f"🟢 Active: {c.get('ACTIVE', 0)}\n"
            f"🔴 Errors: {c.get('ERROR', 0) + c.get('AUTH_ERROR', 0)}\n"
            f"⏳ FloodWait: {c.get('FLOOD_WAIT', 0)}\n"
            f"🟡 Connecting: {c.get('CONNECTING', 0)}\n"
            f"📚 Collections LIVE: {coord.sync_progress()[0]}/{len(coord.states)} "
            f"(остальные в тихой синхронизации)\n"
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

    def _sessions_view(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
        c = app.sessions.counts()
        lines = [
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
                    f"коллекций: {shards.get(name, 0)}")
            if w.status.value not in ("ACTIVE", "CONNECTING"):
                line += f"\n    Reason: {html.escape(w.reason or '—')}"
            lines.append(line)
            if can_manage(uid, owners.get(name)):
                rows.append([InlineKeyboardButton(text=f"🗑 Удалить {name}", callback_data=f"rmsess:{name}")])
        if len(app.sessions.workers) > 60:
            lines.append(f"… и ещё {len(app.sessions.workers) - 60}")
        return "\n".join(lines), (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

    @users.message(Command("sessions"))
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
        saved = await app.backup.save()
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

    @admin.message(Command("pause"))
    async def cmd_pause(m: Message) -> None:
        await app.coordinator.set_paused(True)
        await app.backup.save()          # чтобы пауза сохранилась и после деплоя
        await m.answer("⏸ Парсинг остановлен: трекер не делает запросов к Telegram, аккаунты отдыхают.\n"
                       "Продолжить: /resume")

    @admin.message(Command("resume"))
    async def cmd_resume(m: Message) -> None:
        await app.coordinator.set_paused(False)
        await app.backup.save()
        await m.answer("▶️ Парсинг продолжен.")

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

    @users.message(Command("stats"))
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

    @users.message(Command("settings"))
    async def cmd_settings(m: Message) -> None:
        v = settings.public_view()
        keys = ["min_stars", "min_ton", "allow_ton_only", "russian_only", "unknown_seller_policy",
                "seller_phone_prefixes", "initial_sync", "tracker_interval", "page_limit",
                "max_concurrent_sessions", "session_rps", "global_rps", "catalog_interval",
                "collections", "fetch_full_seller", "test_mode", "target_chat_id", "api_hash", "bot_token"]
        body = "\n".join(f"{k.upper()} = {html.escape(str(v[k]))}" for k in keys)
        await m.answer(f"<b>⚙️ Настройки</b>\n<pre>{body}</pre>\n"
                       f"Изменить: /set KEY VALUE\nДоступно: {', '.join(RUNTIME_KEYS)}")

    @admin.message(Command("set"))
    async def cmd_set(m: Message, command: CommandObject) -> None:
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2:
            await m.answer("Использование: /set MIN_STARS 5000")
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
    @users.message(Command("add_session"), F.chat.type == "private")
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
        name = app.storage.next_name()
        client = TelegramClient(str(app.storage.session_path(name).with_suffix("")),
                                data["api_id"], data["api_hash"],
                                device_model="GiftTracker", system_version="1.0", app_version="1.0")
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
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
            await m.answer(f"❌ Ошибка: {html.escape(type(e).__name__)}")
            return
        pending[m.from_user.id] = {"client": client, "name": name, "phone": phone,
                                   "hash": sent.phone_code_hash, "api_id": data["api_id"],
                                   "api_hash": data["api_hash"]}
        await state.set_state(AddSession.code)
        await m.answer("Шаг 2/3. Отправьте <b>код входа</b>, который пришёл в Telegram.\n\n"
                       "⚠️ Вводите код <b>с пробелами или дефисами</b>, например <code>1 2 3 4 5</code> — "
                       "иначе Telegram сочтёт, что код переслали, и заблокирует вход.")

    async def _finish(m: Message, state: FSMContext) -> None:
        p = pending.pop(m.from_user.id)
        client: TelegramClient = p["client"]
        me = await client.get_me()
        await client.disconnect()
        uid = m.from_user.id
        app.storage.save_meta(p["name"], p["api_id"], p["api_hash"], p["phone"], owner_id=uid)
        await state.clear()
        if not all_admins():
            save_admins({uid})           # первый, кто добавил аккаунт, становится админом
        rec = SessionRecord(p["name"], app.storage.session_path(p["name"]), p["api_id"], p["api_hash"], uid)
        w = await app.sessions.add_record(rec)
        log.info("Session %s added via bot (account id %s, by user %s)", p["name"], getattr(me, "id", "?"), uid)
        saved = await app.backup.save(chat_hint=m.chat.id)
        await m.answer(f"✅ Аккаунт <b>{p['name']}</b> добавлен\n"
                       f"Аккаунт ID: <code>{getattr(me, 'id', '?')}</code>\n"
                       f"Статус: {w.status.value}\n"
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
