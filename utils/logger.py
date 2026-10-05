"""
Логирование:
  logs/app.log       — всё (INFO+)
  logs/errors.log    — WARNING+
  logs/listings.log  — только события по листингам (NEW / PRICE / FILTER / SEND / DUP)

Формат: [01:20:10] [NEW] Listing #...
Секреты (API_HASH, BOT_TOKEN и т.п.) вычищаются из любых сообщений фильтром.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LISTINGS_LOGGER = "listings"
_secrets: set[str] = set()


class _TagFilter(logging.Filter):
    """Добавляет record.tag (по умолчанию = уровень)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "tag"):
            record.tag = record.levelname
        return True


class _SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _secrets:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            changed = False
            for s in _secrets:
                if s and s in msg:
                    msg = msg.replace(s, "***")
                    changed = True
            if changed:
                record.msg, record.args = msg, ()
            # текст исключения (traceback) тоже может содержать токен — например URL Bot API
            if record.exc_info:
                try:
                    text = logging.Formatter().formatException(record.exc_info)
                    for s in _secrets:
                        if s:
                            text = text.replace(s, "***")
                    record.exc_text, record.exc_info = text, None
                except Exception:
                    pass
        return True


class _OnlyListings(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == LISTINGS_LOGGER


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _secrets.add(value)


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("[%(asctime)s] [%(tag)s] %(message)s", "%H:%M:%S")
    file_fmt = logging.Formatter("[%(asctime)s] [%(tag)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level, logging.INFO))

    def _mk(handler: logging.Handler, formatter: logging.Formatter, lvl: int) -> logging.Handler:
        handler.setLevel(lvl)
        handler.setFormatter(formatter)
        handler.addFilter(_TagFilter())
        handler.addFilter(_SecretFilter())
        return handler

    lvl = root.level                      # LOG_LEVEL=DEBUG действительно включает отладку
    console = _mk(logging.StreamHandler(sys.stdout), fmt, lvl)
    app = _mk(RotatingFileHandler(logs_dir / "app.log", maxBytes=20_000_000, backupCount=5, encoding="utf-8"),
              file_fmt, lvl)
    errors = _mk(RotatingFileHandler(logs_dir / "errors.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8"),
                 file_fmt, logging.WARNING)
    listings = _mk(RotatingFileHandler(logs_dir / "listings.log", maxBytes=20_000_000, backupCount=5, encoding="utf-8"),
                   fmt, logging.INFO)
    listings.addFilter(_OnlyListings())

    for h in (console, app, errors, listings):
        root.addHandler(h)

    # шумные библиотеки
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def llog(tag: str, msg: str, level: int = logging.INFO) -> None:
    """Лог событий по листингам: llog('NEW', 'Listing #123')."""
    logging.getLogger(LISTINGS_LOGGER).log(level, msg, extra={"tag": tag})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
