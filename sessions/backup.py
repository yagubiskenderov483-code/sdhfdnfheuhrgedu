"""
Резервная копия в самом Telegram — чтобы после деплоя НИЧЕГО не пропадало
(на хостингах без постоянного диска папки telegram_sessions/ и data/ очищаются при каждом деплое).

Что сохраняется (зашифровано, Fernet):
  • все sessions (auth key + api_id/api_hash + кто добавил);
  • список админов;
  • история отправленных лотов (чтобы после деплоя не было повторов NFT / продавцов).

Где хранится — без ADMIN_IDS, без закрепов и без прав в канале:
  1. файл sessions.backup отправляется ботом в чат (личка того, кто добавил аккаунт,
     иначе личка админа, иначе канал TARGET_CHAT_ID);
  2. file_id этого файла бот записывает в СВОЁ описание для языка «yo»
     (setMyDescription(language_code="yo") — это описание видят только пользователи
     с интерфейсом на йоруба, т.е. фактически никто);
  3. при запуске бот читает getMyDescription(language_code="yo"), скачивает файл по file_id
     и восстанавливает всё до подключения sessions.

Ключ шифрования: SESSION_BACKUP_KEY, а если он не задан — производный от BOT_TOKEN.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Awaitable, Callable

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
POINTER_LANG = "yo"
POINTER_PREFIX = "gtb1|"


def _fernet() -> Fernet | None:
    key = settings.session_backup_key or settings.bot_token
    if not key:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("gift-tracker:" + key).encode()).digest()))


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
    def __init__(self, bot: Bot, storage: SessionStorage, admins_getter: Callable[[], set[int]],
                 admins_setter: Callable[[set[int]], None] | None = None,
                 history_export: Callable[[], Awaitable[list]] | None = None,
                 history_import: Callable[[list], Awaitable[int]] | None = None):
        self.bot = bot
        self.storage = storage
        self._admins = admins_getter
        self._set_admins = admins_setter
        self._history_export = history_export
        self._history_import = history_import
        self.backup_names: set[str] | None = None
        self._last_chat: int | None = None
        self._last_msg: int | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ pointer (bot description)
    async def _read_pointer(self) -> tuple[str, int | None, int | None] | None:
        try:
            d = await self.bot.get_my_description(language_code=POINTER_LANG)
        except Exception as e:
            log.warning("getMyDescription failed: %s", e)
            return None
        text = (d.description or "") if d else ""
        if not text.startswith(POINTER_PREFIX):
            return None
        parts = text[len(POINTER_PREFIX):].split("|")
        file_id = parts[0]
        chat = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
        msg = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        return file_id, chat, msg

    async def _write_pointer(self, file_id: str, chat: int, msg: int) -> None:
        await self.bot.set_my_description(description=f"{POINTER_PREFIX}{file_id}|{chat}|{msg}",
                                          language_code=POINTER_LANG)

    # ------------------------------------------------------------------ save
    def candidate_chats(self, hint: int | None = None) -> list[int]:
        out: list[int] = []
        for c in ([hint, settings.sessions_backup_chat_id, self._last_chat] + sorted(self._admins())
                  + [settings.target_chat_id]):
            if c and c not in out:
                out.append(c)
        return out

    async def save(self, chat_hint: int | None = None) -> bool:
        f = _fernet()
        if f is None:
            self.last_error = "нет BOT_TOKEN/SESSION_BACKUP_KEY"
            return False
        items = []
        for rec in self.storage.list_sessions():
            s = session_file_to_string(rec.path)
            if s:
                items.append({"name": rec.name, "string": s, "api_id": rec.api_id,
                              "api_hash": rec.api_hash, "owner_id": rec.owner_id})
        history = []
        if self._history_export:
            try:
                history = await self._history_export()
            except Exception as e:
                log.warning("Sent history export failed: %s", e)
        payload = {"v": 2, "sessions": items, "admins": sorted(self._admins()), "sent": history}
        blob = f.encrypt(json.dumps(payload).encode())

        if self._last_chat is None:
            ptr = await self._read_pointer()
            if ptr:
                _, self._last_chat, self._last_msg = ptr

        errors = []
        for chat in self.candidate_chats(chat_hint):
            try:
                msg = await self.bot.send_document(
                    chat, BufferedInputFile(blob, BACKUP_FILENAME),
                    caption=f"🔐 Служебная копия трекера: аккаунтов {len(items)}. Зашифрована, не удаляйте.",
                    disable_notification=True)
                await self._write_pointer(msg.document.file_id, chat, msg.message_id)
            except Exception as e:
                errors.append(f"{chat}: {e}")
                continue
            # удалить предыдущую копию (новая уже записана)
            if self._last_chat and self._last_msg and (self._last_chat, self._last_msg) != (chat, msg.message_id):
                try:
                    await self.bot.delete_message(self._last_chat, self._last_msg)
                except Exception:
                    pass
            self._last_chat, self._last_msg = chat, msg.message_id
            self.backup_names = {it["name"] for it in items}
            self.last_error = None
            log.info("Backup saved (chat %s): %s account(s), %s sent listing(s)", chat, len(items), len(history))
            return True
        self.last_error = "; ".join(errors)[:300] or "нет чата для копии"
        log.error("Backup failed: %s", self.last_error)
        return False

    async def sync_if_needed(self) -> None:
        local = {r.name for r in self.storage.list_sessions()}
        if local and (self.backup_names is None or not local <= self.backup_names):
            await self.save()

    # ------------------------------------------------------------------ restore
    async def restore(self) -> int:
        f = _fernet()
        ptr = await self._read_pointer()
        if f is None or ptr is None:
            log.info("No backup pointer found — nothing to restore")
            return 0
        file_id, self._last_chat, self._last_msg = ptr
        try:
            buf = io.BytesIO()
            await self.bot.download(file_id, destination=buf)
            data = json.loads(f.decrypt(buf.getvalue()))
        except InvalidToken:
            log.error("Backup: wrong key (BOT_TOKEN or SESSION_BACKUP_KEY changed?)")
            return 0
        except Exception as e:
            log.error("Backup download failed: %s", e)
            return 0

        if self._set_admins and data.get("admins"):
            self._set_admins(set(int(a) for a in data["admins"]))
        if self._history_import and data.get("sent"):
            try:
                n = await self._history_import(data["sent"])
                log.info("Restored %s sent listing(s) from backup (no repeats after deploy)", n)
            except Exception as e:
                log.warning("Sent history import failed: %s", e)

        restored = 0
        self.backup_names = {it.get("name") for it in data.get("sessions", [])}
        for it in data.get("sessions", []):
            path = self.storage.session_path(it["name"])
            if path.exists():
                continue
            try:
                string_to_session_file(it["string"], path)
                self.storage.save_meta(it["name"], int(it["api_id"]), it["api_hash"],
                                       owner_id=it.get("owner_id"))
                restored += 1
            except Exception as e:
                log.error("Restore %s failed: %s", it.get("name"), e)
        log.info("Restored %s session(s) from Telegram backup", restored)
        return restored
