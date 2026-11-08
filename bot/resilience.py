"""Acknowledge slow controls and report errors without replaying side effects."""
from __future__ import annotations
import asyncio
import logging
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

log = logging.getLogger(__name__)


class ResponsiveReplies(BaseMiddleware):
    def __init__(self):
        self.active = set()

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        if not isinstance(message, Message):
            return await handler(event, data)
        from bot.admin import MAIN_KB
        labels = {button.text for row in MAIN_KB.keyboard for button in row}
        text = (event.data or "") if callback else (message.text or "")
        if not callback and not text.startswith("/") and text not in labels:
            return await handler(event, data)
        key = (event.from_user.id if event.from_user else 0, message.chat.id, text,
               message.message_id if callback else 0)

        async def notice(text):
            try:
                if callback:
                    await event.answer(text)
                else:
                    await message.answer(text)
            except Exception:
                log.debug("Control notice could not be delivered")

        if key in self.active:
            await notice("Команда уже выполняется.")
            return None
        self.active.add(key)

        async def acknowledge():
            await asyncio.sleep(0.7 if callback else 1.2)
            await notice("Команда принята, выполняю.")

        task = asyncio.create_task(acknowledge())
        try:
            return await handler(event, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Control handler failed: %s", type(exc).__name__)
            await notice("Ошибка команды. Перед повтором проверь /status; трекер продолжает работу.")
            return None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.active.discard(key)
