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
            # json.dumps экранирует кавычки и спецсимволы — f-строка выше ломала валидный
            # JSON при исключениях с кавычками/бэкслешами в тексте.
            import json as _json
            return web.Response(text=_json.dumps({"error": str(e)}),
                                content_type="application/json", status=500)

    async def _metrics(request):
        """Prometheus text exposition format для внешнего мониторинга.
        Основные метрики: sessions, lookup, pipeline. Просто текст, без зависимости от prometheus_client.
        """
        try:
            lines = ["# gift-tracker metrics (plain-text, Prometheus-compatible)"]
            counts = app_ref.sessions.counts()
            # Sessions
            lines.append("# HELP tracker_sessions Count of sessions by status")
            lines.append("# TYPE tracker_sessions gauge")
            for status, n in counts.items():
                lines.append(f'tracker_sessions{{status="{status.lower()}"}} {n}')
            lines.append(f"tracker_sessions_total {len(app_ref.sessions.workers)}")
            lines.append(f"tracker_sessions_effective_capacity {app_ref.sessions.effective():.2f}")
            lines.append(f"tracker_floods_per_minute {app_ref.sessions.flood_rate_per_minute()}")
            # Mode
            mode = getattr(getattr(app_ref, "coordinator", None), "mode", "?")
            paused = int(getattr(getattr(app_ref, "coordinator", None), "paused", False))
            # Prometheus требует экранирования \\, \" и \n в значениях меток.
            # mode обычно "LIVE"/"CATALOG", но защитимся от неожиданного ввода.
            safe_mode = str(mode).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            lines.append("# HELP tracker_mode_paused 1 if paused")
            lines.append("# TYPE tracker_mode_paused gauge")
            lines.append(f'tracker_mode_paused{{mode="{safe_mode}"}} {paused}')
            # Pipeline
            p = getattr(getattr(app_ref, "pipeline", None), "stats", {}) or {}
            lines.append("# HELP tracker_listings Count of listings processed")
            lines.append("# TYPE tracker_listings counter")
            for k in ("new", "sent", "filtered_price", "filtered_seller", "filtered_repeat",
                      "duplicates", "failed", "owner_bot_dead_bypass"):
                lines.append(f'tracker_listings{{outcome="{k}"}} {p.get(k, 0)}')
            # Coordinator
            c = getattr(getattr(app_ref, "coordinator", None), "stats", {}) or {}
            lines.append(f"tracker_polls_total {c.get('polls', 0)}")
            lines.append(f"tracker_poll_errors_total {c.get('poll_errors', 0)}")
            lines.append(f"tracker_items_scanned_total {c.get('scanned', 0)}")
            lines.append(f"tracker_silent_total {c.get('silent', 0)}")
            lines.append(f"tracker_price_moves_total {c.get('price_moves', 0)}")
            # Lookup
            lk = getattr(getattr(app_ref, "lookup", None), "stats", {}) or {}
            lines.append("# HELP tracker_lookup Lookup operations")
            lines.append("# TYPE tracker_lookup counter")
            for k in ("lookups", "found", "not_found", "from_memory", "no_answer", "neg_cached",
                      "no_candidates", "too_many", "rate_limited", "resolve_limit",
                      "captcha", "captcha_auto", "captcha_random", "captcha_auto_fail",
                      "bot_unresolved", "bot_dead_marked", "no_bot_pool", "no_session"):
                lines.append(f'tracker_lookup{{op="{k}"}} {lk.get(k, 0)}')
            try:
                pending_cap = app_ref.lookup.pending_captchas() if hasattr(app_ref, "lookup") else 0
                pool_size = len(app_ref.lookup._bot_pool()) if hasattr(app_ref.lookup, "_bot_pool") else 0
                now_m = __import__("time").monotonic()
                dead_cnt = sum(1 for v in (app_ref.lookup._bot_dead_until or {}).values() if v > now_m)
                slow_cnt = sum(1 for v in (getattr(app_ref.lookup, "_bot_slow_until", {}) or {}).values()
                               if v > now_m)
                lines.append(f"tracker_lookup_active_captchas {pending_cap}")
                lines.append(f"tracker_lookup_bot_pool_size {pool_size}")
                lines.append(f"tracker_lookup_bot_dead_count {dead_cnt}")
                lines.append(f"tracker_lookup_bot_slow_count {slow_cnt}")
                lines.append(f"tracker_lookup_pending {app_ref.lookup.queue_size()}")
            except Exception:
                pass
            # Subscriptions (если есть)
            if hasattr(app_ref, "subs"):
                try:
                    sub = app_ref.subs.stats()
                    lines.append(f"tracker_subs_active {sub.get('active', 0)}")
                    lines.append(f"tracker_subs_paid_day_count {sub.get('paid_day_count', 0)}")
                    lines.append(f'tracker_subs_revenue_day_ton {sub.get("paid_day", 0):g}')
                except Exception:
                    pass
            lines.append("")  # trailing newline
            return web.Response(text="\n".join(lines), content_type="text/plain; version=0.0.4")
        except Exception as e:
            log.debug("health /metrics failed: %s", e)
            return web.Response(text=f"# error: {e!s}\n", content_type="text/plain", status=500)

    async def _root(request):
        return web.Response(text="OK — gift-tracker health endpoint. See /health, /stats, /metrics")

    srv = web.Application()
    srv.router.add_get("/", _root)
    srv.router.add_get("/health", _health)
    srv.router.add_get("/live", _health)
    srv.router.add_get("/stats", _stats)
    srv.router.add_get("/metrics", _metrics)
    runner = web.AppRunner(srv)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info("Health HTTP server started on 0.0.0.0:%d (/health /live /stats /metrics)", port)
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
