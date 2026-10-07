"""
TelegramSender — отправка постов в канал TARGET_CHAT_ID через Bot API (aiogram 3).

• Сериализованная отправка с минимальным интервалом SEND_INTERVAL (лимит Telegram для
  одного канала ~20 сообщений/мин, при превышении → RetryAfter, который мы честно ждём).
• TEST_MODE: в канал ничего не отправляется; первые TEST_MAX_MESSAGES сообщений уходят в
  TEST_CHAT_ID (или первому админу), остальные только пишутся в лог.
"""
from __future__ import annotations

import asyncio
import heapq
import time
from collections import deque

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter, TelegramServerError)
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from config import settings
from utils.logger import get_logger

log = get_logger("sender")


class SendResult:
    def __init__(self, ok: bool, message_id: int | None = None, test_only: bool = False, error: str | None = None):
        self.ok, self.message_id, self.test_only, self.error = ok, message_id, test_only, error


class _PriorityLock:
    """Как asyncio.Lock, но следующим получает доступ ждущий с меньшим prio (дорогие лоты — 0)."""

    def __init__(self):
        self._busy = False
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._seq = 0

    async def acquire(self, prio: int = 1) -> None:
        if not self._busy and not self._waiters:
            self._busy = True
            return
        self._seq += 1
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (prio, self._seq, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()                 # доступ уже передали — отдаём следующему
            else:
                self._waiters = [w for w in self._waiters if w[2] is not fut]
                heapq.heapify(self._waiters)
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)           # _busy остаётся True — доступ передан
                return
        self._busy = False


