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

import asyncio
import base64
import hashlib
import io
import json
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.types import BufferedInputFile
from cryptography.fernet import Fernet, InvalidToken
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from config import export_runtime_overrides, import_runtime_overrides, settings
from sessions.storage import SessionStorage
from utils.logger import get_logger

log = get_logger("backup")


class BackupUnavailable(Exception):
    """Не удалось прочитать указатель на копию — нельзя ни восстанавливать, ни перезаписывать."""


class BackupCorrupt(BackupUnavailable):
    """Файл скачался, но не расшифровался (неверный ключ или битый шифротекст).
    Ретрай переживёт транзиентный обрыв; если повторяется — ключ реально сменён/копия испорчена."""
BACKUP_FILENAME = "sessions.backup"
POINTER_LANG = "yo"
POINTER_PREFIX = "gtb1|"
POINTER_PREFIX2 = "gtb2"
MARKET_FILENAME = "market.backup"
MAX_BOT_DOWNLOAD = 19 * 1024 * 1024       # Bot API отдаёт файлы до 20 МБ


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
        self._ptr: dict | None = None
        self._ptr_lock = asyncio.Lock()  # read-modify-write указателя без гонки s/m ключей
        self.restore_done = False       # пока восстановление не завершено, копию НЕ перезаписываем
        self.restored_count = 0
        self.market_export: Callable[[], Awaitable[dict]] | None = None
        self.market_import: Callable[[dict], Awaitable[int]] | None = None
        self.paused_getter: Callable[[], bool] | None = None
        self.subs_export: Callable[[], dict] | None = None
        self.flags: dict = {}                         # разовые миграции, уже выполненные (переживают деплой)
        self._save_lock = asyncio.Lock()
        self.subs_import: Callable[[dict], Awaitable[int]] | None = None
        self.restored_paused: bool = False
        # удалённые аккаунты (снёс Telegram или удалили вручную) — хранятся в копии 14 дней,
        # чтобы ошибочно удалённый аккаунт можно было вернуть командой /revive
        self.graveyard: dict[str, dict] = {}
        self._prev_items: dict[str, dict] = {}      # аккаунты из последней копии (имя → запись)

    GRAVE_DAYS = 14
    GRAVE_MAX = 300

    def bury(self, name: str, reason: str | None) -> bool:
        """Запомнить аккаунт перед удалением (вызывать ДО storage.remove)."""
        rec = next((r for r in self.storage.list_sessions() if r.name == name), None)
        prev = self._prev_items.get(name) or {}
        # файл может быть временно не читаем (SQLite занят) — тогда берём ключ из последней копии
        string = (session_file_to_string(rec.path) if rec else None) or prev.get("string")
        if not string:
            return False
        old = self.graveyard.get(name)
        if old and old.get("string") != string:
            # то же имя, но ДРУГОЙ аккаунт (удалён раньше) — не затираем его запись
            alt = f"{name}~{old.get('ts', 0)}"
            self.graveyard[alt] = dict(old, name=alt)
        self.graveyard[name] = {"name": name, "string": string,
                                "api_id": rec.api_id if rec else prev.get("api_id"),
                                "api_hash": rec.api_hash if rec else prev.get("api_hash"),
                                "owner_id": rec.owner_id if rec else prev.get("owner_id"),
                                "reason": (reason or "")[:200], "ts": int(time.time())}
        self._trim_graveyard()
        return True

    def _trim_graveyard(self) -> None:
        cutoff = time.time() - self.GRAVE_DAYS * 86400
        items = sorted((g for g in self.graveyard.values() if g.get("ts", 0) >= cutoff),
                       key=lambda g: g.get("ts", 0), reverse=True)[:self.GRAVE_MAX]
        self.graveyard = {g["name"]: g for g in items}

    def unbury(self, name: str) -> str | None:
        """Вернуть аккаунт из удалённых в папку sessions. → имя, под которым он восстановлен."""
        g = self.graveyard.get(name)
        if not g:
            return None
        for rec in self.storage.list_sessions():
            if session_file_to_string(rec.path) == g["string"]:
                # уже на диске (копия вернула его в «удалённые») — второй файл с тем же auth key
                # Telegram убьёт обе сессии (AUTH_KEY_DUPLICATED)
                self.graveyard.pop(name, None)
                return rec.name
        prev = self._prev_items.get(name)
        clash = (self.storage.session_path(name).exists() or "~" in name
                 or (prev is not None and prev.get("string") != g["string"]))
        target = self._free_name() if clash else name
        string_to_session_file(g["string"], self.storage.session_path(target))
        self.storage.save_meta(target, int(g["api_id"]), g["api_hash"], owner_id=g.get("owner_id"))
        self.graveyard.pop(name, None)
        return target

    def _free_name(self) -> str:
        """Имя, не занятое ни на диске, ни в копии, ни среди удалённых."""
        taken = set(self.backup_names or ()) | set(self.graveyard)
        name = self.storage.next_name()
        while name in taken or self.storage.session_path(name).exists():
            name = f"session_{int(name.rsplit('_', 1)[1]) + 1:03d}"
        return name

    # ------------------------------------------------------------------ pointer (bot description)
    async def _read_pointer(self) -> dict:
        """{"s": [file_id, chat, msg], "m": [file_id, chat, msg, ts]} — где лежат копии."""
        if self._ptr is not None:
            return self._ptr
        d = None
        for attempt in range(3):
            try:
                d = await self.bot.get_my_description(language_code=POINTER_LANG)
                break
            except Exception as e:
                log.warning("getMyDescription failed (attempt %s): %s", attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        else:
            raise BackupUnavailable("cannot read backup pointer from Telegram")
        text = (d.description or "") if d else ""
        ptr: dict = {}
        if text.startswith(POINTER_PREFIX2):
            try:
                ptr = json.loads(text[len(POINTER_PREFIX2):])
            except json.JSONDecodeError:
                ptr = {}
        elif text.startswith(POINTER_PREFIX):          # старый формат
            parts = text[len(POINTER_PREFIX):].split("|")
            chat = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
            msg = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            ptr = {"s": [parts[0], chat, msg]}
        self._ptr = ptr
        return ptr

    async def _write_pointer(self, key: str, value: list) -> None:
        # Лок на весь read-modify-write: без него параллельные save() (ключ "s") и save_market()
        # ("m") на точке await внутри set_my_description теряют один из ключей.
        async with self._ptr_lock:
            ptr = dict(await self._read_pointer())  # BackupUnavailable → не затираем чужие ключи
            ptr[key] = value
            text = POINTER_PREFIX2 + json.dumps(ptr, separators=(",", ":"))
            for attempt in range(3):
                try:
                    await self.bot.set_my_description(description=text, language_code=POINTER_LANG)
                    break
                except TelegramRetryAfter as e:      # описание бота часто менять нельзя — подождать
                    if attempt == 2 or e.retry_after > 120:
                        raise
                    await asyncio.sleep(e.retry_after + 1)
            self._ptr = ptr

    # ------------------------------------------------------------------ save
    def candidate_chats(self, hint: int | None = None) -> list[int]:
        out: list[int] = []
        # общий канал — только в самом крайнем случае (подписчикам служебные файлы ни к чему)
        for c in ([hint, settings.sessions_backup_chat_id] + sorted(self._admins())
                  + [self._last_chat, settings.target_chat_id]):
            if c and c not in out:
                out.append(c)
        return out

    async def save(self, chat_hint: int | None = None, allow_shrink: bool = False) -> bool:
        async with self._save_lock:        # одновременные сохранения не перетирают друг друга
            return await self._save(chat_hint, allow_shrink)

    async def _save(self, chat_hint: int | None = None, allow_shrink: bool = False) -> bool:
        """allow_shrink=True — только при удалении аккаунта: разрешает сохранить копию с меньшим
        числом аккаунтов, чем в прошлой. В остальных случаях меньшая копия НЕ перезаписывает старую
        (защита от затирания, если аккаунты ещё не восстановились или файл временно недоступен)."""
        f = _fernet()
        if f is None:
            self.last_error = "нет BOT_TOKEN/SESSION_BACKUP_KEY"
            return False
        if not self.restore_done:
            self.last_error = "аккаунты ещё восстанавливаются после запуска — копия сохранится позже"
            log.info("Backup skipped: restore not finished yet")
            return False
        items = []
        for rec in self.storage.list_sessions():
            s = session_file_to_string(rec.path)
            if s:
                items.append({"name": rec.name, "string": s, "api_id": rec.api_id,
                              "api_hash": rec.api_hash, "owner_id": rec.owner_id, "proxy": rec.proxy})
        if self.backup_names:
            # каких-то аккаунтов сейчас нет на диске (не успели восстановиться / файл недоступен) —
            # не теряем их и не отказываемся от сохранения (иначе не сохранились бы оплаты подписок):
            # берём их из прошлой копии как есть
            have = {it["name"] for it in items}
            gone = set(self.graveyard)
            if allow_shrink:
                # удаление: отбрасываем только то, чего реально нет на диске; файл, который сейчас
                # не прочитался (SQLite занят), берём из прошлой копии, а не теряем
                gone |= {n for n in self.backup_names - have if not self.storage.session_path(n).exists()}
            have_s = {it["string"] for it in items}
            missing = [self._prev_items[n] for n in sorted(self.backup_names - have - gone)
                       if n in self._prev_items and self._prev_items[n].get("string") not in have_s]
            if missing:
                log.info("Backup: %s account(s) not on disk now — kept from the previous copy", len(missing))
                items += missing
        history = []
        if self._history_export:
            try:
                history = await self._history_export()
            except Exception as e:
                log.warning("Sent history export failed: %s", e)
        payload = {"v": 2, "sessions": items, "admins": sorted(self._admins()), "sent": history,
                   "proxies": self.storage.load_pool(),
                   "settings": export_runtime_overrides(),
                   "paused": bool(self.paused_getter()) if self.paused_getter else False,
                   "subs": self.subs_export() if self.subs_export else {},
                   "flags": self.flags,
                   "removed": list(self.graveyard.values())}
        # сжатие + потолок размера: Bot API отдаёт на скачивание только файлы до 20 МБ — больший файл
        # загрузится, но при деплое его уже не скачать (все аккаунты и подписки пропали бы)
        def _pack(p: dict) -> bytes:
            return f.encrypt(b"Z1" + zlib.compress(json.dumps(p, separators=(",", ":")).encode(), 6))
        blob = await asyncio.to_thread(_pack, payload)
        while len(blob) > MAX_BOT_DOWNLOAD and payload["sent"]:
            payload["sent"] = payload["sent"][: len(payload["sent"]) // 2]     # свежие — первыми, режем старые
            log.warning("Backup too big — keeping only %s newest sent listings", len(payload["sent"]))
            blob = await asyncio.to_thread(_pack, payload)

        if self._last_chat is None:
            try:
                ptr = (await self._read_pointer()).get("s")
            except BackupUnavailable as e:
                self.last_error = str(e)
                return False
            if ptr:
                _, self._last_chat, self._last_msg = ptr[:3]

        errors = []
        for chat in self.candidate_chats(chat_hint):
            try:
                msg = await self.bot.send_document(
                    chat, BufferedInputFile(blob, BACKUP_FILENAME),
                    caption=f"🔐 Служебная копия трекера: аккаунтов {len(items)}. Зашифрована, не удаляйте.",
                    disable_notification=True)
            except (TelegramForbiddenError, TelegramBadRequest, TelegramNotFound) as e:
                errors.append(f"{chat}: {e}")      # этот чат копию не принимает — пробуем следующий
                continue
            except Exception as e:
                # сеть/таймаут/флуд: файл МОГ уйти — не рассылаем копию по другим чатам
                self.last_error = f"{chat}: {e}"[:300]
                log.warning("Backup upload to %s failed: %s", chat, e)
                return False
            try:
                await self._write_pointer("s", [msg.document.file_id, chat, msg.message_id])
            except Exception as e:
                # запрос мог дойти до Telegram (таймаут после применения) — перечитываем указатель
                self._ptr = None
                try:
                    cur = (await self._read_pointer()).get("s")
                except Exception:
                    cur = False                   # неизвестно — НЕ удаляем: указатель может вести сюда
                if not (cur and cur[0] == msg.document.file_id):
                    if cur is not False:
                        try:
                            await self.bot.delete_message(chat, msg.message_id)
                        except Exception:
                            pass
                    self.last_error = f"pointer: {e}"
                    log.warning("Backup pointer write failed: %s", e)
                    return False
            # удалить предыдущую копию (новая уже записана)
            if self._last_chat and self._last_msg and (self._last_chat, self._last_msg) != (chat, msg.message_id):
                try:
                    await self.bot.delete_message(self._last_chat, self._last_msg)
                except Exception:
                    pass
            self._last_chat, self._last_msg = chat, msg.message_id
            self.backup_names = {it["name"] for it in items}
            self._prev_items = {it["name"]: it for it in items}
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
        last_corrupt = False
        digests: list[str] = []
        for attempt in range(10):
            try:
                n = await self._restore_once()
                self.restored_count = n
                self.restore_done = True
                return n
            except BackupCorrupt as e:
                # битая закачка резолвится ретраем; если нет — ключ реально сменён (см. ниже)
                last_corrupt = True
                digests.append(getattr(e, "digest", None) or f"?{attempt}")
                log.warning("Backup decrypt failed: %s (retry %s/10)", e, attempt + 1)
                await asyncio.sleep(10)
            except BackupUnavailable as e:
                last_corrupt = False
                log.warning("Backup restore delayed: %s (retry %s/10)", e, attempt + 1)
                await asyncio.sleep(10)
        if last_corrupt and len(digests) >= 3 and len(set(digests)) == 1:
            # 10 попыток скачали файл, но расшифровать не смогли — это не транзиент, а сменённый
            # ключ/испорченная копия. Старую копию мы всё равно не прочитаем, поэтому разрешаем
            # старт «с чистого листа»: сохранение включаем, чтобы новые аккаунты попали в свежую копию.
            log.error("Backup permanently undecryptable (key changed?) — starting fresh, saving ENABLED")
            self.restore_done = True
            self.backup_names = set()
            # старую копию не удаляем и указатель на неё сохраняем — вдруг ключ вернут
            if self._ptr and self._ptr.get("s"):
                self._ptr["s_old"] = self._ptr.pop("s")     # иначе первое сохранение удалило бы старую копию
            self._last_chat = self._last_msg = None
            return 0
        log.error("Backup could not be read (download failing) — saving disabled this run to protect the old copy")
        return 0

    async def _restore_once(self) -> int:
        f = _fernet()
        own = await self._read_pointer()
        ptr = own.get("s")
        src_bot = self.bot
        old_bot = None
        old_id = (settings.old_bot_token or "").split(":", 1)[0]
        if (settings.old_bot_token and settings.old_bot_token != settings.bot_token
                and str(own.get("mig") or "") != old_id):
            # Сменили бота: свежая копия лежит у прошлого бота (OLD_BOT_TOKEN). Берём её оттуда ОДИН раз,
            # даже если у нового бота есть своя (возможно, давно устаревшая) копия. После переноса
            # в указателе нового бота ставится отметка mig — следующие деплои читают уже свою копию.
            try:
                old_bot = Bot(settings.old_bot_token)
                d = await old_bot.get_my_description(language_code=POINTER_LANG)
                text = (d.description or "") if d else ""
                old_ptr = None
                if text.startswith(POINTER_PREFIX2):
                    old_ptr = (json.loads(text[len(POINTER_PREFIX2):]) or {}).get("s")
                if old_ptr:
                    ptr = old_ptr
                    src_bot = old_bot
                    log.info("Backup found on the previous bot (OLD_BOT_TOKEN) — migrating accounts")
            except Exception as e:
                log.warning("Could not read backup from the previous bot: %s", e)
        if f is None or not ptr:
            if old_bot:
                await old_bot.session.close()
            log.info("No backup pointer found — nothing to restore")
            return 0
        file_id = ptr[0]
        if src_bot is self.bot:
            _, self._last_chat, self._last_msg = ptr[:3]
        try:
            buf = io.BytesIO()
            await src_bot.download(file_id, destination=buf)
            raw = f.decrypt(buf.getvalue())
            data = json.loads(zlib.decompress(raw[2:]) if raw[:2] == b"Z1" else raw)   # Z1 = сжатая копия
        except InvalidToken as e:
            # одинаковые байты на всех попытках = реально другой ключ; разные = битая закачка
            # InvalidToken = неверный ключ ЛИБО оборванная/битая закачка. НЕ помечаем restore
            # выполненным здесь (иначе save() снял бы защиту и затёр хорошую копию пустой на
            # транзиенте). BackupCorrupt → restore() ретраит; если повторяется все 10 раз — это
            # реальная смена ключа, и restore() тогда разрешит старт заново (не блок навсегда).
            err = BackupCorrupt(f"decrypt failed (wrong key or truncated download): {e}")
            err.digest = hashlib.sha256(buf.getvalue()).hexdigest()
            raise err from e
        except Exception as e:
            raise BackupUnavailable(f"backup download failed: {e}") from e
        finally:
            if old_bot:
                await old_bot.session.close()

        self.restored_paused = bool(data.get("paused"))
        # удалённые В ЭТОМ запуске (до чтения копии) — их не возвращаем; список удалённых из самой копии
        # тут не учитываем: в старой копии аккаунт мог быть и в удалённых, и уже возвращённым
        buried = {g.get("string") for g in self.graveyard.values()}
        if isinstance(data.get("removed"), list):
            for g in data["removed"]:
                if isinstance(g, dict) and g.get("name") and g.get("string"):
                    self.graveyard.setdefault(g["name"], g)
            self._trim_graveyard()
        if isinstance(data.get("flags"), dict):
            mine_muted = set(self.flags.get("muted") or [])
            for k, v in data["flags"].items():
                self.flags.setdefault(k, v)        # изменённое уже в этом запуске (например /mute) не затираем
            if mine_muted:
                self.flags["muted"] = sorted(mine_muted | set(data["flags"].get("muted") or []))
        if self._set_admins and data.get("admins"):
            self._set_admins(set(int(a) for a in data["admins"]))
        if self._history_import and data.get("sent"):
            try:
                n = await self._history_import(data["sent"])
                log.info("Restored %s sent listing(s) from backup (no repeats after deploy)", n)
            except Exception as e:
                log.warning("Sent history import failed: %s", e)

        if self.subs_import and data.get("subs"):
            try:
                n = await self.subs_import(data["subs"])
                log.info("Restored %s subscriber(s) from backup", n)
            except Exception as e:
                log.warning("Subscriptions import failed: %s", e)
        if data.get("settings") and src_bot is not self.bot:
            # новый бот = новая группа/темы из .env: старые маршруты не переносим
            for k in ("TARGET_CHAT_ID", "PRICE_ROUTES", "BACKDROP_ROUTES", "SUB_CHAT_ID"):
                data["settings"].pop(k, None)
        if data.get("settings"):
            import_runtime_overrides(data["settings"])     # /set-настройки переживают деплой
            log.info("Restored settings changed via /set: %s", ", ".join(sorted(data["settings"])))
        if data.get("proxies") and not self.storage.load_pool():
            self.storage.save_pool(list(data["proxies"]))
        restored = 0
        self.backup_names = {it.get("name") for it in data.get("sessions", [])}
        self._prev_items = {it["name"]: it for it in data.get("sessions", []) if it.get("name")}
        local_strings = {session_file_to_string(r.path) for r in self.storage.list_sessions()}
        for it in data.get("sessions", []):
            name = it["name"]
            if it.get("string") in buried:
                continue                       # аккаунт удалили (вручную или его снёс Telegram) — не возвращаем
            path = self.storage.session_path(name)
            if it.get("string") in local_strings:
                continue                       # этот аккаунт уже есть на диске (под любым именем)
            if path.exists():
                if None in local_strings:
                    continue                   # какой-то файл сейчас не читается — не рискуем дублем
                name = self._free_name()       # то же имя, другой аккаунт: локальный не трогаем, копию кладём рядом
                path = self.storage.session_path(name)
            try:
                string_to_session_file(it["string"], path)
                self.storage.save_meta(name, int(it["api_id"]), it["api_hash"],
                                       owner_id=it.get("owner_id"), proxy=it.get("proxy") or "")
                restored += 1
            except Exception as e:
                log.error("Restore %s failed: %s", it.get("name"), e)
        log.info("Restored %s session(s) from Telegram backup", restored)
        if src_bot is not self.bot:
            # перенесли копию с прошлого бота — отметка в указателе нового, чтобы не переносить повторно
            self._ptr = dict(self._ptr or {})
            self._ptr.pop("s", None)                 # своя старая копия нового бота больше не нужна
            self._ptr["mig"] = old_id
        return restored

    # ------------------------------------------------------------------ market snapshot
    async def save_market(self) -> bool:
        """Снимок известных лотов рынка — чтобы после деплоя не перечитывать весь маркет заново."""
        f = _fernet()
        if f is None or self.market_export is None:
            return False
        try:
            snap = await self.market_export()
            snap["ts"] = time.time()
            blob = await asyncio.to_thread(
                lambda: f.encrypt(zlib.compress(json.dumps(snap, separators=(",", ":")).encode(), 6)))
        except Exception as e:
            log.warning("Market snapshot export failed: %s", e)
            return False
        if len(blob) > MAX_BOT_DOWNLOAD:
            log.warning("Market snapshot too big for Bot API (%.1f MB) — skipped", len(blob) / 2**20)
            return False
        try:
            old = (await self._read_pointer()).get("m")
        except BackupUnavailable:
            return False
        for chat in self.candidate_chats():
            try:
                msg = await self.bot.send_document(
                    chat, BufferedInputFile(blob, MARKET_FILENAME),
                    caption="🗂 Служебный снимок рынка трекера (ускоряет запуск после деплоя). Не удаляйте.",
                    disable_notification=True)
            except Exception as e:
                log.warning("Market snapshot upload to %s failed: %s", chat, e)
                continue
            try:
                await self._write_pointer("m", [msg.document.file_id, chat, msg.message_id, int(time.time())])
            except Exception as e:
                # запрос мог дойти (таймаут после применения) — удаляем файл, только если точно не он в указателе
                self._ptr = None
                try:
                    cur = (await self._read_pointer()).get("m")
                except Exception:
                    cur = False
                if cur and cur[0] == msg.document.file_id:
                    log.info("Market snapshot pointer applied despite error: %s", e)
                    return True
                if cur is not False:
                    try:
                        await self.bot.delete_message(chat, msg.message_id)
                    except Exception:
                        pass
                log.warning("Market snapshot pointer write failed: %s", e)
                return False
            if old and len(old) >= 3 and (old[1], old[2]) != (chat, msg.message_id):
                try:
                    await self.bot.delete_message(old[1], old[2])
                except Exception:
                    pass
            log.info("Market snapshot saved: %s items, %.1f MB", len(snap.get("items", [])), len(blob) / 2**20)
            return True
        return False

    async def restore_market(self) -> int:
        f = _fernet()
        try:
            ptr = (await self._read_pointer()).get("m")
        except BackupUnavailable:
            return 0
        if f is None or not ptr or self.market_import is None:
            return 0
        age_h = (time.time() - (ptr[3] if len(ptr) > 3 else 0)) / 3600
        if age_h > settings.market_snapshot_max_age_hours:
            log.info("Market snapshot is %.1f h old (> %s h) — full silent sync instead",
                     age_h, settings.market_snapshot_max_age_hours)
            return 0
        try:
            buf = io.BytesIO()
            await self.bot.download(ptr[0], destination=buf)
            raw = buf.getvalue()
            snap = await asyncio.to_thread(lambda: json.loads(zlib.decompress(f.decrypt(raw))))
            n = await self.market_import(snap)
            log.info("Market snapshot restored: %s items (%.1f h old) — no full resync needed", n, age_h)
            return n
        except Exception as e:
            log.warning("Market snapshot restore failed: %s — full silent sync instead", e)
            return 0
