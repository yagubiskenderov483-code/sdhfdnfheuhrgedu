"""
SessionWorker — одна Telegram-сессия (аккаунт).

• Сам никаких запросов не планирует: запросы ему выдаёт MarketplaceCoordinator.
• Соблюдает минимальный интервал между своими запросами (SESSION_RPS) и глобальный лимит.
• FloodWait: session помечается FLOOD до истечения ожидания; задача уходит другой session.
• Разрыв соединения: reconnect с экспоненциальным backoff, остальные workers работают дальше.
• Отзыв авторизации: статус AUTH_ERROR, worker выключается из ротации.
"""
from __future__ import annotations

import asyncio
import time
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from telethon import TelegramClient, errors

from tracker.ratelimit import TokenBucket, backoff_delay
from utils.proxy import mask_proxy, parse_proxy
from utils.logger import get_logger

log = get_logger("worker")


class WorkerStatus(str, Enum):
    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    FLOOD = "FLOOD_WAIT"
    ERROR = "ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    STOPPED = "STOPPED"


try:  # ошибки прокси (python-socks) — это проблемы соединения, а не авторизации
    from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
    _PROXY_ERRORS: tuple = (ProxyError, ProxyConnectionError, ProxyTimeoutError)
except ImportError:  # pragma: no cover
    _PROXY_ERRORS = ()

# Почему Telegram «выкинул» аккаунт — понятным языком
AUTH_REASONS = {
    "AuthKeyDuplicatedError": "аккаунт был запущен в двух местах одновременно (две копии бота, "
                              "например старая и новая при деплое) — Telegram аннулировал эту сессию",
    "AuthKeyUnregisteredError": "сессия завершена: её закрыли в «Устройствах» Telegram, "
                                "это сделал прежний владелец аккаунта или сам Telegram",
    "SessionRevokedError": "сессия завершена из «Устройств» Telegram (владелец или прежний владелец аккаунта)",
    "SessionExpiredError": "сессия истекла",
    "UserDeactivatedError": "аккаунт удалён",
    "UserDeactivatedBanError": "аккаунт заблокирован Telegram",
}


class WorkerUnavailable(Exception):
    """Запрос не выполнен по причине, связанной с самой session (flood / disconnect / auth) —
    координатор должен повторить его на другой session."""


