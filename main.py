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
import html
import signal
import time
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from bot.admin import all_admins, build_routers, init_claim_code, save_admins
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


class App:
    def __init__(self) -> None:
        self.db = Database(settings.path(settings.database_path))
        self.storage = SessionStorage(settings.path(settings.sessions_dir), settings.api_id, settings.api_hash)
        self.sessions = SessionManager(self.storage)
        self.bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
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
        self.dp = Dispatcher(storage=MemoryStorage())
        for r in build_routers(self):
            self.dp.include_router(r)
        self._tasks: list[asyncio.Task] = []

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
        # после деплоя папка telegram_sessions/ может быть пустой → восстановить из Telegram
        await self.backup.restore()
        await self.backup.restore_market()    # снимок рынка → без долгой полной синхронизации
        if self.backup.restored_paused:
            await self.db.kv_set("paused", 1)  # пауза, поставленная до деплоя, сохраняется
        if settings.startup_delay > 0 and self.storage.list_sessions():
            # при деплое старая копия бота может ещё работать: если обе подключат один аккаунт,
            # Telegram аннулирует его (AUTH_KEY_DUPLICATED). Даём старой копии завершиться.
            log.info("Waiting %ss before connecting accounts (old instance shutdown)...", settings.startup_delay)
            await asyncio.sleep(settings.startup_delay)
        await self.sessions.start_all()
        await self.backup.sync_if_needed()   # например, sessions положены в папку вручную
        if run_bot:
            await self._set_menu()
            self._tasks.append(asyncio.create_task(self._backup_loop(), name="backup-loop"))
        if run_bot:
            init_claim_code()
            self._tasks.append(asyncio.create_task(
                self.dp.start_polling(self.bot, handle_signals=False), name="admin-bot"))
        self.pipeline.start()
        self._tasks.append(asyncio.create_task(self.coordinator.start(), name="coordinator-start"))

    async def _notify_auth_error(self, w) -> None:
        owner = next((r.owner_id for r in self.storage.list_sessions() if r.name == w.name), None)
        targets = {owner} if owner else set()
        targets |= all_admins()
        text = (f"⚠️ Аккаунт <b>{html.escape(w.name)}</b> отключён Telegram.\n"
                f"Причина: {html.escape(w.reason or '—')}\n\n"
                "Трекер его не удалял — Telegram больше не принимает эту сессию. "
                "Удалите его в /sessions и добавьте аккаунт заново через /add_session.")
        for chat in targets:
            try:
                await self.bot.send_message(chat, text)
            except Exception:
                pass

    async def _set_menu(self) -> None:
        """Команды в синей кнопке «Меню» рядом с полем ввода."""
        cmds = [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="add_session", description="Добавить аккаунт"),
            BotCommand(command="sessions", description="Мои аккаунты / удалить"),
            BotCommand(command="status", description="Состояние трекера"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="pause", description="Остановить парсинг"),
            BotCommand(command="resume", description="Продолжить парсинг"),
            BotCommand(command="settings", description="Настройки"),
            BotCommand(command="test", description="Проверка работы"),
            BotCommand(command="cancel", description="Отменить действие"),
        ]
        try:
            await self.bot.set_my_commands(cmds)
        except Exception as e:
            log.warning("setMyCommands failed: %s", e)

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
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        await self.coordinator.stop()
        try:
            if self.storage.list_sessions():
                await asyncio.wait_for(self.backup.save(), timeout=15)
                await asyncio.wait_for(self.backup.save_market(), timeout=40)
        except Exception:
            pass
        # дать очереди дообработать уже найденные листинги (до 15 сек)
        try:
            await asyncio.wait_for(self.pipeline.queue.join(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("Queue not drained; unfinished listings will be resumed on next start")
        await self.pipeline.stop()
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
