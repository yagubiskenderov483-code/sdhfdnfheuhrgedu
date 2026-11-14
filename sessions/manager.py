"""
SessionManager — пул SessionWorker'ов.

При запуске: находит все *.session → подключает (не более CONNECT_CONCURRENCY одновременно) →
проверяет авторизацию → запускает health-loop (reconnect) для каждой.
Одна сломанная session никогда не останавливает остальные.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from config import settings
from sessions.storage import SessionRecord, SessionStorage
from tracker.ratelimit import TokenBucket
from tracker.worker import SessionWorker, WorkerStatus, WorkerUnavailable
from utils.logger import get_logger

log = get_logger("sessions")


class SessionManager:
    def __init__(self, storage: SessionStorage):
        self.storage = storage
        self.workers: dict[str, SessionWorker] = {}
        self.limiter = TokenBucket(settings.global_rps)
        self._connect_sem = asyncio.Semaphore(settings.connect_concurrency)
        self._changed = asyncio.Event()
        self.on_auth_error = None      # async callback(worker)
        self._base_rps = float(settings.global_rps)        # оригинал — чтобы разгон вернул на исходное
        self._throttle_until = 0.0                          # до какого момента держим пониженный RPS

    def flood_rate_per_minute(self) -> int:
        """Сколько FloodWait поймано всеми аккаунтами за последние 60 секунд."""
        now = time.monotonic()
        return sum(1 for w in self.workers.values()
                   for t in getattr(w, "recent_floods", []) if now - t < 60)

    def flooding_accounts_per_minute(self) -> int:
        """Сколько РАЗНЫХ аккаунтов ловили FloodWait за последние 60 секунд."""
        now = time.monotonic()
        return sum(1 for w in self.workers.values()
                   if any(now - t < 60 for t in getattr(w, "recent_floods", [])))

    def apply_adaptive_throttle(self) -> None:
        """Если шторм FloodWait по всему флоту — замедляем GLOBAL RPS в 2-10x
        И превентивно даём ВСЕМ рабочим воркерам speed-штраф, чтобы те кто ещё не словил
        тоже не словили. Через 3 минуты спокойствия возвращаем. Вызывается из _bench_loop.

        Пороги занижены (user: 20 FloodWait/мин был 0.3x → теперь 0.2x, 15/мин → 0.4x)
        после жалоб на шторм, где 26/26 аккаунтов замедлены до 0.15."""
        n = self.flood_rate_per_minute()
        now = time.monotonic()
        # Шторм по флоту — только если FloodWait ловят МНОГО РАЗНЫХ аккаунтов. Раньше 3 «больных»
        # аккаунта по 20 FloodWait/час давали 5+/мин и каждую минуту зажимали ВСЕ 25 до 0.3
        # («Мощность 1.4 из 25, замедлено 25»). Больные аккаунты и так замедляются сами по себе.
        accounts = self.flooding_accounts_per_minute()
        usable = max(1, len(self.usable()))
        fleet_storm = accounts >= max(4, int(usable * 0.3 + 0.999))
        if n >= 5 and fleet_storm:
            # более агрессивная шкала: раньше 10/мин = 0.5x, теперь уже 5/мин = 0.6x.
            # 40+/мин = 0.1x (катастрофа — замираем почти полностью, даём Telegram'у передышку)
            if n >= 40:
                factor = 0.1
            elif n >= 30:
                factor = 0.15
            elif n >= 20:
                factor = 0.2
            elif n >= 15:
                factor = 0.3
            elif n >= 10:
                factor = 0.45
            else:                        # 5..10/мин
                factor = 0.6
            new_rate = max(0.3, self._base_rps * factor)
            if abs(self.limiter.rate - new_rate) > 0.1:
                self.limiter.set_rate(new_rate)
                log.warning("FloodWait storm (%s/min) — global RPS → %.1f (×%.2f)", n, new_rate, factor)
            # превентив: ВСЕ ACTIVE воркеры (не flood'нувшие) тоже замедляются до speed*factor,
            # но не ниже 0.2 при обычном шторме / 0.1 при катастрофе. Это распределяет нагрузку равномерно.
            min_speed = 0.6 if factor >= 0.5 else (0.4 if factor >= 0.2 else 0.2)
            for w in self.workers.values():
                # аккаунты без своих FloodWait за 10 мин зажимаем мягко, а не до самого дна
                own_recent = any(now - t < 600 for t in getattr(w, "recent_floods", []))
                if w.is_usable() and w.speed > min_speed and own_recent is False:
                    old = w.speed
                    w.speed = max(min_speed, old * factor)
                    if old - w.speed > 0.05:
                        log.info("Session %s: превентивно speed %.2f → %.2f (шторм)",
                                 w.name, old, w.speed)
            # катастрофа: 5 минут cooldown вместо обычных 3 мин
            self._throttle_until = now + (300 if factor <= 0.2 else 180)
        elif self.limiter.rate < self._base_rps and now >= self._throttle_until:
            self.limiter.set_rate(self._base_rps)
            log.info("FloodWait calmed — global RPS → %.1f (base)", self._base_rps)

    # ------------------------------------------------------------------ loading
    async def start_all(self) -> None:
        records = self.storage.list_sessions()
        log.info("Found %s session file(s)", len(records))
        # return_exceptions=True: битая session (UserDeactivated, AuthKeyUnregistered,
        # повреждённый файл) не должна валить старт остальных 24 сессий. Логируем виновника.
        results = await asyncio.gather(
            *(self.add_record(r) for r in records), return_exceptions=True)
        for rec, res in zip(records, results):
            if isinstance(res, BaseException):
                log.error("Session %s failed to start: %s: %s",
                          rec.name, type(res).__name__, res)
        log.info("Sessions: %s", self.summary_line())

    async def add_record(self, rec: SessionRecord) -> SessionWorker:
        if rec.name in self.workers:
            return self.workers[rec.name]
        if not rec.api_id or not rec.api_hash:
            w = SessionWorker(rec.name, rec.path, 0, "", self.limiter, lambda: settings.session_rps)
            w.status, w.reason = WorkerStatus.ERROR, "API_ID/API_HASH not configured"
            self.workers[rec.name] = w
            return w
        w = SessionWorker(rec.name, rec.path, rec.api_id, rec.api_hash, self.limiter,
                          lambda: settings.session_rps, proxy=rec.proxy)
        w.on_auth_error = self.on_auth_error
        self.workers[rec.name] = w
        async with self._connect_sem:
            await w.connect()
        if w.status != WorkerStatus.AUTH_ERROR:
            w.start_health_loop()
        self._changed.set()
        return w

    async def rescan(self) -> list[str]:
        """Подхватить новые *.session, появившиеся в папке (команда /reload)."""
        # Явно сбрасываем кеш list_sessions — /reload может быть вызвана после ручного копирования
        # .session файлов, кеш (5с) не узнает о них вовремя.
        self.storage._invalidate_list_cache()
        added = []
        for rec in self.storage.list_sessions():
            if rec.name not in self.workers:
                await self.add_record(rec)
                added.append(rec.name)
        return added

    async def remove(self, name: str) -> bool:
        w = self.workers.pop(name, None)
        if w:
            await w.stop()
        return self.storage.remove(name) or w is not None

    async def stop_all(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)

    # ------------------------------------------------------------------ selection
    def get(self, name: str | None) -> SessionWorker | None:
        return self.workers.get(name) if name else None

    def reserved(self) -> set[str]:
        """Аккаунты, выделенные только под бота поиска (LOOKUP_SESSIONS + LOOKUP_DEDICATED).
        Если выделены вообще все рабочие аккаунты — резерв не действует, иначе трекер встанет."""
        if (settings.lookup_dedicated and settings.auto_lookup and settings.lookup_bot_username
                and settings.lookup_sessions):
            names = set(settings.lookup_sessions)
            if all(w.name in names for w in self.workers.values() if w.is_usable()):
                return set()
            return names
        # АВТО-резерв: LOOKUP_SESSIONS не заданы вручную — держим N здоровых аккаунтов только под funstat,
        # чтобы FloodWait от трекинга не отбирал у lookup все сессии («половина ботов не кидает запросы»).
        n = int(getattr(settings, "lookup_reserve", 0) or 0)
        if n > 0 and settings.auto_lookup and settings.lookup_bot_username:
            usable = [w for w in self.workers.values() if w.is_usable()]
            # Раньше порог был >=12: при FloodWait живых падало ниже 12, резерв ОТКЛЮЧАЛСЯ
            # целиком, и весь funstat валился на один anchor-аккаунт — очередь на резолв росла
            # до ~100 с. Теперь резерв работает и в деградации: не больше трети живых уходит под
            # funstat (трекинг всегда держит >=2/3), но минимум 2 сессии — чтобы резолв шёл
            # параллельно, а не по одной. Полностью выключаем только если живых совсем мало (<6).
            if len(usable) >= 6:
                n = min(n, max(2, len(usable) // 3))     # под funstat не больше трети живых, но >=2
                picked = sorted(usable, key=lambda w: w.name)[:n]   # стабильный набор (по имени)
                return {w.name for w in picked}
        return set()

    def usable(self, include_reserved: bool = False) -> list[SessionWorker]:
        res = set() if include_reserved else self.reserved()
        return [w for w in self.workers.values() if w.is_usable() and w.name not in res]

    def effective(self) -> float:
        """Реальная мощность аккаунтов с учётом замедления после FloodWait:
        24 аккаунта на x0.3 — это ~7 «полных», а не 24."""
        return sum(max(0.15, w.speed) for w in self.usable())

    async def run(self, fn, only: str | None = None, attempts: int = 6,
                  timeout: float | None = 60, prefer: str | None = None,
                  priority: bool = False) -> tuple[Any, str]:
        """Выполнить MTProto-запрос на любой свободной session (или строго на `only`).
        FloodWait / обрыв на одной session → автоматически повтор на другой.
        Возвращает (результат, имя session)."""
        last: Exception | None = None
        for attempt in range(attempts):
            pw = self.workers.get(only) if (priority and only) else None
            if pw is not None:
                pw.priority_waiters += 1        # обход коллекций уступает эту session проверке лота
            try:
                w = await self.acquire(timeout=timeout, only=only, prefer=prefer if attempt == 0 else None)
            finally:
                if pw is not None:
                    pw.priority_waiters -= 1
            try:
                return await w.call(fn, priority=priority), w.name
            except WorkerUnavailable as e:
                last = e
                if only:
                    break
                await asyncio.sleep(min(2.0, 0.1 * (2 ** attempt)))
            finally:
                self.release(w)
        raise last or WorkerUnavailable("no sessions")

    async def acquire(self, timeout: float | None = None, only: str | None = None,
                      prefer: str | None = None) -> SessionWorker:
        """Ждёт свободную session с наименьшим ready_at. Резервирует её (busy=True)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if only:
                w = self.workers.get(only)
                if w is None or w.status in (WorkerStatus.AUTH_ERROR, WorkerStatus.STOPPED):
                    raise WorkerUnavailable(f"session {only} unavailable")
                if not w.is_usable() and (w.status == WorkerStatus.ERROR or w.flood_until - now > 3):
                    # умерла / долгий FloodWait — не ждём её, вызывающий переключится на другую
                    raise WorkerUnavailable(f"session {only} is {w.status.value}")
                candidates = [w] if (w.is_usable() and not w.busy) else []
            else:
                res = self.reserved()
                candidates = [w for w in self.workers.values()
                              if w.is_usable() and not w.busy and not w.priority_waiters and w.name not in res]
            if candidates:
                pw = self.workers.get(prefer) if prefer else None
                soonest = min(candidates, key=lambda w: w.ready_at())
                # «своя» session коллекции — только если она не сильно медленнее самой свободной
                best = pw if (pw in candidates and pw.ready_at() <= soonest.ready_at() + 0.5) else soonest
                best.busy = True   # остаток интервала session выдержит сам внутри call()
                return best
            if deadline is not None and now >= deadline:
                raise TimeoutError("no available sessions")
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass

    def release(self, w: SessionWorker) -> None:
        w.busy = False
        self._changed.set()

    # ------------------------------------------------------------------ reporting
    def counts(self) -> Counter:
        c: Counter = Counter()
        for w in self.workers.values():
            c[w.status.value] += 1
        return c

    def summary_line(self) -> str:
        c = self.counts()
        return (f"total={len(self.workers)} active={c.get('ACTIVE', 0)} "
                f"flood={c.get('FLOOD_WAIT', 0)} connecting={c.get('CONNECTING', 0)} "
                f"error={c.get('ERROR', 0) + c.get('AUTH_ERROR', 0)}")

    def problem_workers(self) -> list[SessionWorker]:
        return [w for w in self.workers.values() if w.status != WorkerStatus.ACTIVE]
