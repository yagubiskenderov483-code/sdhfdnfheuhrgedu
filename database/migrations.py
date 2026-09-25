"""
Схема БД. SQL написан максимально переносимо (SQLite ↔ PostgreSQL):
при переходе на PostgreSQL достаточно заменить INTEGER PRIMARY KEY AUTOINCREMENT на BIGSERIAL
и драйвер в database/db.py (aiosqlite → asyncpg), интерфейс класса Database остаётся тем же.
"""
from __future__ import annotations

SCHEMA_VERSION = 4

MIGRATIONS: dict[int, list[str]] = {
    1: [
        # Каждый обработанный НОВЫЙ листинг (и отправленный, и отфильтрованный)
        """
        CREATE TABLE IF NOT EXISTS listings (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id            TEXT    NOT NULL,
            gift_unique_id        INTEGER NOT NULL,
            gift_id               INTEGER NOT NULL,
            gift_name             TEXT,
            gift_num              INTEGER,
            gift_slug             TEXT,
            gift_link             TEXT,
            model                 TEXT,
            backdrop              TEXT,
            symbol                TEXT,
            seller_key            TEXT,
            seller_id             INTEGER,
            seller_type           TEXT,
            seller_username       TEXT,
            seller_name           TEXT,
            seller_profile_link   TEXT,
            stars_price           INTEGER,
            ton_price             REAL,
            ton_nano              INTEGER,
            currency              TEXT,
            ton_only              INTEGER DEFAULT 0,
            level                 INTEGER,
            premium               INTEGER,
            messages              TEXT,
            seller_filter_result  TEXT,
            status                TEXT    NOT NULL,
            reason                TEXT,
            first_seen            TEXT    NOT NULL,
            listing_created_at    TEXT,
            sent                  INTEGER NOT NULL DEFAULT 0,
            sent_at               TEXT,
            message_id            INTEGER,
            session_source        TEXT
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_listings_listing_id ON listings(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_listings_status ON listings(status)",
        "CREATE INDEX IF NOT EXISTS ix_listings_first_seen ON listings(first_seen)",
        # Состояние рынка: какие коллекционные подарки сейчас (или недавно) были выставлены
        """
        CREATE TABLE IF NOT EXISTS market_items (
            unique_id   INTEGER PRIMARY KEY,
            gift_id     INTEGER NOT NULL,
            seller_key  TEXT    NOT NULL,
            stars       INTEGER,
            ton_nano    INTEGER,
            first_seen  TEXT    NOT NULL,
            last_seen   TEXT    NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_market_items_gift ON market_items(gift_id)",
        # Состояние по каждой коллекции (источнику)
        """
        CREATE TABLE IF NOT EXISTS collection_state (
            gift_id              INTEGER PRIMARY KEY,
            title                TEXT,
            synced               INTEGER NOT NULL DEFAULT 0,
            complete             INTEGER NOT NULL DEFAULT 0,
            last_cursor          TEXT,
            last_seen_listing_id TEXT,
            last_seen_timestamp  TEXT,
            availability_resale  INTEGER,
            last_poll            TEXT,
            last_session         TEXT
        )
        """,
        # key-value: режим трекера, счётчики, глобальный last_seen_*
        """
        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """,
    ],
    2: [
        "CREATE INDEX IF NOT EXISTS ix_listings_gift_sent ON listings(gift_unique_id, sent)",
        "CREATE INDEX IF NOT EXISTS ix_listings_seller_sent ON listings(seller_key, sent, sent_at)",
    ],
    # v3: каноническая идентичность листинга и его состояние в market_items
    3: [
        "ALTER TABLE market_items ADD COLUMN listing_id TEXT",
        "ALTER TABLE market_items ADD COLUMN state TEXT",
        "UPDATE market_items SET listing_id = unique_id || ':' || seller_key WHERE listing_id IS NULL",
        "UPDATE market_items SET state = 'KNOWN' WHERE state IS NULL",
        # старые данные собраны неполной синхронизацией (только верх рынка) → коллекции
        # нельзя считать complete, иначе ценовые изменения старых лотов примут за новые
        "UPDATE collection_state SET complete = 0",
    ],
    # v4: коллекции, которые Telegram отдаёт не целиком (прочитано заметно меньше, чем count)
    4: [
        "ALTER TABLE collection_state ADD COLUMN partial INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE collection_state ADD COLUMN last_count INTEGER",
    ],
}

# Колонки, без которых новая логика работать не может (проверяются после миграций)
REQUIRED_COLUMNS: dict[str, set[str]] = {
    "listings": {"listing_id", "gift_unique_id", "gift_id", "seller_key", "status", "sent", "sent_at",
                 "first_seen"},
    "market_items": {"unique_id", "gift_id", "seller_key", "stars", "ton_nano", "listing_id", "state"},
    "collection_state": {"gift_id", "synced", "complete", "last_cursor", "partial", "last_count"},
}
