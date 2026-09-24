"""
Асинхронный слой БД (SQLite через aiosqlite, WAL).

Глобальная дедупликация: listings.listing_id имеет UNIQUE-индекс,
insert_listing_if_new() делает INSERT OR IGNORE и возвращает True только тому,
кто вставил строку первым. Поэтому один и тот же лот не может быть обработан дважды,
даже если его одновременно увидели несколько sessions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from database.migrations import MIGRATIONS, SCHEMA_VERSION
from database.models import Listing, ListingStatus, utcnow_iso
from utils.logger import get_logger

log = get_logger("db")

_LISTING_COLUMNS = (
    "listing_id", "gift_unique_id", "gift_id", "gift_name", "gift_num", "gift_slug", "gift_link",
    "model", "backdrop", "symbol", "seller_key", "seller_id", "seller_type", "seller_username",
    "seller_name", "seller_profile_link", "stars_price", "ton_price", "ton_nano", "currency", "ton_only",
    "level", "premium", "messages", "seller_filter_result", "status", "reason", "first_seen",
    "listing_created_at", "sent", "message_id", "session_source",
)


def _listing_row(l: Listing) -> tuple:
    return (
        l.listing_id, l.gift_unique_id, l.gift_id, l.gift_name, l.gift_num, l.gift_slug, l.gift_link,
        l.model, l.backdrop, l.symbol, l.seller_key, l.seller_id, l.seller_type, l.seller_username,
        l.seller_name, l.seller_profile_link, l.stars_price, l.ton_price, l.ton_nano, l.currency,
        int(l.ton_only), l.level, None if l.premium is None else int(l.premium), l.messages,
        l.seller_filter_result, l.status, l.reason, l.first_seen, l.listing_created_at, int(l.sent),
        l.message_id, l.session_source,
    )


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self._migrate()
        log.info("Database ready: %s", self.path.name)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _migrate(self) -> None:
        assert self.conn
        await self.conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        cur = await self.conn.execute("SELECT value FROM kv WHERE key='schema_version'")
        row = await cur.fetchone()
        current = int(row[0]) if row else 0
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS[version]:
                await self.conn.execute(stmt)
            await self.conn.execute(
                "INSERT INTO kv(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
            log.info("Applied DB migration v%s", version)
        await self.conn.commit()

    # ------------------------------------------------------------------ kv
    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        assert self.conn
        cur = await self.conn.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

    async def kv_set_many(self, items: dict[str, Any]) -> None:
        assert self.conn
        async with self._write_lock:
            await self.conn.executemany(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, None if v is None else str(v)) for k, v in items.items()])
            await self.conn.commit()

    async def kv_set(self, key: str, value: Any) -> None:
        await self.kv_set_many({key: value})

    # ------------------------------------------------------------------ market state
    async def load_market_items(self) -> dict[int, dict[int, tuple[str, int | None, int | None]]]:
        """{gift_id: {unique_id: (seller_key, stars, ton_nano)}}"""
        assert self.conn
        out: dict[int, dict[int, tuple[str, int | None, int | None]]] = {}
        async with self.conn.execute(
                "SELECT unique_id, gift_id, seller_key, stars, ton_nano FROM market_items") as cur:
            async for r in cur:
                out.setdefault(r["gift_id"], {})[r["unique_id"]] = (r["seller_key"], r["stars"], r["ton_nano"])
        return out

    async def upsert_market_items(self, rows: Iterable[tuple[int, int, str, int | None, int | None]]) -> None:
        """rows: (unique_id, gift_id, seller_key, stars, ton_nano)"""
        assert self.conn
        now = utcnow_iso()
        data = [(u, g, s, st, t, now, now) for (u, g, s, st, t) in rows]
        if not data:
            return
        async with self._write_lock:
            await self.conn.executemany(
                """INSERT INTO market_items(unique_id, gift_id, seller_key, stars, ton_nano, first_seen, last_seen)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(unique_id) DO UPDATE SET
                     gift_id=excluded.gift_id, seller_key=excluded.seller_key, stars=excluded.stars,
                     ton_nano=excluded.ton_nano, last_seen=excluded.last_seen""", data)
            await self.conn.commit()

    async def count_market_items(self) -> int:
        assert self.conn
        cur = await self.conn.execute("SELECT COUNT(*) FROM market_items")
        return (await cur.fetchone())[0]

    # ------------------------------------------------------------------ collection state
    async def load_collection_states(self) -> list[dict[str, Any]]:
        assert self.conn
        cur = await self.conn.execute("SELECT * FROM collection_state")
        return [dict(r) for r in await cur.fetchall()]

    async def save_collection_state(self, s: dict[str, Any]) -> None:
        assert self.conn
        async with self._write_lock:
            await self.conn.execute(
                """INSERT INTO collection_state(gift_id, title, synced, complete, last_cursor,
                       last_seen_listing_id, last_seen_timestamp, availability_resale, last_poll, last_session)
                   VALUES(:gift_id, :title, :synced, :complete, :last_cursor, :last_seen_listing_id,
                          :last_seen_timestamp, :availability_resale, :last_poll, :last_session)
                   ON CONFLICT(gift_id) DO UPDATE SET
                     title=excluded.title, synced=excluded.synced, complete=excluded.complete,
                     last_cursor=excluded.last_cursor, last_seen_listing_id=excluded.last_seen_listing_id,
                     last_seen_timestamp=excluded.last_seen_timestamp,
                     availability_resale=excluded.availability_resale,
                     last_poll=excluded.last_poll, last_session=excluded.last_session""", s)
            await self.conn.commit()

    # ------------------------------------------------------------------ listings
    async def insert_listing_if_new(self, l: Listing) -> bool:
        """Атомарно: True — лот новый и вставлен; False — уже был (дубликат)."""
        assert self.conn
        placeholders = ",".join("?" for _ in _LISTING_COLUMNS)
        async with self._write_lock:
            cur = await self.conn.execute(
                f"INSERT OR IGNORE INTO listings({','.join(_LISTING_COLUMNS)}) VALUES({placeholders})",
                _listing_row(l))
            await self.conn.commit()
            return cur.rowcount == 1

    async def update_listing(self, l: Listing) -> None:
        assert self.conn
        cols = [c for c in _LISTING_COLUMNS if c != "listing_id"]
        values = dict(zip(_LISTING_COLUMNS, _listing_row(l)))
        sets = ", ".join(f"{c}=?" for c in cols)
        params = [values[c] for c in cols]
        extra = ""
        if l.sent:
            extra = ", sent_at=COALESCE(sent_at, ?)"
            params.append(utcnow_iso())
        params.append(l.listing_id)
        async with self._write_lock:
            await self.conn.execute(f"UPDATE listings SET {sets}{extra} WHERE listing_id=?", params)
            await self.conn.commit()

    async def listing_exists(self, listing_id: str) -> bool:
        assert self.conn
        cur = await self.conn.execute("SELECT 1 FROM listings WHERE listing_id=?", (listing_id,))
        return (await cur.fetchone()) is not None

    async def unfinished_listings(self) -> list[dict[str, Any]]:
        """Лоты, которые были вставлены, но процесс упал до финального статуса."""
        assert self.conn
        cur = await self.conn.execute("SELECT * FROM listings WHERE status=?", (ListingStatus.NEW,))
        return [dict(r) for r in await cur.fetchall()]

    async def last_listing(self) -> dict[str, Any] | None:
        assert self.conn
        cur = await self.conn.execute("SELECT * FROM listings ORDER BY id DESC LIMIT 1")
        r = await cur.fetchone()
        return dict(r) if r else None

    async def last_sent_listing(self) -> dict[str, Any] | None:
        assert self.conn
        cur = await self.conn.execute("SELECT * FROM listings WHERE sent=1 ORDER BY id DESC LIMIT 1")
        r = await cur.fetchone()
        return dict(r) if r else None

    async def status_counts(self) -> dict[str, int]:
        assert self.conn
        cur = await self.conn.execute("SELECT status, COUNT(*) FROM listings GROUP BY status")
        return {r[0]: r[1] for r in await cur.fetchall()}

    async def counts_since(self, since_iso: str) -> dict[str, int]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) FROM listings WHERE first_seen >= ? GROUP BY status", (since_iso,))
        return {r[0]: r[1] for r in await cur.fetchall()}
