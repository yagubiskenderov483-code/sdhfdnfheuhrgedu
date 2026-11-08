"""Entrypoint preserving the existing lifecycle with explicit lookup warnings."""
from __future__ import annotations
import asyncio
import html
import app_core as _core
from app_core import *
from bot.resilience import ResponsiveReplies


def __getattr__(name):
    return getattr(_core, name)


class App(_core.App):
    def __init__(self):
        super().__init__()
        responsive = ResponsiveReplies()
        self.dp.message.middleware(responsive)
        self.dp.callback_query.middleware(responsive)

    async def _notify_bot_dead(self, bot, next_bot=None):
        text = (f"⚠️ Lookup-бот @{html.escape(bot)} не отвечает или недоступен.\n"
                "Причина не подтверждена: возможен временный сбой, переименование или удаление.\n\n"
                "Трекер продолжает работу. Основной бот не изменён; аккаунты не отключены. "
                "Лоты без проверенного владельца и @username не публикуются.")
        async def notify(chat):
            try:
                await asyncio.wait_for(self.bot.send_message(chat, text), timeout=8)
            except Exception:
                log.warning("Cannot deliver lookup warning to %s", chat)
        targets = set(settings.admin_ids or set()) | set(all_admins())
        await asyncio.gather(*(notify(chat) for chat in targets))


# Keep the original startup, shutdown, validation and CLI implementation intact.
# Its amain() resolves App through this module binding at execution time.
_core.App = App
if __name__ == "__main__":
    _core.main()