class TelegramSender:
    # Telegram: не больше ~20 сообщений в минуту в одну группу. Держимся чуть ниже, чтобы не ловить
    # RetryAfter (он бывает на 30+ секунд и задерживает все лоты разом).
    PER_CHAT_MINUTE = 18

    def __init__(self, bot: Bot):
        self.bot = bot
        self._lock = _PriorityLock()
        self._last = 0.0
        self._recent: dict[int, deque[float]] = {}
        self.test_sent = 0

    def _window_wait(self, chat_id: int) -> float:
        """Сколько ждать, чтобы не превысить PER_CHAT_MINUTE сообщений в минуту в эту группу."""
        recent = self._recent.setdefault(chat_id, deque())
        now = time.monotonic()
        while recent and now - recent[0] > 60:
            recent.popleft()
        return recent[0] + 60 - now if len(recent) >= self.PER_CHAT_MINUTE else 0.0

    def _test_chat(self) -> int | None:
        if settings.test_chat_id:
            return settings.test_chat_id
        return next(iter(sorted(settings.admin_ids)), None)

    async def send_listing(self, text: str, preview_url: str | None,
                           markup: InlineKeyboardMarkup | None = None,
                           fallback_markup: InlineKeyboardMarkup | None = None,
                           target: int | None = None, thread: int | None = None,
                           priority: bool = False) -> SendResult:
        if settings.test_mode:
            chat = self._test_chat()
            if chat is None or self.test_sent >= settings.test_max_messages:
                log.info("[TEST_MODE] message not sent (log only)")
                return SendResult(True, None, test_only=True)
            self.test_sent += 1
            res = await self.send(chat, "🧪 <b>TEST MODE</b>\n" + text, preview_url, markup, fallback_markup,
                                  priority=priority)
            res.test_only = True
            return res
        # target/thread — канал и тема по маршруту (PRICE/BACKDROP_ROUTES); иначе общий TARGET_CHAT_ID
        return await self.send(target or settings.target_chat_id, text, preview_url, markup,
                               fallback_markup, thread_id=thread, priority=priority)

    async def send(self, chat_id: int, text: str, preview_url: str | None = None,
                   markup: InlineKeyboardMarkup | None = None,
                   fallback_markup: InlineKeyboardMarkup | None = None, attempts: int = 5,
                   thread_id: int | None = None, priority: bool = False) -> SendResult:
        preview = (LinkPreviewOptions(url=preview_url, prefer_small_media=True, show_above_text=False)
                   if preview_url else LinkPreviewOptions(is_disabled=True))
        extra = {"message_thread_id": thread_id} if thread_id else {}
        reopened: int | bool = False
        # лимит группы ждём ВНЕ общей очереди: пока одна группа «отдыхает», посты в другие идут
        while True:
            w = self._window_wait(chat_id)
            if w > 0:
                await asyncio.sleep(w)
                continue
            await self._lock.acquire(0 if priority else 1)     # дорогие лоты — первыми в очереди на отправку
            if self._window_wait(chat_id) > 0:
                self._lock.release()
                continue
            break
        held = True
        if True:
          try:
            attempt, flood_waits = -1, 0
            while attempt + 1 < attempts:
                attempt += 1
                wait = self._last + settings.send_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    msg = await self.bot.send_message(chat_id, text, parse_mode="HTML",
                                                      link_preview_options=preview, reply_markup=markup,
                                                      **extra)
                    self._last = time.monotonic()
                    self._recent.setdefault(chat_id, deque()).append(self._last)
                    return SendResult(True, msg.message_id)
                except TelegramRetryAfter as e:
                    log.warning("Bot API RetryAfter %ss", e.retry_after)
                    # ждём БЕЗ общей очереди: пока эта группа под лимитом, посты в другие темы/группы идут;
                    # ожидание лимита — не «неудачная попытка», лот не теряем (но и не ждём бесконечно)
                    flood_waits += 1
                    if flood_waits <= 8:
                        attempt -= 1
                    held = False
                    self._lock.release()
                    await asyncio.sleep(e.retry_after + 0.5)
                    await self._lock.acquire(0 if priority else 1)
                    held = True
                except (TelegramNetworkError, TelegramServerError) as e:
                    delay = min(30, 2 ** attempt)
                    log.warning("Bot API network/server error: %s (retry in %ss)", e, delay)
                    await asyncio.sleep(delay)
                except TelegramBadRequest as e:
                    es = str(e)
                    # тема закрыта → пробуем открыть её (нужно право «Управление темами») и повторить
                    if extra and "TOPIC_CLOSED" in es and not reopened:
                        try:
                            await self.bot.reopen_forum_topic(chat_id, extra["message_thread_id"])
                            reopened = extra["message_thread_id"]     # закрывать обратно — только если открыли
                            log.info("Topic %s is closed — opened it for one post, will close again",
                                     extra["message_thread_id"])
                            continue
                        except Exception as re_err:
                            log.warning("Topic %s is closed and cannot be reopened (%s). Open the topic "
                                        "in the group or give the bot the «Manage topics» admin right",
                                        extra["message_thread_id"], re_err)
                    # тема (topic) закрыта/удалена/не найдена → шлём в общий канал, чтобы лот не потерять
                    if extra and ("TOPIC_CLOSED" in es or "TOPIC_DELETED" in es
                                  or "message thread not found" in es.lower()
                                  or "MESSAGE_THREAD_NOT_FOUND" in es):
                        log.warning("Topic %s unavailable (%s) — posting to the main channel instead",
                                    extra.get("message_thread_id"), es)
                        extra = {}
                        continue
                    # кнопка «Продавец» по ID запрещена приватностью продавца → без этой кнопки
                    if ("BUTTON_USER_PRIVACY_RESTRICTED" in es or "BUTTON_URL_INVALID" in es
                            or "BUTTON_USER_INVALID" in es):
                        if fallback_markup is not None and markup is not fallback_markup:
                            markup = fallback_markup
                            continue
                    # например, битая ссылка превью — повторяем без превью
                    if preview.is_disabled is not True:      # (у aiogram там бывает Default(...), он «истинный»)
                        preview = LinkPreviewOptions(is_disabled=True)
                        continue
                    log.error("Bot API bad request: %s", e)
                    return SendResult(False, error=str(e))
                except TelegramForbiddenError as e:
                    log.error("Bot has no access to chat %s: %s", chat_id, e)
                    return SendResult(False, error=str(e))
            return SendResult(False, error="retries exhausted")
          except TelegramAPIError as e:
            log.error("Bot API error: %s", e)
            return SendResult(False, error=str(e))
          finally:
            try:
                if reopened:
                    # тему закрыл владелец (чтобы писал только бот) — закрываем обратно в любом случае
                    try:
                        await self.bot.close_forum_topic(chat_id, reopened)
                    except Exception as e:
                        log.warning("Could not close topic %s back: %s", reopened, e)
            finally:
                if held:
                    self._lock.release()
