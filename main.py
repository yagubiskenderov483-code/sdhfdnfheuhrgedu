"""
Telegram Gift Marketplace — Live Tracker новых листингов.

Запуск:
    python main.py            — обычный режим
    python main.py --check    — диагностика (sessions, Marketplace API, парсинг, фильтры, БД, бот)
                                без рассылки; печатает пример сообщения
"""
from __future__ import annotations

import argparse
import asyncio
import os
import html
import json
import logging
import re
import signal
import time
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.admin import all_admins, build_routers, init_claim_code, is_manager, save_admins
from bot.subscription import Subscriptions, build_sub_router
from bot.formatter import MessageFormatter
from bot.sender import TelegramSender
from config import settings
from database.db import Database
from filters.price_filter import PriceFilter
from filters.seller_filter import SellerFilter
from sessions.backup import SessionBackup
from sessions.manager import SessionManager
from sessions.storage import SessionStorage
from tracker.coordinator import MarketplaceCoordinator
from tracker.marketplace import fetch_catalog, fetch_resale_page
from tracker.queue import ListingPipeline, build_listing
from utils.logger import get_logger, register_secret, setup_logging

log = get_logger("main")


_TAG_RE = re.compile(r"<[^>]+>")


async def _html_fallback(make_request, bot, method):
    """Если Telegram не принял HTML-разметку сообщения (can't parse entities), повторяем без неё,
    чтобы команда всё равно получила ответ, а не падала молча."""
    try:
        return await make_request(bot, method)
    except TelegramBadRequest as e:
        if "can't parse entities" not in str(e) or not isinstance(getattr(method, "text", None), str):
            raise
        log.warning("HTML parse error, resending as plain text: %s", e)
        method.text = html.unescape(_TAG_RE.sub("", method.text))
        method.parse_mode = None
        return await make_request(bot, method)


_WHY_RU = (
    ("no Russian bio / phone / whitelist", "продавец не русский (нет русского био/телефона) — RUSSIAN_ONLY"),
    ("profile text is in a foreign language", "профиль продавца на другом языке — RUSSIAN_ONLY"),
    ("visible phone prefix does not match", "телефон продавца не той страны"),
    ("seller profile unavailable", "профиль продавца не прочитался (аккаунты заняты)"),
    ("seller in blacklist", "продавец в чёрном списке"),
    ("same collection", "та же коллекция подряд"),
    ("this gift was already sent", "этот NFT уже отправляли"),
    ("this seller was already sent", "этот продавец уже был (UNIQUE_SELLERS, SELLER_REPEAT_HOURS)"),
    ("this seller is being sent", "этот продавец отправляется прямо сейчас"),
    ("stale", "устарел, пока проверялся"),
    ("TON-only listing", "лот только за TON (ALLOW_TON_ONLY=false)"),
    ("messages:", "у продавца платные сообщения / только Premium"),
    ("level N >", "уровень продавца выше MAX_SELLER_LEVEL"),
    ("N NFT >", "у продавца больше NFT, чем MAX_SELLER_NFTS"),
)


class _ConflictWatch(logging.Handler):
    """aiogram пишет «Failed to fetch updates - TelegramConflictError», когда тот же токен бота
    запущен где-то ещё (токен утёк / второй деплой). Запоминаем время — сторож предупредит админов."""
    def __init__(self):
        super().__init__(logging.ERROR)
        self.last: float = 0.0
        self.first: float = 0.0
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if "TelegramConflictError" in record.getMessage():
                now = time.time()
                if now - self.last > 120:
                    self.first, self.count = now, 0       # новая серия (старая давно кончилась)
                self.last, self.count = now, self.count + 1
        except Exception:
            pass


CONFLICT_WATCH = _ConflictWatch()
logging.getLogger("aiogram.dispatcher").addHandler(CONFLICT_WATCH)


