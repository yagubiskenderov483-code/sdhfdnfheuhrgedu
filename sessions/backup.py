"""
Резервная копия sessions в самом Telegram — чтобы аккаунты НЕ пропадали после деплоя
(на хостингах без постоянного диска папка telegram_sessions/ очищается при каждом деплое).

Как работает:
  • после каждого /add_session и /remove_session бот собирает все sessions
    (auth key + api_id/api_hash), шифрует (Fernet, ключ = SESSION_BACKUP_KEY из .env)
    и отправляет файлом sessions.backup в SESSIONS_BACKUP_CHAT_ID (по умолчанию —
    личка первого админа), затем закрепляет это сообщение;
  • при запуске, до подключения sessions, бот берёт закреплённое сообщение этого чата
    (getChat → pinned_message), скачивает файл, расшифровывает и восстанавливает
    недостающие *.session. Количество аккаунтов не ограничено.

Без SESSION_BACKUP_KEY бэкап не делается (зашифровать нечем).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile
from cryptography.fernet import Fernet, InvalidToken
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from config import settings
from sessions.storage import SessionStorage
from utils.logger import get_logger

log = get_logger("backup")
BACKUP_FILENAME = "sessions.backup"


def _fernet() -> Fernet | None:
    key = settings.session_backup_key
    if not key:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def session_file_to_string(path: Path) -> str | None:
    """*.session (SQLite Telethon) → StringSession. Читаем только auth key и DC."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = con.execute("SELECT dc_id, server_address, port, auth_key FROM sessions").fetchone()
        con.close()
    except sqlite3.Error:
        return None
    if not row or not row[3]:
        return None
    ss = StringSession()
    ss.set_dc(row[0], row[1], row[2])
    ss.auth_key = AuthKey(row[3])
    return ss.save()


def string_to_session_file(string: str, path: Path) -> None:
    src = StringSession(string)
    dst = SQLiteSession(str(path.with_suffix("")))
    dst.set_dc(src.dc_id, src.server_address, src.port)
    dst.auth_key = src.auth_key
    dst.save()
    dst.close()


class SessionBackup:
    def __init__(self, bot: Bot, storage: SessionStorage, admins_getter):
        self.bot = bot
        self.storage = storage
        self._admins = admins_getter
        self.backup_names: set[str] | None = None   # имена sessions в закреплённой копии

    def chat_id(self) -> int | None:
        if settings.sessions_backup_chat_id:
            return settings.sessions_backup_chat_id
        return next(iter(sorted(self._admins())), None)

    async def save(self) -> bool:
        f, chat = _fernet(), self.chat_id()
        if f is None or chat is None:
            log.warning("Session backup skipped: set SESSION_BACKUP_KEY and ADMIN_IDS (or claim admin)")
            return False
        items = []
        for rec in self.storage.list_sessions():
            s = session_file_to_string(rec.path)
            if s:
                items.append({"name": rec.name, "string": s, "api_id": rec.api_id, "api_hash": rec.api_hash})
        blob = f.encrypt(json.dumps({"v": 1, "sessions": items}).encode())
        try:
            msg = await self.bot.send_document(
                chat, BufferedInputFile(blob, BACKUP_FILENAME),
                caption=f"🔐 Резервная копия аккаунтов трекера: {len(items)} шт.\n"
                        "Не удаляйте и не открепляйте это сообщение.",
                disable_notification=True)
            try:
                await self.bot.unpin_all_chat_messages(chat)
            except Exception:
                pass
            await self.bot.pin_chat_message(chat, msg.message_id, disable_notification=True)
            self.backup_names = {it["name"] for it in items}
            log.info("Sessions backup saved: %s account(s)", len(items))
            return True
        except Exception as e:
            log.error("Sessions backup failed: %s", e)
            return False

    async def sync_if_needed(self) -> None:
        """Сохранить копию, если локальных sessions нет в закреплённой копии."""
        local = {r.name for r in self.storage.list_sessions()}
        if local and (self.backup_names is None or not local <= self.backup_names):
            await self.save()

    async def restore(self) -> int:
        f, chat = _fernet(), self.chat_id()
        if f is None or chat is None:
            return 0
        try:
            info = await self.bot.get_chat(chat)
            pinned = info.pinned_message
            if not pinned or not pinned.document or pinned.document.file_name != BACKUP_FILENAME:
                log.info("No sessions backup pinned in backup chat")
                return 0
            buf = io.BytesIO()
            await self.bot.download(pinned.document, destination=buf)
            data = json.loads(f.decrypt(buf.getvalue()))
        except InvalidToken:
            log.error("Sessions backup: wrong SESSION_BACKUP_KEY")
            return 0
        except Exception as e:
            log.error("Sessions backup restore failed: %s", e)
            return 0
        restored = 0
        self.backup_names = {it.get("name") for it in data.get("sessions", [])}
        for it in data.get("sessions", []):
            path = self.storage.session_path(it["name"])
            if path.exists():
                continue
            try:
                string_to_session_file(it["string"], path)
                self.storage.save_meta(it["name"], int(it["api_id"]), it["api_hash"])
                restored += 1
            except Exception as e:
                log.error("Restore %s failed: %s", it.get("name"), e)
        if restored:
            log.info("Restored %s session(s) from Telegram backup", restored)
        return restored