class SessionWorker:
    def __init__(self, name: str, session_path: Path, api_id: int, api_hash: str,
                 limiter: TokenBucket, rps_getter: Callable[[], float], proxy: str | None = None):
        self.name = name
        self.session_path = session_path
        self._api_id = api_id
        self._api_hash = api_hash
        self.limiter = limiter
        self._rps = rps_getter
        self.client: TelegramClient | None = None
        self.proxy = proxy

        self.status = WorkerStatus.CONNECTING
        self.reason: str | None = None
        self.flood_until = 0.0
        self.next_allowed = 0.0
        self.busy = False
        self.requests = 0
        self.errors = 0
        self.floods = 0
        self.me_id: int | None = None
        self._reconnect_attempt = 0
        self._running = True
        self._health_task: asyncio.Task | None = None
        self.on_auth_error = None          # async callback(worker) — уведомить владельца

    # ------------------------------------------------------------------ lifecycle
    def _make_client(self) -> TelegramClient:
        # путь без расширения — Telethon сам добавит .session
        base = str(self.session_path.with_suffix(""))
        return TelegramClient(
            base, self._api_id, self._api_hash,
            proxy=parse_proxy(self.proxy),
            flood_sleep_threshold=0,      # FloodWait обрабатываем сами, чтобы переключать session
            connection_retries=3,
            retry_delay=2,
            auto_reconnect=True,
            request_retries=2,
            receive_updates=False,        # апдейты этим аккаунтам не нужны → меньше нагрузки
            device_model="GiftTracker",
            system_version="1.0",
            app_version="1.0",
        )

    async def connect(self) -> bool:
        self.status = WorkerStatus.CONNECTING
        self.reason = None
        try:
            if self.client is None:
                self.client = self._make_client()
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                self.status = WorkerStatus.AUTH_ERROR
                self.reason = ("сессия не авторизована: Telegram завершил её (закрыли в «Устройствах» / "
                               "прежний владелец / две копии бота одновременно)")
                if self.on_auth_error:
                    asyncio.get_running_loop().create_task(self.on_auth_error(self))
                log.warning("Session %s: %s", self.name, self.reason)
                await self._safe_disconnect()
                return False
            me = await self.client.get_me()
            self.me_id = getattr(me, "id", None)
            self.status = WorkerStatus.ACTIVE
            self._reconnect_attempt = 0
            log.info("Session %s connected", self.name)
            return True
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.UserDeactivatedError,
                errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError, errors.SessionExpiredError) as e:
            self._auth_failed(e)
            await self._safe_disconnect()
            return False
        except Exception as e:  # сеть, прокси и т.п.
            self.status = WorkerStatus.ERROR
            self.reason = f"{type(e).__name__}: {e}"
            log.warning("Session %s connect failed: %s", self.name, self.reason)
            return False

    def start_health_loop(self) -> None:
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_loop(), name=f"health-{self.name}")

    async def _health_loop(self) -> None:
        while self._running:
            try:
                now = time.monotonic()
                if self.status == WorkerStatus.FLOOD and now >= self.flood_until:
                    self.status = WorkerStatus.ACTIVE
                    log.info("Session %s: FloodWait finished → ACTIVE", self.name)
                elif self.status == WorkerStatus.ACTIVE and self.client and not self.client.is_connected():
                    self.status = WorkerStatus.ERROR
                    self.reason = "disconnected"
                if self.status == WorkerStatus.ERROR:
                    delay = backoff_delay(self._reconnect_attempt, base=5, cap=300)
                    self._reconnect_attempt += 1
                    log.info("Session %s reconnect in %.0fs (attempt %s)", self.name, delay, self._reconnect_attempt)
                    await asyncio.sleep(delay)
                    if not self._running:
                        break
                    await self.connect()
                    continue
                if self.status in (WorkerStatus.AUTH_ERROR, WorkerStatus.STOPPED):
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Session %s health loop error", self.name)
            await asyncio.sleep(2)

    async def change_proxy(self, proxy: str | None) -> bool:
        """Переподключить аккаунт через другой прокси (или без него)."""
        self.proxy = proxy
        await self._safe_disconnect()
        self.client = None
        ok = await self.connect()
        log.info("Session %s now via %s: %s", self.name, mask_proxy(proxy), "OK" if ok else self.reason)
        return ok

    async def stop(self) -> None:
        self._running = False
        if self._health_task:
            self._health_task.cancel()
        await self._safe_disconnect()
        if self.status != WorkerStatus.AUTH_ERROR:
            self.status = WorkerStatus.STOPPED

    async def _safe_disconnect(self) -> None:
        try:
            if self.client:
                await self.client.disconnect()
        except Exception:
            pass

    def _auth_failed(self, e: Exception) -> None:
        first = self.status != WorkerStatus.AUTH_ERROR
        self.status = WorkerStatus.AUTH_ERROR
        self.reason = f"{AUTH_REASONS.get(type(e).__name__, 'авторизация отозвана Telegram')} ({type(e).__name__})"
        log.error("Session %s: %s", self.name, self.reason)
        if first and self.on_auth_error:
            try:
                asyncio.get_running_loop().create_task(self.on_auth_error(self))
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ availability
    def is_usable(self) -> bool:
        return self.status == WorkerStatus.ACTIVE or (
            self.status == WorkerStatus.FLOOD and time.monotonic() >= self.flood_until)

    def ready_at(self) -> float:
        return max(self.next_allowed, self.flood_until)

    def available(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.is_usable() and not self.busy and now >= self.ready_at()

    # ------------------------------------------------------------------ calls
    async def call(self, fn: Callable[[TelegramClient], Awaitable[Any]]) -> Any:
        """Выполнить запрос. Бросает WorkerUnavailable при flood/сети/авторизации,
        прочие RPCError (например STARGIFT_INVALID) пробрасывает как есть."""
        if self.client is None:
            raise WorkerUnavailable("no client")
        self.busy = True
        try:
            wait = self.next_allowed - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            await self.limiter.acquire()
            rps = max(0.05, self._rps())
            self.next_allowed = time.monotonic() + 1.0 / rps
            self.requests += 1
            if self.status == WorkerStatus.FLOOD:
                self.status = WorkerStatus.ACTIVE
            return await fn(self.client)
        except errors.FloodError as e:
            seconds = int(getattr(e, "seconds", 0) or 30)
            self.floods += 1
            self.flood_until = time.monotonic() + seconds + 1
            self.status = WorkerStatus.FLOOD
            self.reason = f"FloodWait {seconds}s"
            log.warning("Session %s FloodWait %ss — waiting, requests go to other sessions", self.name, seconds)
            raise WorkerUnavailable(self.reason) from e
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.UserDeactivatedError,
                errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError, errors.SessionExpiredError) as e:
            self.errors += 1
            self._auth_failed(e)
            raise WorkerUnavailable(self.reason) from e
        except (ConnectionError, OSError, asyncio.TimeoutError, *_PROXY_ERRORS) as e:
            self.errors += 1
            self.status = WorkerStatus.ERROR
            self.reason = f"connection{' (proxy ' + mask_proxy(self.proxy) + ')' if self.proxy else ''}: {type(e).__name__}"
            raise WorkerUnavailable(self.reason) from e
        except errors.ServerError as e:
            # «Telegram is having internal issues» (500, RpcMcgetFail и т.п.) — сбой на стороне Telegram,
            # аккаунт в порядке: запрос повторит другой аккаунт
            raise WorkerUnavailable(f"telegram internal error: {type(e).__name__}") from e
        except errors.RPCError:
            self.errors += 1
            raise
        except ValueError as e:
            # Telethon после нескольких неудачных повторов: «Request was unsuccessful N time(s)»
            if "unsuccessful" in str(e):
                raise WorkerUnavailable("telegram internal error (retries exhausted)") from e
            raise
        finally:
            self.busy = False
