"""
Асинхронный слой БД (SQLite через aiosqlite, WAL).

Глобальная дедупликация: listings.listing_id имеет UNIQUE-индекс,
insert_listing_if_new() делает INSERT OR IGNORE и возвращает True только тому,
кто вставил строку первым. Поэтому один и тот же лот не может быть обработан дважды,
даже если его одновременно увидели несколько sessions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from config import settings

from database.migrations import MIGRATIONS, REQUIRED_COLUMNS, SCHEMA_VERSION
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
        try:
            await self._migrate()
        except Exception:
            await self.conn.close()
            self.conn = None
            raise
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
        if current > SCHEMA_VERSION:
            log.warning("tracker.db schema v%s is newer than this code (v%s). Make a backup of %s "
                        "before continuing.", current, SCHEMA_VERSION, self.path.name)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS[version]:
                try:
                    await self.conn.execute(stmt)
                except aiosqlite.OperationalError as e:
                    # колонка уже есть (частично применённая миграция) — пропускаем
                    if "duplicate column" in str(e).lower():
                        continue
                    await self.conn.rollback()
                    raise RuntimeError(
                        f"DB migration v{version} failed: {e}. tracker.db НЕ удалена. Сделайте backup "
                        f"файла {self.path} и либо исправьте его, либо переименуйте, чтобы начать с "
                        f"чистой базы (будет выполнена новая initial sync).") from e
            await self.conn.execute(
                "INSERT INTO kv(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
            log.info("Applied DB migration v%s", version)
        await self.conn.commit()
        await self._check_schema()

    async def _check_schema(self) -> None:
        """Защита от старой/битой базы: без нужных колонок работать нельзя — но и не удаляем."""
        assert self.conn
        problems = []
        for table, cols in REQUIRED_COLUMNS.items():
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            have = {r[1] for r in await cur.fetchall()}
            missing = cols - have
            if missing:
                problems.append(f"{table}: нет колонок {sorted(missing)}")
        if problems:
            msg = ("tracker.db несовместима с текущей версией (" + "; ".join(problems) + "). "
                   f"База НЕ удалена. Сделайте backup {self.path} и переименуйте файл — "
                   "трекер создаст новую базу и выполнит тихую initial sync.")
            log.warning(msg)
            raise RuntimeError(msg)

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

    async def touch_market_items(self, unique_ids: Iterable[int]) -> None:
        """Отметить, что лоты всё ещё на рынке (якоря, которые не менялись)."""
        assert self.conn
        ids = list({int(u) for u in unique_ids})
        if not ids:
            return
        now = utcnow_iso()
        async with self._write_lock:
            await self.conn.executemany("UPDATE market_items SET last_seen=? WHERE unique_id=?",
                                        [(now, u) for u in ids])
            await self.conn.commit()

    async def housekeeping(self, keep_days: int = 14, market_days: int = 0) -> int:
        """Чистка: старые отсеянные лоты (не отправленные) и пустые служебные записи.
        Отправленные лоты не трогаем — по ним проверяется «этот продавец/NFT уже был»."""
        assert self.conn
        cutoff = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        async with self._write_lock:
            cur = await self.conn.execute(
                "DELETE FROM listings WHERE sent=0 AND status!='NEW' "
                "AND datetime(first_seen) < datetime(?, ?)", (cutoff, f"-{keep_days} days"))
            n = cur.rowcount or 0
            if market_days > 0:
                # лоты, которых давно нет в выдаче (проданы/сняты) — иначе таблица и снимок растут вечно
                cur = await self.conn.execute(
                    "DELETE FROM market_items WHERE datetime(last_seen) < datetime(?, ?)",
                    (cutoff, f"-{market_days} days"))
                n += cur.rowcount or 0
            await self.conn.execute("DELETE FROM kv WHERE value IS NULL")
            await self.conn.commit()
        return n

    # ------------------------------------------------------------------ market state
    async def lookup_market_items(self, unique_ids: Iterable[int]) -> dict[int, tuple[str, str, int | None, int | None]]:
        """{unique_id: (listing_id, seller_key, stars, ton_nano)} только для запрошенных NFT."""
        assert self.conn
        ids = list({int(u) for u in unique_ids})
        out: dict[int, tuple[str, str, int | None, int | None]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" for _ in chunk)
            async with self.conn.execute(
                    f"SELECT unique_id, listing_id, seller_key, stars, ton_nano FROM market_items "
                    f"WHERE unique_id IN ({q})", chunk) as cur:
                async for r in cur:
                    out[r["unique_id"]] = (r["listing_id"] or f"{r['unique_id']}:{r['seller_key']}",
                                           r["seller_key"], r["stars"], r["ton_nano"])
        return out

    async def upsert_market_items(self, rows: Iterable[tuple]) -> None:
        """rows: (unique_id, gift_id, listing_id, seller_key, stars, ton_nano, state)"""
        assert self.conn
        now = utcnow_iso()
        data = [(*r, now, now) for r in rows]
        if not data:
            return
        async with self._write_lock:
            await self.conn.executemany(
                """INSERT INTO market_items(unique_id, gift_id, listing_id, seller_key, stars, ton_nano,
                                            state, first_seen, last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(unique_id) DO UPDATE SET
                     gift_id=excluded.gift_id, listing_id=excluded.listing_id,
                     seller_key=excluded.seller_key, stars=excluded.stars, ton_nano=excluded.ton_nano,
                     state=excluded.state, last_seen=excluded.last_seen""", data)
            await self.conn.commit()

    async def count_market_items(self, gift_id: int | None = None) -> int:
        assert self.conn
        if gift_id is None:
            cur = await self.conn.execute("SELECT COUNT(*) FROM market_items")
        else:
            cur = await self.conn.execute("SELECT COUNT(*) FROM market_items WHERE gift_id=?", (gift_id,))
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
                       last_seen_listing_id, last_seen_timestamp, availability_resale, last_poll, last_session,
                       partial, last_count, baseline)
                   VALUES(:gift_id, :title, :synced, :complete, :last_cursor, :last_seen_listing_id,
                          :last_seen_timestamp, :availability_resale, :last_poll, :last_session,
                          :partial, :last_count, :baseline)
                   ON CONFLICT(gift_id) DO UPDATE SET
                     title=excluded.title, synced=excluded.synced, complete=excluded.complete,
                     last_cursor=excluded.last_cursor, last_seen_listing_id=excluded.last_seen_listing_id,
                     last_seen_timestamp=excluded.last_seen_timestamp,
                     availability_resale=excluded.availability_resale,
                     last_poll=excluded.last_poll, last_session=excluded.last_session,
                     partial=excluded.partial, baseline=excluded.baseline,
                     last_count=COALESCE(excluded.last_count, collection_state.last_count)""",
                {"partial": 0, "last_count": None, "baseline": 0, **s})
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

    async def gift_already_sent(self, gift_unique_id: int) -> bool:
        """Этот NFT уже отправлялся в канал (любым продавцом, по любой цене)."""
        assert self.conn
        cur = await self.conn.execute(
            "SELECT 1 FROM listings WHERE gift_unique_id=? AND sent=1 LIMIT 1", (gift_unique_id,))
        return (await cur.fetchone()) is not None

    async def seller_already_sent(self, seller_key: str, since_iso: str | None = None) -> bool:
        """Лот этого продавца уже отправлялся (с момента since_iso или вообще)."""
        assert self.conn
        if since_iso:
            cur = await self.conn.execute(
                "SELECT 1 FROM listings WHERE seller_key=? AND sent=1 AND sent_at >= ? LIMIT 1",
                (seller_key, since_iso))
        else:
            cur = await self.conn.execute(
                "SELECT 1 FROM listings WHERE seller_key=? AND sent=1 LIMIT 1", (seller_key,))
        return (await cur.fetchone()) is not None

    async def export_sent_history(self, limit: int = 200000) -> list[list]:
        """Отправленные листинги для переноса через деплой: [listing_id, gift_unique_id, gift_id, seller_key, sent_at]."""
        assert self.conn
        cur = await self.conn.execute(
            "SELECT listing_id, gift_unique_id, gift_id, seller_key, sent_at FROM listings "
            "WHERE sent=1 ORDER BY id DESC LIMIT ?", (limit,))
        return [list(r) for r in await cur.fetchall()]

    async def import_sent_history(self, rows: list[list]) -> int:
        assert self.conn
        now = utcnow_iso()
        data = [(r[0], r[1], r[2], r[3], r[4] or now, r[4] or now) for r in rows if len(r) >= 5]
        async with self._write_lock:
            cur = await self.conn.executemany(
                "INSERT OR IGNORE INTO listings(listing_id, gift_unique_id, gift_id, seller_key, sent_at, "
                "first_seen, status, sent, reason) VALUES(?,?,?,?,?,?,'SENT',1,'restored from backup')", data)
            await self.conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    async def export_market_snapshot(self) -> dict:
        assert self.conn
        cur = await self.conn.execute("SELECT unique_id, gift_id, seller_key, stars, ton_nano FROM market_items")
        items = [list(r) for r in await cur.fetchall()]
        return {"v": 1, "items": items, "collections": await self.load_collection_states()}

    async def import_market_snapshot(self, snap: dict) -> int:
        """Только в пустую базу (свежий деплой) — существующее состояние не перезаписываем."""
        assert self.conn
        if await self.count_market_items() > 0:
            return 0
        now = utcnow_iso()
        rows = [(u, g, f"{u}:{sk}", sk, st, t, "KNOWN", now, now) for u, g, sk, st, t in snap.get("items", [])]
        async with self._write_lock:
            await self.conn.executemany(
                "INSERT OR IGNORE INTO market_items(unique_id, gift_id, listing_id, seller_key, stars, ton_nano, "
                "state, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)", rows)
            await self.conn.commit()
        for c in snap.get("collections", []):
            row = {k: c.get(k) for k in (
                "gift_id", "title", "synced", "complete", "last_cursor", "last_seen_listing_id",
                "last_seen_timestamp", "availability_resale", "last_poll", "last_session", "last_count")}
            row["partial"] = int(c.get("partial") or 0)
            # снимок старше MAX_LOT_AGE: всё, что выставили за простой, уже не свежее → первый опрос тихий.
            # Короткий деплой (пара минут) — лоты за это время свежие, их отправляем.
            ts = snap.get("ts")
            old_snap = bool(ts) and settings.max_lot_age > 0 and time.time() - float(ts) > settings.max_lot_age
            row["baseline"] = 1 if old_snap else 0
            await self.save_collection_state(row)
        return len(rows)

    async def recent_listings(self, limit: int = 15) -> list[dict]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT gift_name, gift_num, stars_price, status, reason, seller_filter_result FROM listings "
            "WHERE reason IS NULL OR reason != 'restored from backup' ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    async def seller_reject_counts(self) -> dict[str, int]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT seller_filter_result, COUNT(*) FROM listings WHERE status='FILTERED_SELLER' "
            "GROUP BY seller_filter_result")
        return {str(r[0]): r[1] for r in await cur.fetchall()}

    async def status_counts(self) -> dict[str, int]:
        assert self.conn
        cur = await self.conn.execute("SELECT status, COUNT(*) FROM listings GROUP BY status")
        return {r[0]: r[1] for r in await cur.fetchall()}

    async def top_collections(self, since_iso: str, limit: int = 15) -> list[tuple[str, int, int]]:
        """[(коллекция, найдено новых, отправлено)] за период — самые активные первыми."""
        assert self.conn
        cur = await self.conn.execute(
            "SELECT gift_name, COUNT(*) AS n, SUM(CASE WHEN status='SENT' THEN 1 ELSE 0 END) AS s "
            "FROM listings WHERE first_seen >= ? GROUP BY gift_name ORDER BY n DESC LIMIT ?", (since_iso, limit))
        return [(r[0] or "?", int(r[1] or 0), int(r[2] or 0)) for r in await cur.fetchall()]

    async def collection_counts(self, gift_id: int, since_iso: str) -> dict[str, int]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) FROM listings WHERE gift_id=? AND first_seen >= ? GROUP BY status",
            (gift_id, since_iso))
        return {r[0]: r[1] for r in await cur.fetchall()}

    async def counts_since(self, since_iso: str) -> dict[str, int]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) FROM listings WHERE first_seen >= ? GROUP BY status", (since_iso,))
        return {r[0]: r[1] for r in await cur.fetchall()}
