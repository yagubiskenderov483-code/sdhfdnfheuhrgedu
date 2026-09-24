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
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from bot.admin import build_routers, init_claim_code
from bot.formatter import MessageFormatter
from bot.sender import TelegramSender
from config import settings
from database.db import Database
from filters.price_filter import PriceFilter
from filters.seller_filter import SellerFilter
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
        await self.sessions.start_all()
        if run_bot:
            init_claim_code()
            self._tasks.append(asyncio.create_task(
                self.dp.start_polling(self.bot, handle_signals=False), name="admin-bot"))
        self.pipeline.start()
        self._tasks.append(asyncio.create_task(self.coordinator.start(), name="coordinator-start"))

    async def stop(self) -> None:
        log.info("Shutting down...")
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        await self.coordinator.stop()
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

    await app.start()
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