class App:
    def __init__(self) -> None:
        self.db = Database(settings.path(settings.database_path))
        self.storage = SessionStorage(settings.path(settings.sessions_dir), settings.api_id, settings.api_hash)
        self.sessions = SessionManager(self.storage)
        self.bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
        self.bot.session.middleware(_html_fallback)   # битая HTML-разметка → отправить текстом, а не молчать
        self.sender = TelegramSender(self.bot)
        self.backup = SessionBackup(self.bot, self.storage, all_admins, save_admins,
                                    history_export=lambda: self.db.export_sent_history(),
                                    history_import=lambda rows: self.db.import_sent_history(rows))
        self.backup.market_export = self.db.export_market_snapshot
        self.backup.market_import = self.db.import_market_snapshot
        self.backup.paused_getter = lambda: self.coordinator.paused
        self.sessions.on_auth_error = self._notify_auth_error
        self.formatter = MessageFormatter(settings.path(settings.template_path))
        self.price_filter = PriceFilter()
        self.seller_filter = SellerFilter(settings.path("data"))
        self.pipeline = ListingPipeline(self.db, self.sessions, self.sender, self.formatter,
                                        self.price_filter, self.seller_filter)
        self.coordinator = MarketplaceCoordinator(self.db, self.sessions, self.pipeline)
        self.pipeline.paused_getter = lambda: self.coordinator.paused
        self.pipeline.floor_getter = lambda gid: getattr(self.coordinator.states.get(gid), "floor_stars", None)
        # коллекции, выключенные админом через /mute (хранятся в резервной копии)
        self.pipeline.muted_getter = lambda: set(self.backup.flags.get("muted") or [])
        self.dp = Dispatcher(storage=MemoryStorage())
        self._alerts: dict[str, float] = {}
        self._bad_since: dict[str, float] = {}
        self._started_at = time.time()
        self.subs = Subscriptions(self)
        sub_router, sub_tail = build_sub_router(self, self.subs, is_manager)
        self.sub_welcome = sub_router.welcome
        self.subs.on_change = lambda: self.backup.save()
        self.backup.subs_export = self.subs.export
        self.backup.subs_import = self.subs.import_
        self.dp.include_router(sub_router)
        for r in build_routers(self):
            self.dp.include_router(r)
        self.dp.include_router(sub_tail)      # последним: всё прочее от не-админов → приветствие
        self._tasks: list[asyncio.Task] = []
        self.booting = True          # аккаунты ещё подключаются после запуска
        self.bench: dict | None = None
        self.users_closed = False    # /close: пользователям «Парсер закрыт», а лоты идут

    # ------------------------------------------------------------------ diagnostics
    async def run_check(self) -> tuple[str, str | None]:
        """Проверка всей цепочки без рассылки. → (отчёт HTML, пример сообщения или None)"""
        lines = ["<b>🧪 Проверка</b>"]
        sample = None
        c = self.sessions.counts()
        lines.append(f"Sessions: {len(self.sessions.workers)} (active {c.get('ACTIVE', 0)})"
                     + (" ✅" if c.get("ACTIVE") else " ❌"))
        try:
            await self.db.kv_get("schema_version")
            lines.append("Database: ✅")
        except Exception as e:
            lines.append(f"Database: ❌ {html.escape(str(e))}")
        try:
            me = await self.bot.get_me()
            lines.append(f"Bot: ✅ @{me.username}")
        except Exception as e:
            lines.append(f"Bot: ❌ {html.escape(type(e).__name__)}")
        try:
            chat = await self.bot.get_chat(settings.target_chat_id)
            lines.append(f"Target chat: ✅ {html.escape(chat.title or str(chat.id))}")
        except Exception as e:
            lines.append(f"Target chat: ❌ {html.escape(str(e))[:200]}")

        if not self.sessions.usable():
            lines.append("Marketplace API: ⏭ нет активной session")
            return "\n".join(lines), None
        try:
            catalog, sname = await self.sessions.run(fetch_catalog, timeout=30)
            lines.append(f"payments.getStarGifts: ✅ {len(catalog)} коллекций с маркетом (via {sname})")
            top = max(catalog, key=lambda x: x.availability_resale or 0)
            page, sname = await self.sessions.run(lambda cl: fetch_resale_page(cl, top.gift_id, "", 5), timeout=30)
            lines.append(f"payments.getResaleStarGifts: ✅ «{html.escape(top.title)}» — "
                         f"{page.count} лотов, получено {len(page.items)}")
            if page.items:
                it = page.items[0]
                l = build_listing(it, sname)
                l.listing_id = "TEST:" + l.listing_id
                await self.pipeline._enrich_seller(l)
                pr = self.price_filter.check(l)
                sr = self.seller_filter.check(l)
                l.seller_filter_result = sr.code
                lines.append(f"Parsed: {html.escape(l.gift_name)} #{l.gift_num} | "
                             f"{l.stars_price} Stars / {l.ton_price} TON | model {html.escape(str(l.model))}")
                lines.append(f"Seller: {html.escape(l.seller_key)} @{html.escape(str(l.seller_username))} "
                             f"level={l.level} premium={l.premium} msgs={html.escape(str(l.messages))}")
                lines.append(f"PriceFilter: {'✅' if pr.passed else '❌'} {html.escape(pr.reason)}")
                lines.append(f"SellerFilter: {'✅' if sr.passed else '❌'} {sr.code}")
                sample = self.formatter.render(l)
        except Exception as e:
            lines.append(f"Marketplace API: ❌ {html.escape(type(e).__name__)}: {html.escape(str(e))[:300]}")
        return "\n".join(lines), sample

    # ------------------------------------------------------------------ lifecycle
    async def start(self, run_bot: bool = True) -> None:
        await self.db.connect()
        # сменили бота (новый токен) → новая группа и темы берутся из .env, а не из старых /set
        bot_id = settings.bot_token.split(":", 1)[0]
        prev = await self.db.kv_get("bot_id")
        if prev and prev != bot_id:
            from config import drop_runtime_overrides
            gone = drop_runtime_overrides(["TARGET_CHAT_ID", "PRICE_ROUTES", "BACKDROP_ROUTES", "SUB_CHAT_ID"])
            if gone:
                log.info("New bot token — using channel/topic routes from .env (dropped /set: %s)", ", ".join(gone))
        await self.db.kv_set("bot_id", bot_id)
        # 1) бот отвечает на команды СРАЗУ, ещё до долгого запуска трекера
        if run_bot:
            init_claim_code()
            await self._drop_webhook("startup")
            self._spawn(asyncio.create_task(
                self.dp.start_polling(self.bot, handle_signals=False,
                                      allowed_updates=self.dp.resolve_used_update_types()),
                name="admin-bot"))
            self._spawn(asyncio.create_task(self.subs.run(), name="subscriptions"))
            self._spawn(asyncio.create_task(self._webhook_guard(), name="webhook-guard"))
            await self._set_menu()
        # 2) всё остальное — в фоне; ошибка любого шага не роняет бота
        self._spawn(asyncio.create_task(self._boot(run_bot), name="boot"))
        self._spawn(asyncio.create_task(self._bench_loop(), name="bench"))

    async def _boot(self, run_bot: bool) -> None:
        async def step(name, coro):
            try:
                return await coro
            except Exception:
                log.exception("Startup step failed: %s (continuing)", name)
        # после деплоя папка telegram_sessions/ может быть пустой → восстановить из Telegram
        await step("restore backup", self.backup.restore())
        # копия подписок на месте — можно обрабатывать входы и оплаты; если копию прочитать не удалось,
        # никого не кикаем «за отсутствие подписки» (данных о подписках просто нет)
        self.subs.degraded = not self.backup.restore_done
        self.subs.ready.set()
        if not self.backup.restore_done:
            # копию прочитать не удалось — пробуем дальше в фоне; пока не прочитаем, сохранять
            # нельзя (затрём хорошую копию), а значит и новые аккаунты не попадут в копию
            self._spawn(asyncio.create_task(self._restore_retry_loop(), name="restore-retry"))
        await step("restore market snapshot", self.backup.restore_market())
        # разовая миграция: убрать старые ценовые /set (555, фильтр дороже флора, лимит аккаунтов) —
        # теперь действуют значения из .env (порог 400, все аккаунты параллельно)
        if not await self._flag("migr_prices_v1"):
            from config import drop_runtime_overrides
            gone = drop_runtime_overrides(["COLLECTION_MIN_FLOOR", "MIN_STARS", "MAX_ABOVE_FLOOR",
                                           "MAX_CONCURRENT_SESSIONS", "GLOBAL_RPS", "SESSION_RPS",
                                           "CATALOG_INTERVAL", "TRACKER_INTERVAL", "COLD_POLL_INTERVAL"])
            await self._set_flag("migr_prices_v1")
            if gone:
                log.info("Reset old /set values to .env: %s", ", ".join(gone))
        if not await self._flag("migr_seller_v3"):
            from config import drop_runtime_overrides
            drop_runtime_overrides(["UNKNOWN_SELLER_POLICY", "SELLER_PHONE_PREFIXES"])     # block: нужен хоть один признак
            await self._set_flag("migr_seller_v3")
        if not await self._flag("migr_speed_v4"):
            from config import drop_runtime_overrides
            drop_runtime_overrides(["CATALOG_INTERVAL", "GLOBAL_RPS", "PRIORITY_POLL_INTERVAL", "SESSION_RPS",
                                    "MAX_CONCURRENT_SESSIONS", "TRACKER_INTERVAL", "COLD_POLL_INTERVAL"])
            await self._set_flag("migr_speed_v4")
        if not await self._flag("migr_speed_v5"):
            # спокойные скорости вместо старых /set (из-за них был шторм FloodWait)
            from config import drop_runtime_overrides
            drop_runtime_overrides(["PRIORITY_SHARE", "PRIORITY_POLL_INTERVAL", "SESSION_RPS", "GLOBAL_RPS",
                                    "COLD_POLL_INTERVAL", "SYNC_MODE"])
            await self._set_flag("migr_speed_v5")
        self.sessions.limiter.set_rate(settings.global_rps)     # значения после восстановления /set
        if os.getenv("USE_PROXIES", "0").strip().lower() not in ("1", "true", "yes", "on"):
            n = self.storage.wipe_proxies()          # прокси выключены — стираем пул и привязки
            if n:
                log.info("Proxies disabled: removed proxy from %d account(s)", n)
        if self.backup.restored_paused:
            await step("restore pause", self.db.kv_set("paused", 1))
        self.users_closed = bool(self.backup.flags.get("users_closed"))
        if self.backup.restored_paused or str(await self.db.kv_get("paused") or "0") == "1":
            self.coordinator.paused = True     # /off действует сразу, ещё до подключения аккаунтов
        if settings.startup_delay > 0 and self.storage.list_sessions():
            # при деплое старая копия бота может ещё работать: если обе подключат один аккаунт,
            # Telegram аннулирует его (AUTH_KEY_DUPLICATED). Даём старой копии завершиться.
            log.info("Waiting %ss before connecting accounts (old instance shutdown)...", settings.startup_delay)
            await asyncio.sleep(settings.startup_delay)
        await step("connect sessions", self.sessions.start_all())
        await step("restore account speeds", self._restore_speeds())
        self.booting = False
        await step("menu", self._set_menu())      # админы из копии получают полное меню
        if run_bot:
            self._spawn(asyncio.create_task(self._startup_report(), name="startup-report"))
        if getattr(self, "_flags_dirty", False):
            await step("save flags", self.backup.save())   # чтобы разовые сбросы не повторились
        await step("backup sync", self.backup.sync_if_needed())
        if run_bot:
            self._spawn(asyncio.create_task(self._backup_loop(), name="backup-loop"))
        self.pipeline.start()
        await step("tracker", self.coordinator.start())

    async def _notify_auth_error(self, w) -> None:
        """Telegram снёс сессию (выкинули из «Устройств», две копии бота, бан) — она уже не оживёт.
        Удаляем аккаунт из трекера автоматически и сообщаем владельцу/админам."""
        owner = next((r.owner_id for r in self.storage.list_sessions() if r.name == w.name), None)
        targets = {owner} if owner else set()
        targets |= (settings.admin_ids or all_admins())
        removed = False
        if settings.auto_remove_dead:
            await asyncio.sleep(3)
            try:
                if w.name in self.sessions.workers and w.status.value == "AUTH_ERROR":
                    self.backup.bury(w.name, w.reason)      # можно вернуть через /revive
                    await self.sessions.remove(w.name)
                    await self.backup.save(allow_shrink=True)
                    removed = True
                    log.warning("Session %s removed automatically: %s", w.name, w.reason)
            except Exception as e:
                log.warning("Auto-remove of %s failed: %s", w.name, e)
        text = (f"⚠️ Аккаунт <b>{html.escape(w.name)}</b> отключён Telegram.\n"
                f"Причина: {html.escape(w.reason or '—')}\n\n"
                + ("🗑 Удалён из трекера автоматически. Если это ошибка — /removed, вернуть: /revive ИМЯ."
                   if removed else "Удалите его в /sessions и добавьте заново через /add_session."))
        for chat in targets:
            try:
                await self.bot.send_message(chat, text)
            except Exception:
                pass

    def _spawn(self, t: asyncio.Task) -> None:
        """Фоновая задача: держим ссылку и громко пишем в лог, если она упала (а не молча умерла)."""
        self._tasks.append(t)

        def _done(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                log.error("Background task %s crashed: %r", task.get_name(), task.exception())
        t.add_done_callback(_done)

    async def _flag(self, name: str) -> bool:
        return bool(self.backup.flags.get(name) or await self.db.kv_get(name))

    async def _set_flag(self, name: str) -> None:
        self.backup.flags[name] = 1          # попадёт в копию → миграция не повторится после деплоя
        await self.db.kv_set(name, 1)
        self._flags_dirty = True

    async def _bench_loop(self) -> None:
        """Живой замер раз в минуту: запросы, FloodWait, ошибки, пинг, задержка лота по этапам."""
        prev = None
        tick = 0
        while True:
            await asyncio.sleep(60)
            tick += 1
            if tick % 360 == 1:          # раз в 6 часов — чистка базы от старого мусора
                try:
                    # чистку рынка — не в первые часы после запуска (сначала опросы обновят «последний раз видели»)
                    n = await self.db.housekeeping(
                        market_days=30 if (settings.sync_mode == "fast" and tick > 1) else 0)
                    if n:
                        log.info("Housekeeping: removed %s old filtered listing(s)", n)
                except Exception as e:
                    log.debug("housekeeping failed: %s", e)
            try:
                ws = list(self.sessions.workers.values())
                cur = (sum(w.requests for w in ws), sum(w.floods for w in ws), sum(w.errors for w in ws),
                       self.pipeline.stats.get("new", 0), self.pipeline.stats.get("sent", 0))
                if prev is not None:
                    d = [a - b for a, b in zip(cur, prev)]
                    pings = [w.ping_ms for w in ws if getattr(w, "ping_ms", None)]
                    slow = sum(1 for w in ws if getattr(w, "speed", 1.0) < 1.0)
                    lat = self.pipeline.latency
                    self.bench = {"req": d[0], "flood": d[1], "err": d[2], "new": d[3], "sent": d[4],
                                  "ping": (sum(pings) / len(pings)) if pings else None, "slow": slow}
                    log.info("BENCH 60s: requests=%s (%.1f/s) floods=%s errors=%s new=%s sent=%s ping=%s "
                             "slowed_sessions=%s | lot: request=%s queue=%s check=%s send=%s total=%s",
                             d[0], d[0] / 60, d[1], d[2], d[3], d[4],
                             f"{self.bench['ping']:.0f}ms" if self.bench["ping"] else "-", slow,
                             *(f"{lat[k]:.2f}s" if k in lat else "-"
                               for k in ("request", "queue", "check", "send", "total")))
                prev = cur
            except Exception as e:
                log.debug("bench failed: %s", e)
            try:
                await self._save_speeds()
                await self._watchdog()
                await self._watchdog_sent()
                await self._watchdog_conflict()
                await self._daily_report()
            except Exception as e:
                log.debug("watchdog failed: %s", e)

    async def _restore_retry_loop(self) -> None:
        delay = 60
        while not self.backup.restore_done:
            await asyncio.sleep(delay)
            delay = min(600, delay * 2)
            try:
                await self.backup.restore()
            except Exception as e:
                log.warning("Background backup restore failed: %s", e)
                continue
            if self.backup.restore_done:
                log.info("Backup restored in background — connecting restored accounts and saving")
                self.subs.degraded = False
                # применить восстановленное состояние, как при обычном запуске
                if self.backup.restored_paused:
                    self.coordinator.paused = True
                    try:
                        await self.db.kv_set("paused", 1)
                    except Exception as e:
                        log.warning("Could not store paused flag: %s", e)
                self.users_closed = bool(self.backup.flags.get("users_closed"))
                self.sessions.limiter.set_rate(settings.global_rps)
                found = bool(self.backup.backup_names) or self.backup.restored_count
                try:
                    await self.sessions.rescan()
                    await self.backup.save()      # теперь в копию попадут и аккаунты, добавленные за это время
                except Exception as e:
                    log.warning("After background restore: %s", e)
                for admin in (settings.admin_ids or all_admins()) if found else ():
                    try:
                        await self.bot.send_message(admin, "✅ Резервная копия прочитана — аккаунты и подписки "
                                                           "восстановлены, копия снова сохраняется.")
                    except Exception:
                        pass

    async def _save_speeds(self) -> None:
        """Выученный темп каждого аккаунта (после FloodWait) — чтобы после перезапуска аккаунты не
        начинали снова с полной скорости и не ловили FloodWait заново."""
        if self.booting:
            return                 # аккаунты ещё не подключены — не затирать сохранённое
        now = time.time()
        sp = {w.name: [round(w.speed, 3), now] for w in self.sessions.workers.values() if w.speed < 1.0}
        self.backup.flags["speeds"] = sp           # попадёт в резервную копию при следующем сохранении
        await self.db.kv_set("speeds", json.dumps(sp))

    async def _restore_speeds(self) -> None:
        raw = await self.db.kv_get("speeds")
        try:
            sp = json.loads(raw) if raw else (self.backup.flags.get("speeds") or {})
        except ValueError:
            sp = {}
        now, n = time.time(), 0
        for name, (speed, ts) in (sp or {}).items():
            w = self.sessions.workers.get(name)
            age = now - float(ts)
            if w is None or age > 6 * 3600:
                continue
            # за время простоя аккаунт «остыл» — немного разгоняем, но не до полной скорости
            w.speed = min(1.0, max(0.15, float(speed) * (1.2 if age > 1800 else 1.0)))
            n += 1
        if n:
            log.info("Restored learned speed for %s account(s)", n)

    async def _startup_report(self) -> None:
        """После запуска/деплоя — короткий отчёт админам: всё ли поднялось."""
        await asyncio.sleep(60)          # даём коллекциям выйти в работу
        try:
            c = self.sessions.counts()
            live, total = self.coordinator.sync_progress()
            ok = c.get("ACTIVE", 0)
            head = "✅ <b>Бот запущен</b>" if ok and live else "⚠️ <b>Бот запущен, но есть проблемы</b>"
            lines = [head, "",
                     f"👥 Аккаунтов: {len(self.sessions.workers)} · работают: {ok}"
                     + (f" · FloodWait: {c.get('FLOOD_WAIT', 0)}" if c.get("FLOOD_WAIT") else "")
                     + (f" · ошибки: {c.get('ERROR', 0) + c.get('AUTH_ERROR', 0)}"
                        if c.get("ERROR") or c.get("AUTH_ERROR") else ""),
                     f"📚 Коллекций в работе: {live}/{total}",
                     f"⏸ Парсер выключен (/on)" if self.coordinator.paused else "🟢 Парсер работает"]
            if getattr(self, "users_closed", False):
                lines.append("🔒 Для пользователей закрыто (/open)")
            strict = []
            if settings.russian_only and settings.unknown_seller_policy == "block":
                strict.append("только русские продавцы")
            if settings.require_free_messages:
                strict.append("только с бесплатными сообщениями")
            if settings.unique_sellers:
                strict.append("продавец один раз" + (f" за {settings.seller_repeat_hours:g} ч"
                                                      if settings.seller_repeat_hours else " навсегда"))
            if strict:
                lines.append("🧹 Фильтры: " + ", ".join(strict) + " · почему отсеяно: /why")
            try:
                rights = await self._chat_rights_report()
            except Exception as e:
                rights = [f"⚠️ Не удалось проверить права бота: {html.escape(str(e)[:120])}"]
            if rights:
                lines[0] = "⚠️ <b>Бот запущен, но есть проблемы</b>"
                lines += [""] + rights
            for admin in (settings.admin_ids or all_admins()):
                try:
                    await self.bot.send_message(admin, "\n".join(lines))
                except Exception:
                    pass
        except Exception as e:
            log.debug("startup report failed: %s", e)

    async def why_report(self, hours: float = 1.0) -> str:
        """«Почему нет постов»: что трекер нашёл за последние часы и на чём лоты отсеялись."""
        import re as _re
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600))
        rows = await self.db.reasons_since(since)
        by_status: dict[str, int] = {}
        reasons: dict[tuple[str, str], int] = {}
        last_fail = ""
        for st, reason, n in rows:
            by_status[st] = by_status.get(st, 0) + n
            key = _re.sub(r"\d[\d.,]*", "N", reason or "—")
            for en, ru in _WHY_RU:
                if key.startswith(en):
                    key = ru
                    break
            key = key[:90]
            reasons[(st, key)] = reasons.get((st, key), 0) + n
            if st == "SEND_FAILED" and reason:
                last_fail = reason
        found = sum(by_status.values())
        names = {"SENT": "✅ отправлено", "FILTERED_PRICE": "💰 отсеяно по цене",
                 "FILTERED_SELLER": "👤 отсеяно по продавцу", "FILTERED_DUPLICATE": "🔁 повторы",
                 "SEND_FAILED": "❌ не отправилось", "ERROR": "⌛ устарели/ошибка", "NEW": "⏳ в очереди",
                 "SENDING": "📤 отправляются", "TEST_ONLY": "🧪 тест"}
        span = f"{hours:g} ч"
        lines = [f"🔎 <b>Почему нет постов — за {span}</b>", ""]
        c = self.sessions.counts()
        if self.coordinator.paused:
            lines.append("⏸ <b>Парсер выключен</b> — включить: /on")
        if not self.sessions.workers:
            lines.append("🚨 <b>Нет ни одного аккаунта</b> — добавьте: /add_session")
        elif not self.sessions.usable():
            lines.append(f"🚨 <b>Нет рабочих аккаунтов</b> ({len(self.sessions.workers)} всего) — /sessions")
        lines.append(f"👥 Аккаунтов работает: {c.get('ACTIVE', 0)} из {len(self.sessions.workers)}")
        live, total = self.coordinator.sync_progress()
        lines.append(f"📚 Коллекций в работе: {live}/{total}")
        lines.append(f"🎁 Найдено новых лотов: <b>{found}</b>")
        for st, n in sorted(by_status.items(), key=lambda x: -x[1]):
            lines.append(f"  {names.get(st, st)}: {n}")
        top = sorted(((n, st, r) for (st, r), n in reasons.items() if st not in ("SENT", "TEST_ONLY")), reverse=True)[:6]
        if top:
            lines += ["", "<b>Главные причины отсева:</b>"]
            lines += [f"  {n} × {html.escape(r)}" for n, st, r in top]
        if last_fail:
            lines += ["", f"❌ Последняя ошибка отправки: <code>{html.escape(last_fail[:300])}</code>",
                      "Проверьте, что бот — админ группы с правом писать и управлять темами (/test channel)."]
        if found == 0 and self.sessions.usable() and not self.coordinator.paused:
            lines += ["", "Новых лотов не найдено совсем — рынок тихий или коллекции ещё синхронизируются."]
        return "\n".join(lines)

    async def _chat_rights_report(self) -> list[str]:
        """Проверка прав бота во всех группах, куда идут посты (частая причина «не кидает лоты»)."""
        from bot.subscription import sub_chat_id
        sub = sub_chat_id()
        chats = {settings.target_chat_id, sub} | {c for _, _, c, _ in settings.price_routes} | \
                {c for _, c, _ in settings.backdrop_routes}
        out = []
        me = await self.bot.get_me()
        for chat in sorted(c for c in chats if c):
            try:
                m = await self.bot.get_chat_member(chat, me.id)
            except Exception as e:
                out.append(f"❌ Группа <code>{chat}</code>: бот не видит её ({html.escape(str(e)[:120])}) — добавьте бота")
                continue
            st = getattr(m, "status", "")
            if st not in ("administrator", "creator"):
                out.append(f"❌ Группа <code>{chat}</code>: бот не админ (статус {html.escape(str(st))}) — выдайте права")
            elif st == "administrator":
                miss = []
                if getattr(m, "can_manage_topics", None) is False:
                    miss.append("управление темами")
                if chat == sub and getattr(m, "can_invite_users", None) is False:
                    miss.append("приглашения (для подписок)")
                if chat == sub and getattr(m, "can_restrict_members", None) is False:
                    miss.append("бан (для подписок)")
                if miss:
                    out.append(f"⚠️ Группа <code>{chat}</code>: нет прав — {', '.join(miss)}")
        return out

    async def _alert(self, kind: str, text: str, every: float = 3600) -> None:
        """Сообщение админам, но одно и то же не чаще раза в `every` секунд."""
        now = time.monotonic()
        last = self._alerts.get(kind, -1e9)
        if now - last < every:
            return
        self._alerts[kind] = now
        for admin in (settings.admin_ids or all_admins()):
            try:
                await self.bot.send_message(admin, text)
            except Exception:
                pass

    async def _watchdog(self) -> None:
        """Сам сообщает админам, если трекер перестал нормально работать."""
        coord = self.coordinator
        if self.booting or coord.paused or coord.mode != "LIVE":
            self._bad_since.clear()
            return
        now = time.monotonic()
        total = len(self.sessions.workers)
        usable = len(self.sessions.usable())

        def lasting(kind: str, cond: bool, secs: float) -> bool:
            if not cond:
                if kind in self._bad_since and now - self._bad_since[kind] >= secs:
                    self._alerts.pop(kind, None)
                self._bad_since.pop(kind, None)
                return False
            self._bad_since.setdefault(kind, now)
            return now - self._bad_since[kind] >= secs

        if lasting("no_accounts", usable == 0 and not getattr(self, "booting", False), 180):
            text = (f"🚨 <b>Нет рабочих аккаунтов</b> ({total} всего: FloodWait/ошибки).\n"
                    "Лоты сейчас не ищутся. Подробнее: /sessions") if total else (
                    "🚨 <b>В трекере не осталось ни одного аккаунта</b> — лоты не ищутся.\n"
                    "Добавьте аккаунт: /add_session")
            await self._alert("no_accounts", text, 1800)
        elif lasting("few_accounts", total >= 4 and usable < total * 0.4, 600):
            hint = ("" if os.getenv("USE_PROXIES", "0").strip().lower() in ("1", "true", "yes", "on") else
                    "\nСовет: все аккаунты на одном IP — подключите прокси (USE_PROXIES=1 и /proxies), "
                    "это сильнее всего снижает FloodWait.")
            await self._alert("few_accounts", f"⚠️ Работает только <b>{usable} из {total}</b> аккаунтов уже 10+ минут.\n"
                                              "Лоты идут медленнее. Подробнее: /sessions" + hint, 3600)
        last_new = self.pipeline.last_new_at
        quiet = time.time() - (last_new or self._started_at)
        if lasting("no_lots", usable > 0 and quiet > 1800, 0):
            try:
                why = "\n\n" + await self.why_report(1)
            except Exception:
                why = ""
            await self._alert("no_lots", f"⚠️ <b>{int(quiet // 60)} минут ни одного нового лота.</b>"
                                         + (why or "\nОбычно лоты идут каждые несколько минут — проверьте /status"), 3600)

    async def _watchdog_conflict(self) -> None:
        """Тот же токен бота работает где-то ещё → наши пользователи получают чужие ответы."""
        w = CONFLICT_WATCH
        # при своём же деплое старая и новая копии пересекаются на минуту — это не тревога;
        # тревога — если конфликт идёт дольше 3 минут подряд
        if w.count >= 3 and time.time() - w.last < 300 and w.last - w.first > 180:
            await self._alert("token_conflict",
                              "🚨 <b>Этим токеном бота пользуется кто-то ещё</b> (Telegram: Conflict).\n"
                              "Либо запущена вторая копия бота, либо токен утёк. Если второй копии нет — "
                              "@BotFather → /revoke, новый токен в BOT_TOKEN, репозиторий — приватный.", 3600)

    async def _watchdog_sent(self) -> None:
        """Лоты находятся, но в группу ничего не уходит 30+ минут — сразу говорим админам почему."""
        if self.booting or self.coordinator.paused or self.coordinator.mode != "LIVE" or settings.test_mode:
            return
        last_new = self.pipeline.last_new_at
        last_sent = getattr(self.pipeline, "last_sent_at", None) or self._started_at
        if not last_new or last_new <= last_sent or time.time() - last_sent < 1800:
            return
        try:
            why = await self.why_report(1)
        except Exception as e:
            why = f"(не удалось собрать причины: {html.escape(str(e)[:100])})"
        await self._alert("no_sent", f"⚠️ <b>Лоты находятся, но за {int((time.time() - last_sent) // 60)} мин "
                                     f"ни один не отправлен.</b>\n\n" + why, 3600)

    async def _daily_report(self) -> None:
        """Итоги за сутки админам — раз в день в 10:00 (TIMEZONE)."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        try:
            local = _dt.now(ZoneInfo(settings.timezone))
        except Exception:
            local = _dt.utcnow()
        if local.hour != 10:
            return
        today = local.strftime("%Y-%m-%d")
        if await self.db.kv_get("daily_report") == today:
            return
        await self.db.kv_set("daily_report", today)
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))
        day = await self.db.counts_since(since)
        found = sum(day.values())
        ws = list(self.sessions.workers.values())
        sub = self.subs.stats()
        lines = [f"📊 <b>Итоги за сутки</b> · {local.strftime('%d.%m.%Y')}", "",
                 f"🎁 Найдено лотов: {found} · отправлено: {day.get('SENT', 0)}",
                 f"👤 Отсеяно по продавцу: {day.get('FILTERED_SELLER', 0)} · повторы: {day.get('FILTERED_DUPLICATE', 0)}",
                 f"💰 Оплат: {sub['paid_day_count']} на {sub['paid_day']:g} TON · активных подписчиков: {sub['active']}",
                 f"👥 Аккаунтов: {len(ws)} · сейчас работают: {len(self.sessions.usable())}"]
        tot = sorted(t for t, e in getattr(self.pipeline, "totals", []) if e)
        if len(tot) >= 3:
            lines.append(f"⏱ Дорогие лоты: обычно {tot[len(tot) // 2]:.1f} с до поста, "
                         f"9 из 10 быстрее {tot[min(len(tot) - 1, int(len(tot) * 0.9))]:.1f} с")
        elif "total" in self.pipeline.latency:
            lines.append(f"⏱ Средняя задержка лота: {self.pipeline.latency['total']:.1f} с")
        muted = self.backup.flags.get("muted") if hasattr(self.backup, "flags") else None
        if muted:
            lines.append(f"🔇 Выключено коллекций: {len(muted)} (/mute)")
        if self.backup.graveyard:
            fresh = [g for g in self.backup.graveyard.values() if time.time() - g.get("ts", 0) < 86400]
            if fresh:
                lines.append(f"🗑 Удалено аккаунтов за сутки: {len(fresh)} (/removed)")
        for admin in (settings.admin_ids or all_admins()):
            try:
                await self.bot.send_message(admin, "\n".join(lines))
            except Exception:
                pass

    async def _set_menu(self) -> None:
        """Команды в синей кнопке «Меню» рядом с полем ввода."""
        cmds = [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="add_session", description="Добавить аккаунт"),
            BotCommand(command="sessions", description="Мои аккаунты / удалить"),
            BotCommand(command="status", description="Состояние трекера"),
            BotCommand(command="why", description="Почему нет постов"),
            BotCommand(command="lot", description="Почему не пришёл лот (ссылка t.me/nft/…)"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="last", description="Последние лоты и почему не отправлены"),
            BotCommand(command="collections", description="Какие коллекции отслеживаются"),
            BotCommand(command="off", description="Выключить парсер (пользователям: «Парсер закрыт»)"),
            BotCommand(command="on", description="Включить парсер"),
            BotCommand(command="close", description="Закрыть для пользователей (лоты идут)"),
            BotCommand(command="open", description="Открыть для пользователей"),
            BotCommand(command="top", description="Самые активные коллекции за сутки"),
            BotCommand(command="find", description="Всё о коллекции: /find название"),
            BotCommand(command="mute", description="Выключить лоты коллекции: /mute название"),
            BotCommand(command="removed", description="Удалённые аккаунты (вернуть: /revive)"),
            BotCommand(command="proxies", description="Прокси для аккаунтов"),
            BotCommand(command="settings", description="Настройки"),
            BotCommand(command="test", description="Проверка работы"),
            BotCommand(command="cancel", description="Отменить действие"),
        ]
        cmds.append(BotCommand(command="subs", description="Подписчики и выручка"))
        cmds.append(BotCommand(command="grant", description="Выдать подписку: /grant ID|@юз 24|3д|навсегда"))
        cmds.append(BotCommand(command="revoke", description="Забрать подписку: /revoke ID|@юз"))
        try:
            # всем — только /start; полное меню — только админам из ADMIN_IDS
            await self.bot.set_my_commands([BotCommand(command="start", description="Главное меню")],
                                           scope=BotCommandScopeDefault())
            for admin in (settings.admin_ids or all_admins()):
                try:
                    await self.bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=admin))
                except Exception as e:
                    log.warning("setMyCommands for admin %s failed: %s", admin, e)
        except Exception as e:
            log.warning("setMyCommands failed: %s", e)

    async def _drop_webhook(self, why: str) -> str | None:
        """Бот работает через getUpdates. Если кто-то включил вебхук с тем же токеном, команды
        перестают доходить (Conflict: terminated by setWebhook request). Снимаем вебхук."""
        try:
            info = await self.bot.get_webhook_info()
            url = info.url or ""
            if url:
                await self.bot.delete_webhook(drop_pending_updates=False)
                log.warning("Webhook %s was set by ANOTHER program using this bot token (%s) — removed. "
                            "Stop that program, otherwise it will keep stealing bot commands.", url, why)
                return url
        except Exception as e:
            log.warning("Webhook check failed: %s", e)
        return None

    async def _webhook_guard(self) -> None:
        """Раз в минуту проверяем, не включил ли кто-то вебхук; при первом случае пишем админам."""
        warned: set[str] = set()
        while True:
            await asyncio.sleep(60)
            url = await self._drop_webhook("guard")
            if url:
                host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
                if host not in warned:
                    warned.add(host)
                    for chat in all_admins():
                        try:
                            await self.bot.send_message(
                                chat, "⚠️ Другая программа использует токен этого бота и включила вебхук "
                                      f"(<code>{html.escape(host)}</code>). Из-за этого бот переставал отвечать "
                                      "на команды. Я его снял, но она включит его снова — остановите её "
                                      "(например, второй бот с этим токеном на Bothost) или смените токен.")
                        except Exception:
                            pass

    async def _backup_loop(self) -> None:
        """История отправленного — раз в 10 мин (если что-то отправлено);
        снимок рынка — когда все коллекции впервые в LIVE и далее раз в MARKET_BACKUP_MINUTES."""
        last_sent = self.pipeline.stats["sent"]
        last_hist = last_market = time.monotonic()
        market_saved_full = False
        while True:
            await asyncio.sleep(60)
            if not self.storage.list_sessions():
                continue
            now = time.monotonic()
            try:
                if self.pipeline.stats["sent"] != last_sent and now - last_hist >= 600:
                    last_sent, last_hist = self.pipeline.stats["sent"], now
                    await self.backup.save()
                live, total = self.coordinator.sync_progress()
                all_live = total > 0 and live == total
                if (all_live and not market_saved_full) or now - last_market >= settings.market_backup_minutes * 60:
                    if await self.backup.save_market():
                        last_market = now
                        market_saved_full = market_saved_full or all_live
            except Exception:
                log.exception("Periodic backup failed")

    async def stop(self) -> None:
        log.info("Shutting down...")
        for t in self._tasks:
            if t.get_name() == "boot" and not t.done():
                t.cancel()             # SIGTERM во время запуска — не стартуем трекер на выходе
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        await self.coordinator.stop()
        # сначала дообработать уже найденные лоты (до 10 сек), чтобы они попали и в канал, и в копию
        if not self.coordinator.paused:
            try:
                await asyncio.wait_for(self.pipeline.queue.join(), timeout=10)
            except asyncio.TimeoutError:
                log.warning("Queue not drained; unfinished listings will be resumed on next start")
        await self.pipeline.stop()
        for t in self._tasks:
            if t.get_name() == "backup-loop":
                t.cancel()             # не мешает финальному сохранению (общий замок сохранения)
        if self.storage.list_sessions():
            # каждый шаг отдельно: сбой одного не отменяет другой
            for what, coro, to in (("backup", self.backup.save(), 20), ("market", self.backup.save_market(), 25)):
                try:
                    await asyncio.wait_for(coro, timeout=to)
                except Exception as e:
                    log.warning("Final %s save failed: %r", what, e)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.sessions.stop_all()
        await self.db.close()
        await self.bot.session.close()
        log.info("Stopped")


def _validate() -> list[str]:
    problems = []
    if not settings.bot_token:
        problems.append("BOT_TOKEN не задан в .env")
    if not settings.target_chat_id:
        problems.append("TARGET_CHAT_ID не задан в .env")
    if not settings.api_id or not settings.api_hash:
        problems.append("API_ID / API_HASH не заданы в .env (нужны для sessions без собственного .json)")
    return problems


async def amain(check: bool) -> None:
    setup_logging(settings.path("logs"), settings.log_level)
    for s in settings.secrets():
        register_secret(s)
    problems = _validate()
    for p in problems:
        log.error(p)
    if problems and not check:
        sys.exit(1)

    app = App()
    if check:
        await app.db.connect()
        await app.sessions.start_all()
        report, sample = await app.run_check()
        print("\n" + html.unescape(report.replace("<b>", "").replace("</b>", "")))
        if sample:
            print("\n--- пример сообщения ---\n" + sample)
        await app.sessions.stop_all()
        await app.db.close()
        await app.bot.session.close()
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows — остановка через Ctrl+C (KeyboardInterrupt)

    try:
        await app.start()
    except RuntimeError as e:          # несовместимая БД и т.п. — понятное сообщение, без удаления данных
        log.error("%s", e)
        await app.bot.session.close()
        sys.exit(2)
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await app.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Gift Marketplace Live Tracker")
    parser.add_argument("--check", action="store_true", help="диагностика без рассылки")
    args = parser.parse_args()
    try:
        asyncio.run(amain(args.check))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
