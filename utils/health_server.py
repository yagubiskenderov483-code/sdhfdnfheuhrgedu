"""Простой HTTP health endpoint для мониторинга со стороны хостинга (Render).

Эндпоинты:
  GET /health   — 200 OK если бот отвечает (всегда 200 кроме reserved=503)
  GET /live     — то же что /health
  GET /stats    — краткая JSON-сводка: аккаунты, mode, pending lookup
  GET /         — простой "OK"

Порт из env HEALTH_PORT (по умолчанию 8080). Если 0 — endpoint не запускается.
Включается только при настройке переменной: Render Background Worker должен
знать что HTTP endpoint есть, иначе endpoint не нужен и жрёт ресурс.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from utils.logger import get_logger

log = get_logger("health")


async def start_health_server(app_ref: Any, port: int) -> Any:
    """Запускает aiohttp-сервер с эндпоинтами. app_ref — экземпляр App (main.py).
    Возвращает runner (для graceful shutdown) или None при ошибке."""
    if port <= 0:
        return None
    try:
        from aiohttp import web
    except ImportError:
        log.warning("Health server: aiohttp не установлен — endpoint пропущен")
        return None

    async def _health(request):
        # Простой liveness-пинг: процесс жив и event-loop не завис.
        # Для Render достаточно 200 OK.
        return web.Response(text="OK", status=200)

    async def _stats(request):
        # Краткая JSON сводка для мониторинга. Не блокирует ничего, читает только счётчики.
        import json
        try:
            counts = app_ref.sessions.counts()
            effective = app_ref.sessions.effective()
            flood_min = app_ref.sessions.flood_rate_per_minute()
            mode = getattr(getattr(app_ref, "coordinator", None), "mode", "?")
            paused = getattr(getattr(app_ref, "coordinator", None), "paused", False)
            lk_stats = getattr(getattr(app_ref, "lookup", None), "stats", {}) or {}
            pending_cap = (app_ref.lookup.pending_captchas()
                           if hasattr(app_ref, "lookup") and app_ref.lookup else 0)
            payload = {
                "mode": mode,
                "paused": bool(paused),
                "sessions": {
                    "total": len(app_ref.sessions.workers),
                    "active": counts.get("ACTIVE", 0),
                    "flood": counts.get("FLOOD_WAIT", 0),
                    "error": counts.get("ERROR", 0) + counts.get("AUTH_ERROR", 0),
                    "effective_capacity": round(effective, 2),
                    "floods_per_min": flood_min,
                },
                "lookup": {
                    "lookups": lk_stats.get("lookups", 0),
                    "found": lk_stats.get("found", 0),
                    "not_found": lk_stats.get("not_found", 0),
                    "from_memory": lk_stats.get("from_memory", 0),
                    "pending": app_ref.lookup.queue_size() if hasattr(app_ref, "lookup") else 0,
                    "active_captchas": pending_cap,
                    "bot_dead_marked": lk_stats.get("bot_dead_marked", 0),
                    "bypass": (getattr(getattr(app_ref, "pipeline", None), "stats", {}) or {})
                              .get("owner_bot_dead_bypass", 0),
                },
            }
            return web.Response(text=json.dumps(payload), content_type="application/json")
        except Exception as e:
            log.debug("health /stats failed: %s", e)
            return web.Response(text=f'{{"error":"{e!s}"}}', content_type="application/json", status=500)

    async def _root(request):
        return web.Response(text="OK — gift-tracker health endpoint. See /health, /stats")

    srv = web.Application()
    srv.router.add_get("/", _root)
    srv.router.add_get("/health", _health)
    srv.router.add_get("/live", _health)
    srv.router.add_get("/stats", _stats)
    runner = web.AppRunner(srv)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info("Health HTTP server started on 0.0.0.0:%d (/health /live /stats)", port)
        return runner
    except Exception as e:
        log.warning("Health server could not start on port %d: %s", port, e)
        try:
            await runner.cleanup()
        except Exception:
            pass
        return None


def health_port_from_env() -> int:
    """Читает HEALTH_PORT из env. По умолчанию 0 (выключен).
    На Render при нужде задать HEALTH_PORT=8080."""
    raw = (os.environ.get("HEALTH_PORT") or "").strip()
    if not raw:
        return 0
    try:
        p = int(raw)
        if not (0 < p < 65536):
            return 0
        return p
    except ValueError:
        return 0
