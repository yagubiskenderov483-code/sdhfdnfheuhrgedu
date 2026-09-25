"""
TelegramSender — отправка постов в канал TARGET_CHAT_ID через Bot API (aiogram 3).

• Сериализованная отправка с минимальным интервалом SEND_INTERVAL (лимит Telegram для
  одного канала ~20 сообщений/мин, при превышении → RetryAfter, который мы честно ждём).
• TEST_MODE: в канал ничего не отправляется; первые TEST_MAX_MESSAGES сообщений уходят в
  TEST_CHAT_ID (или первому админу), остальные только пишутся в лог.
"""
from __future__ import annotations

import asyncio
import time

from aiogram import Bot
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter)
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from config import settings
from utils.logger import get_logger

log = get_logger("sender")


class SendResult:
    def __init__(self, ok: bool, message_id: int | None = None, test_only: bool = False, error: str | None = None):
        self.ok, self.message_id, self.test_only, self.error = ok, message_id, test_only, error


class TelegramSender:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.test_sent = 0

    def _test_chat(self) -> int | None:
        if settings.test_chat_id:
            return settings.test_chat_id
        return next(iter(sorted(settings.admin_ids)), None)

    async def send_listing(self, text: str, preview_url: str | None,
                           markup: InlineKeyboardMarkup | None = None,
                           fallback_markup: InlineKeyboardMarkup | None = None,
                           target: int | None = None) -> SendResult:
        if settings.test_mode:
            chat = self._test_chat()
            if chat is None or self.test_sent >= settings.test_max_messages:
                log.info("[TEST_MODE] message not sent (log only)")
                return SendResult(True, None, test_only=True)
            self.test_sent += 1
            res = await self.send(chat, "🧪 <b>TEST MODE</b>\n" + text, preview_url, markup, fallback_markup)
            res.test_only = True
            return res
        # target — канал по цене (PRICE_ROUTES); если не задан, общий TARGET_CHAT_ID
        return await self.send(target or settings.target_chat_id, text, preview_url, markup, fallback_markup)

    async def send(self, chat_id: int, text: str, preview_url: str | None = None,
                   markup: InlineKeyboardMarkup | None = None,
                   fallback_markup: InlineKeyboardMarkup | None = None, attempts: int = 5) -> SendResult:
        preview = (LinkPreviewOptions(url=preview_url, prefer_small_media=True, show_above_text=False)
                   if preview_url else LinkPreviewOptions(is_disabled=True))
        async with self._lock:
            for attempt in range(attempts):
                wait = self._last + settings.send_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    msg = await self.bot.send_message(chat_id, text, parse_mode="HTML",
                                                      link_preview_options=preview, reply_markup=markup)
                    self._last = time.monotonic()
                    return SendResult(True, msg.message_id)
                except TelegramRetryAfter as e:
                    log.warning("Bot API RetryAfter %ss", e.retry_after)
                    await asyncio.sleep(e.retry_after + 0.5)
                except TelegramNetworkError as e:
                    delay = min(30, 2 ** attempt)
                    log.warning("Bot API network error: %s (retry in %ss)", e, delay)
                    await asyncio.sleep(delay)
                except TelegramBadRequest as e:
                    # кнопка «Продавец» по ID запрещена приватностью продавца → без этой кнопки
                    if "BUTTON_USER_PRIVACY_RESTRICTED" in str(e) or "BUTTON_URL_INVALID" in str(e):
                        if fallback_markup is not None and markup is not fallback_markup:
                            markup = fallback_markup
                            continue
                    # например, битая ссылка превью — повторяем без превью
                    if not preview.is_disabled:
                        preview = LinkPreviewOptions(is_disabled=True)
                        continue
                    log.error("Bot API bad request: %s", e)
                    return SendResult(False, error=str(e))
                except TelegramForbiddenError as e:
                    log.error("Bot has no access to chat %s: %s", chat_id, e)
                    return SendResult(False, error=str(e))
            return SendResult(False, error="retries exhausted")
