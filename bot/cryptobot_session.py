"""Dedicated Telethon user session for interacting with @CryptoBot.

Admin подключает отдельный Telegram-аккаунт через /cryptobot_connect. Session
сохраняется в data/cryptobot_session.string (StringSession, base64-подобная
строка). Когда пользователь выбирает «Crypto Bot» в меню оплаты и у нас нет
CRYPTO_BOT_API_TOKEN, мы используем этот session чтобы сделать inline-query
к @CryptoBot на сумму и вытащить URL чека.

Это запасной путь (merchant-API стабильнее). Если session нет и токена нет —
кнопка ведёт на статическую ссылку (ручная выдача админом).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetInlineBotResultsRequest
from telethon.tl.types import InputPeerSelf

from config import DATA_DIR, settings

log = logging.getLogger("cryptobot_session")

SESSION_PATH: Path = DATA_DIR / "cryptobot_session.string"


def has_session() -> bool:
    try:
        return SESSION_PATH.exists() and bool(SESSION_PATH.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def load_session_string() -> Optional[str]:
    try:
        s = SESSION_PATH.read_text(encoding="utf-8").strip()
        return s or None
    except OSError:
        return None


def save_session_string(s: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(s, encoding="utf-8")
    # владелец читает свою сессию
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass


def delete_session() -> bool:
    try:
        SESSION_PATH.unlink()
        return True
    except FileNotFoundError:
        return False


async def make_client(session_string: Optional[str] = None) -> TelegramClient:
    """Создать Telethon-клиент с нужными API_ID/API_HASH. Подключение вызывает caller."""
    s = session_string if session_string is not None else load_session_string()
    if s is None:
        s = ""
    return TelegramClient(StringSession(s), settings.api_id, settings.api_hash,
                          connection_retries=1, timeout=10,
                          device_model="CryptoBotLink", system_version="1.0", app_version="1.0")


async def create_check(amount: float, asset: str = "TON", timeout: float = 15.0) -> str:
    """Сделать чек через @CryptoBot inline-mode. → URL чека или RuntimeError.

    Используется только если CRYPTO_BOT_API_TOKEN НЕ задан. Основной путь — merchant API."""
    if not has_session():
        raise RuntimeError("CryptoBot session не подключён (/cryptobot_connect)")
    if not settings.api_id or not settings.api_hash:
        raise RuntimeError("API_ID / API_HASH не настроены — session не поднять")

    client = await make_client()
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        if not await client.is_user_authorized():
            raise RuntimeError("Сохранённая CryptoBot-сессия невалидна, переподключи /cryptobot_connect")
        # @CryptoBot inline: query вида '1.2 TON' → список чеков разных номиналов,
        # первый — именно под запрошенную сумму. URL чека — в reply_markup или в url.
        q = f"{amount:g} {asset}".strip()
        res = await asyncio.wait_for(
            client(GetInlineBotResultsRequest(
                bot="CryptoBot",
                peer=InputPeerSelf(),
                query=q,
                offset="",
            )),
            timeout=timeout,
        )
        for r in getattr(res, "results", None) or []:
            # 1) прямой URL у результата (article/photo и т.п.)
            u = getattr(r, "url", None)
            if u and ("t.me/" in u or "crypt.bot" in u):
                return u
            # 2) URL в inline keyboard, прикреплённой к send_message
            sm = getattr(r, "send_message", None)
            rm = getattr(sm, "reply_markup", None) if sm else None
            rows = getattr(rm, "rows", None) or []
            for row in rows:
                for btn in getattr(row, "buttons", None) or []:
                    bu = getattr(btn, "url", None)
                    if bu and ("t.me/" in bu or "crypt.bot" in bu):
                        return bu
        raise RuntimeError(f"CryptoBot inline не вернул URL чека (query='{q}'). "
                           "Возможно @CryptoBot изменил inline-формат — лучше задать "
                           "CRYPTO_BOT_API_TOKEN.")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
