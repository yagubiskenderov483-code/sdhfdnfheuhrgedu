"""Keep the admin bot responsive: acknowledge slow commands, never swallow a repeated command."""
from __future__ import annotations
import asyncio
import logging
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from config import settings

log = logging.getLogger(__name__)

_ACK_AFTER = 1.2      # команда дольше 1.2 с — пишем «выполняю», чтобы было видно что бот жив
_STILL_AFTER = 15.0   # дольше 15 с — ещё одно сообщение, что команда жива


def _is_admin(uid: int | None) -> bool:
    """Админ (из .env ADMIN_IDS или подтверждённый через /claim)."""
    if not uid:
        return False
    if uid in settings.admin_ids:
        return True
    try:
        from bot.admin import all_admins
        return uid in all_admins()
    except Exception:
        return False


class ResponsiveReplies(BaseMiddleware):
    def __init__(self):
        self.active_callbacks = set()
        self._labels_cache: frozenset[str] | None = None   # MAIN_KB.keyboard не меняется

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        if not isinstance(message, Message):
            return await handler(event, data)
        if self._labels_cache is None:
            from bot.admin import MAIN_KB
            self._labels_cache = frozenset(button.text for row in MAIN_KB.keyboard for button in row)
        labels = self._labels_cache
        text = (event.data or "") if callback else (message.text or "")
        if not callback and not text.startswith("/") and text not in labels:
            return await handler(event, data)

        async def notice(msg):
            try:
                if callback:
                    await event.answer(msg)
                else:
                    await message.answer(msg)
            except Exception:
                log.debug("Control notice could not be delivered")

        # Двойное нажатие одной и той же inline-кнопки гасим (чтобы не сделать действие дважды).
        # Команды текстом НЕ гасим: раньше повтор команды отвечал «уже выполняется» и молча
        # выкидывался — из-за этого приходилось писать по несколько раз.
        key = None
        if callback:
            key = (event.from_user.id if event.from_user else 0, message.chat.id, text, message.message_id)
            if key in self.active_callbacks:
                await notice("Уже выполняю…")
                return None
            self.active_callbacks.add(key)

        # «⏳ Выполняю…» — только админам. Обычному пользователю такое сообщение
        # лишнее (Maker: 'обычному пользователю такой писать не должно').
        uid = getattr(event.from_user, "id", None)
        show_ack = _is_admin(uid)

        async def acknowledge():
            await asyncio.sleep(_ACK_AFTER)
            await notice("Выполняю…")
            if not callback:
                await asyncio.sleep(_STILL_AFTER - _ACK_AFTER)
                await notice("Всё ещё выполняется — ответ придёт, повторять не нужно.")

        # для inline-кнопок «⏳» не шлём: на callback можно ответить только один раз,
        # и наш ответ сломал бы callback.answer() внутри обработчика
        task = None if callback or not show_ack else asyncio.create_task(acknowledge())
        import time as _t
        t0 = _t.monotonic()
        try:
            res = await handler(event, data)
            took = _t.monotonic() - t0
            (log.warning if took > 5 else log.info)("CMD %r от %s — %.1f с", text[:40],
                                                    getattr(event.from_user, "id", "?"), took)
            return res
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Control handler failed: %s", type(exc).__name__)
            await notice("Ошибка команды, трекер продолжает работу. Попробуй ещё раз.")
            return None
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if key is not None:
                self.active_callbacks.discard(key)
