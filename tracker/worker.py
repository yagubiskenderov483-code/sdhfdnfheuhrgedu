"""
SessionWorker — одна Telegram-сессия (аккаунт).

• Сам никаких запросов не планирует: запросы ему выдаёт SessionManager (опросы рынка, проверки продавцов).
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

from telethon import TelegramClient, errors, functions, types

from tracker.ratelimit import TokenBucket, backoff_delay
from utils.proxy import mask_proxy, parse_proxy
from utils.logger import get_logger

log = get_logger("worker")



def _foreign_cancel() -> bool:
    """CancelledError пришла не потому, что отменили НАШУ задачу (а, например, Telethon отменил запрос)."""
    t = asyncio.current_task()
    cancelling = getattr(t, "cancelling", None)        # Python 3.11+; на старых — ведём себя как раньше
    return t is not None and cancelling is not None and not cancelling()

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
        self.priority_waiters = 0          # срочные запросы (проверка продавца) ждут именно эту session
        # здоровье аккаунта: при FloodWait/ошибках сам замедляется, при долгой стабильной работе разгоняется
        self.speed = 1.0                   # множитель скорости: 1.0 — норма, 0.2 — сильно замедлен
        self.ok_streak = 0                 # успешных запросов подряд
        self.ping_ms: float | None = None  # среднее время ответа Telegram, мс
        self.recent_floods: list[float] = []   # моменты FloodWait за последний час
        self._last_up = 0.0
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
        if self.client is None:
            self.client = self._make_client()
        cl = self.client
        try:
            return await self._connect(cl)
        except asyncio.CancelledError:
            if not _foreign_cancel():
                raise
            # клиент отключили во время подключения (удаление аккаунта / смена прокси) — это не отмена задачи
            if cl is self.client:
                self.status, self.reason = WorkerStatus.ERROR, "disconnected during connect"
            return False

    async def _connect(self, cl) -> bool:
        try:
            if not cl.is_connected():
                await cl.connect()
            # ВАЖНО: не client.is_user_authorized() — он считает «не авторизован» ЛЮБУЮ ошибку Telegram,
            # включая FloodWait и сбой сервера. Из-за этого живые аккаунты в FloodWait при переподключении
            # помечались «снесёнными» и удалялись. Проверяем сами и различаем причины.
            try:
                users = await cl(functions.users.GetUsersRequest([types.InputUserSelf()]))
            except errors.FloodError as e:
                seconds = int(getattr(e, "seconds", 0) or 30)
                self.flood_until = time.monotonic() + seconds + 1
                self.status = WorkerStatus.ERROR
                self.reason = f"FloodWait {seconds}s при подключении — аккаунт живой, переподключусь позже"
                log.warning("Session %s: %s", self.name, self.reason)
                return False
            except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError) as e:
                self._auth_failed(e)
                await self._safe_disconnect()
                return False
            me = users[0] if users else None
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
            if cl is not self.client:
                return False             # пока подключались, клиент заменили (/proxy) — новый уже работает
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
                    delay = max(backoff_delay(self._reconnect_attempt, base=5, cap=300),
                                self.flood_until - time.monotonic())
                    self._reconnect_attempt += 1
                    log.info("Session %s reconnect in %.0fs (attempt %s)", self.name, delay, self._reconnect_attempt)
                    await asyncio.sleep(delay)
                    if not self._running:
                        break
                    if self.status != WorkerStatus.ERROR:
                        continue         # пока ждали, аккаунт уже подключили (например, /proxy)
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
        if self.status != WorkerStatus.AUTH_ERROR:
            self.start_health_loop()     # цикл мог завершиться — без него аккаунт не переподключится сам
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

    def _slow_down(self, flood_seconds: int) -> None:
        """Аккаунт начал ловить FloodWait → снижаем его скорость, чтобы Telegram его не ограничил сильнее.
        Чем чаще и дольше FloodWait за последний час, тем сильнее замедление."""
        now = time.monotonic()
        self.recent_floods = [t for t in self.recent_floods if now - t < 3600] + [now]
        factor = 0.5 if flood_seconds < 60 else 0.3
        if len(self.recent_floods) >= 3:
            factor *= 0.6
        self.speed = max(0.15, self.speed * factor)
        self.ok_streak = 0
        log.warning("Session %s: FloodWait %ss (%s за час) — замедляю до x%.2f",
                    self.name, flood_seconds, len(self.recent_floods), self.speed)

    def risk(self) -> str:
        """Оценка риска: ok | warn | high — по частоте FloodWait и ошибок."""
        now = time.monotonic()
        n = len([t for t in self.recent_floods if now - t < 3600])
        if n >= 3 or self.speed <= 0.3:
            return "high"
        if n >= 1 or self.speed < 1.0:
            return "warn"
        return "ok"

    def ready_at(self) -> float:
        return max(self.next_allowed, self.flood_until)

    def available(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.is_usable() and not self.busy and now >= self.ready_at()

    # ------------------------------------------------------------------ calls
    async def call(self, fn: Callable[[TelegramClient], Awaitable[Any]], priority: bool = False) -> Any:
        """Выполнить запрос. Бросает WorkerUnavailable при flood/сети/авторизации,
        прочие RPCError (например STARGIFT_INVALID) пробрасывает как есть."""
        if self.client is None:
            raise WorkerUnavailable("no client")
        self.busy = True
        try:
            wait = self.next_allowed - time.monotonic()
            calm = not (self.recent_floods and time.monotonic() - self.recent_floods[-1] < 600)
            # без «долга»: сократить можно только обычную паузу, а не накопленную прошлым сокращением
            if priority and self.speed >= 0.99 and calm and wait <= 1.0 / max(0.05, self._rps() * self.speed):
                # проверка продавца не ждёт полную паузу после опроса рынка: 2 быстрых запроса подряд
                # Telegram не смущают, а лот уходит на секунды раньше. Средний темп аккаунта тот же —
                # следующий опрос рынка на этом аккаунте подождёт.
                wait = min(wait, 0.6)
            if wait > 0:
                await asyncio.sleep(wait)
            await self.limiter.acquire()
            rps = max(0.05, self._rps() * self.speed)
            # сокращённая пауза не теряется: следующий запрос подождёт её остаток плюс свою обычную
            self.next_allowed = max(time.monotonic(), self.next_allowed) + 1.0 / rps
            self.requests += 1
            if self.status == WorkerStatus.FLOOD:
                self.status = WorkerStatus.ACTIVE
            t0 = time.monotonic()
            try:
                result = await fn(self.client)
            except asyncio.CancelledError:
                if not _foreign_cancel():
                    raise
                # Telethon отменяет запросы, когда клиент отключают (удаление аккаунта, /proxy) — это не
                # отмена нашей задачи: иначе молча умирали бы циклы трекера и обработчики лотов
                raise WorkerUnavailable("client disconnected (removed/proxy change)") from None
            ms = (time.monotonic() - t0) * 1000
            self.ping_ms = ms if self.ping_ms is None else self.ping_ms * 0.9 + ms * 0.1
            self.ok_streak += 1
            # долго без FloodWait → понемногу разгоняем (по времени, а не по числу запросов:
            # замедленный аккаунт делает мало запросов и иначе восстанавливался бы часами)
            now = time.monotonic()
            last_flood = self.recent_floods[-1] if self.recent_floods else 0.0
            if self.speed < 1.0 and now - last_flood > 180 and now - self._last_up > 90:
                self.speed = min(1.0, self.speed * 1.3)
                self._last_up = now
            return result
        except errors.FloodError as e:
            seconds = int(getattr(e, "seconds", 0) or 30)
            self.floods += 1
            self.flood_until = time.monotonic() + seconds + 1
            self._slow_down(seconds)
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
            self.ok_streak = 0
            self.status = WorkerStatus.ERROR
            self.reason = f"connection{' (proxy ' + mask_proxy(self.proxy) + ')' if self.proxy else ''}: {type(e).__name__}"
            raise WorkerUnavailable(self.reason) from e
        except errors.TimedOutError as e:
            # «-503 Timeout» — Telegram не успел ответить; аккаунт в порядке, повторит другой аккаунт сразу
            raise WorkerUnavailable("telegram timeout") from e
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
