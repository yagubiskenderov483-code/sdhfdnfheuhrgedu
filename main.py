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

    async def _notify_bot_dead(self, bot, next_bot=None, kind="silent"):
        b = html.escape(bot)
        if kind == "back":
            text = f"✅ Lookup-бот @{b} снова отвечает."
        elif kind == "missing":
            text = (f"⚠️ Lookup-бот @{b} не находится — похоже, его снесли или переименовали.\n"
                    "Трекер работает дальше, поиск идёт через запасных ботов (если они есть).")
        else:
            text = (f"⚠️ Lookup-бот @{b} не отвечает.\n"
                    "Трекер работает дальше: следующие 10 мин сначала спрашиваю запасных ботов, потом снова его.")
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
