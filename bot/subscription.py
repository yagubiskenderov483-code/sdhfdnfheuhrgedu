"""
Подписка на канал с лотами (для всех, кроме ADMIN_IDS).

• /start → приветствие и кнопка «Подписка».
• Пробный период: 60 минут, один раз на человека.
• Платные тарифы — оплата в TON через Tonkeeper на SUB_TON_ADDRESS с уникальным комментарием.
  Бот сам проверяет входящие переводы (toncenter) и, как только оплата пришла, выдаёт ссылку.
• Ссылка одноразовая (member_limit=1). Как только человек зашёл — ссылка удаляется (revoke),
  а время подписки начинает идти с момента входа.
• Когда время вышло — бот кикает человека из канала (ban → сразу unban, чтобы не было ЧС).

Боту в канале/группе нужны права администратора: «Приглашать по ссылке» и «Блокировать участников».
Состояние хранится в kv-таблице БД и попадает в зашифрованную копию (переживает деплой).
"""
from __future__ import annotations

import asyncio
from asyncio import get_running_loop as _running_loop, shield as _shield
import html
import json
import re
import math
import secrets
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery)

from config import settings
from utils.logger import get_logger

if TYPE_CHECKING:
    from main import App

log = get_logger("subs")

WELCOME = ("<b>Добро Пожаловать в лучший Telegram Tracker NFT</b>\n\n"
           "Tracker - отслеживает все лоты выставленные в настоящее время")

CLOSED_TEXT = "Парсер закрыт"

TRIAL_SECONDS = 30 * 60          # Maker: пробный час → 30 минут
REMIND_BEFORE = 60 * 60          # напомнить о продлении за час до конца

# Кастомные Premium-эмодзи Telegram. В inline-кнопках Bot API custom emoji не
# поддерживает (всегда plain text), поэтому emoji_id-эмодзи только в тексте
# сообщений, через HTML-тег <tg-emoji emoji-id="…">fallback</tg-emoji>
# (Premium-юзеры видят кастомку, остальные — fallback). Maker прислал ID:
EMOJI_DIAMOND = "6032742198179532882"     # — для 'Подписка', 'Тариф'
EMOJI_STAR    = "6028338546736107668"     # — цена в Stars
EMOJI_TON     = "5377620962390857342"     # цена в TON
EMOJI_GIFT    = "6032644646587338669"     # — '30 минут бесплатно'


def _tg_emoji(eid: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
FOREVER = -1.0
# код тарифа → (название, цена TON, длительность в секундах или FOREVER, цена в Stars или None)
# Stars=None → тариф не оплачивается звёздами (например, «навсегда» — только TON).
PLANS: dict[str, tuple[str, float, float, int | None]] = {
    "6h":      ("6ч",       1.2, 6 * 3600,   150),
    "12h":     ("12ч",      2.2, 12 * 3600,  250),
    "24h":     ("24ч",      4.0, 24 * 3600,  450),
    "forever": ("Навсегда", 10.0, FOREVER,   None),
}
# Старые тарифы (не показываются в меню). Нужны только чтобы _deliver / check_payments
# не падали при оплате незакрытого счёта, выставленного до смены тарифов.
_LEGACY_PLANS: dict[str, tuple[str, float, float, int | None]] = {
    "48h":  ("48ч", 4.9, 48 * 3600, None),
    "72h":  ("72ч", 8.0, 72 * 3600, None),
}


def plan_info(code: str) -> tuple[str, float, float, int | None] | None:
    """PLANS + старые коды (для доставки оплат по счёту, выставленному до смены тарифов)."""
    return PLANS.get(code) or _LEGACY_PLANS.get(code)
INVOICE_TTL = 60 * 60           # счёт ждёт оплату 1 час
LINK_TTL = 24 * 3600            # ссылка на вход действует сутки
KV_KEY = "subs_state"

# Crypto Bot (CryptoPay) — https://help.crypt.bot/crypto-pay-api
# Если задан CRYPTO_BOT_API_TOKEN — создаём личный invoice на пользователя (через
# createInvoice), получаем pay_url (https://t.me/CryptoBot?start=…). Опрашиваем
# getInvoices в фоне и ловим оплату автоматически. Без токена — фоллбек на статическую
# ссылку (ручная выдача админом).
CRYPTO_PAY_API = "https://pay.crypt.bot/api/"
CRYPTO_PAY_FALLBACK_URL = "https://t.me/send?start=IVxOhr7KT8WT"


async def _crypto_pay_call(method: str, payload: dict) -> dict:
    token = settings.crypto_bot_api_token
    if not token:
        raise RuntimeError("CRYPTO_BOT_API_TOKEN не задан")
    headers = {"Crypto-Pay-API-Token": token, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post(CRYPTO_PAY_API + method, json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(f"CryptoPay {method}: {data.get('error') or data}")
    return data.get("result") or {}


def sub_chat_id() -> int:
    return settings.sub_chat_id or settings.target_chat_id


def _fmt_left(until: float) -> str:
    if until == FOREVER:
        return "навсегда"
    left = until - time.time()
    if left <= 0:
        return "закончилась"
    mins = math.ceil(left / 60 - 1e-6)        # 24 ч сразу после выдачи — «24 ч», а не «23 ч 59 мин»
    d, mins = divmod(mins, 1440)
    h, m = divmod(mins, 60)
    return " ".join(x for x in (f"{d} дн" if d else "", f"{h} ч" if h else "", f"{m} мин" if m else "") if x)


class Subscriptions:
    def __init__(self, app: "App"):
        self.app = app
        self.bot = app.bot
        self.state: dict[str, Any] = {"users": {}, "invoices": {}, "links": {}, "seen_tx": []}
        self._lock = asyncio.Lock()
        self._loaded = False
        # пока копия подписок не восстановлена после деплоя — никого не кикаем и не выдаём пробный час
        self.ready = asyncio.Event()
        self._backup_wanted = False
        self._backup_task: asyncio.Task | None = None
        self.degraded = False          # копия не восстановилась → не кикаем незнакомых, пробный час не даём
        self.on_change: Callable[[], Awaitable[Any]] | None = None   # сохранить копию после оплаты/входа

    # ------------------------------------------------------------------ storage
    async def load(self) -> None:
        if self._loaded:
            return
        try:
            raw = await self.app.db.kv_get(KV_KEY)
            if raw:
                self._merge(json.loads(raw))
        except Exception as e:
            log.warning("Subscriptions load failed: %s", e)
        self._loaded = True

    def _merge(self, data: dict) -> None:
        for k in ("invoices", "links"):
            if isinstance(data.get(k), dict):
                for key, v in data[k].items():
                    self.state[k].setdefault(key, v)
        if isinstance(data.get("users"), dict):
            for key, v in data["users"].items():
                cur = self.state["users"].get(key)
                if cur is None or not isinstance(v, dict):
                    self.state["users"].setdefault(key, v)
                    continue
                if not cur.pop("tmp", False):
                    continue                  # своя запись свежее копии — она и верна
                # запись уже есть (например, создана, пока копия не читалась) — не теряем оплаченное время
                def _best(a, b):
                    if FOREVER in (a, b):
                        return FOREVER
                    return max(float(a or 0), float(b or 0))
                cur["until"] = _best(cur.get("until"), v.get("until"))
                cur["pending"] = _best(cur.get("pending"), v.get("pending"))
                cur["trial"] = bool(cur.get("trial") or v.get("trial"))
                for f in ("username", "name", "in_chat", "started", "reminded"):
                    if cur.get(f) in (None, "", 0, False) and v.get(f):
                        cur[f] = v[f]
        for k in ("revenue_nano", "paid_count"):
            if isinstance(data.get(k), (int, float)):
                self.state[k] = max(int(self.state.get(k) or 0), int(data[k]))
        if isinstance(data.get("seen_tx"), list):
            self.state["seen_tx"] = list(dict.fromkeys(data["seen_tx"] + self.state["seen_tx"]))[-2000:]
        # оплата пришла, а выдача оборвалась перезапуском → повторить выдачу
        for i in self.state["invoices"].values():
            if i.get("paid") and i.get("granted") == "inflight":
                if i.get("paid_at"):
                    i["granted"] = False          # новая версия: время и отметка сохраняются вместе
                else:
                    # счёт старой версии: она могла успеть выдать время, но не отметить счёт —
                    # если у человека есть время после оплаты, считаем выданным (иначе выдали бы дважды)
                    u = self.state["users"].get(str(i.get("uid"))) or {}
                    secs = PLANS.get(i.get("plan"), (None, None, 0, None))[2]
                    if secs == FOREVER:
                        got = u.get("until") == FOREVER or u.get("pending") == FOREVER
                    else:
                        # выданное время тянется минимум до «оплата + срок тарифа»
                        got = (u.get("until") == FOREVER or u.get("pending") == FOREVER
                               or float(u.get("until") or 0) >= float(i.get("ts") or 0) + float(secs or 0)
                               or float(u.get("pending") or 0) >= float(secs or 0))
                    i["granted"] = bool(got)

    async def save(self, backup: bool = False) -> None:
        try:
            await self.app.db.kv_set(KV_KEY, json.dumps(self.state))
        except Exception as e:
            log.warning("Subscriptions save failed: %s", e)
        if backup and self.on_change:
            # резервная копия — в фоне и пачкой: загрузка в Telegram идёт секунды (а при лимитах — минуты),
            # а save(backup=True) часто вызывается под общим локом подписок. При остановке бота копия
            # всё равно сохраняется (App.stop), так что ничего не теряется.
            self._backup_wanted = True
            if self._backup_task is None or self._backup_task.done():
                self._backup_task = _running_loop().create_task(self._backup_soon())

    async def _backup_soon(self) -> None:
        await asyncio.sleep(0.5)                  # несколько изменений подряд → одна загрузка
        while self._backup_wanted:
            self._backup_wanted = False
            try:
                await self.on_change()
            except Exception as e:
                log.warning("Backup after subscription change failed: %s", e)

    def export(self) -> dict:
        return self.state

    async def import_(self, data: dict) -> int:
        """Восстановление из копии (после деплоя)."""
        await self.load()
        async with self._lock:
            self._merge(data or {})
            for u in self.state["users"].values():
                if isinstance(u, dict):
                    u.pop("tmp", None)        # копия прочитана — временных записей больше нет
                    if u.get("unpaid_join") and (self._has_access(u) or u.get("pending")):
                        u.pop("unpaid_join", None)        # в копии нашлась оплата
            await self.save()
        return len(self.state["users"])

    def user(self, uid: int) -> dict:
        u = self.state["users"].get(str(uid))
        if u is None:
            u = {"until": 0, "trial": False, "in_chat": False, "pending": 0}
            if self.degraded or not self.ready.is_set():
                u["tmp"] = True        # создана, пока копия не прочитана: при чтении копии её данные важнее
            self.state["users"][str(uid)] = u
        return u

    # ------------------------------------------------------------------ keyboards
    # Maker: 'убери все эмодзи вообще кроме того что в группе' — в UI подписки
    # эмодзи нет, кастомные остались только в постах лотов (formatter.py).
    def main_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подписка", callback_data="sub:menu")],
            [InlineKeyboardButton(text="Моя подписка", callback_data="sub:my")],
        ])

    def plans_kb(self, uid: int) -> InlineKeyboardMarkup:
        rows = []
        if not self.user(uid).get("trial"):
            rows.append([InlineKeyboardButton(text="30 минут бесплатно",
                                              callback_data="sub:trial")])
        for code, (title, price, _, stars) in PLANS.items():
            stars_part = f" / {stars} Stars" if stars else ""
            label = f"{title} · {price:g} TON{stars_part}"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"sub:buy:{code}")])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data="sub:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ------------------------------------------------------------------ invite links
    async def _member_status(self, uid: int) -> str | None:
        """Статус в канале или None, если Telegram не ответил."""
        try:
            m = await self.bot.get_chat_member(sub_chat_id(), uid)
        except Exception:
            return None
        if m.status == "restricted" and not getattr(m, "is_member", True):
            return "left"          # ограничен, но уже вышел из группы — он НЕ в канале
        return m.status

    async def _is_member(self, uid: int) -> bool:
        return await self._member_status(uid) in ("member", "administrator", "creator", "restricted")

    async def flush_backup(self) -> None:
        """Дождаться фоновой резервной копии (вызывается ВНЕ лока — после денег/выдачи доступа)."""
        t = self._backup_task
        if t is not None and not t.done():
            try:
                await _shield(t)
            except Exception:
                pass

    async def grant(self, uid: int, seconds: float, reason: str, inv: dict | None = None) -> str:
        try:
            return await self._grant(uid, seconds, reason, inv)
        finally:
            await self.flush_backup()

    async def _grant(self, uid: int, seconds: float, reason: str, inv: dict | None = None) -> str:
        """Выдать доступ: если человек уже в канале — продлить; иначе — одноразовая ссылка."""
        async with self._lock:
            u = self.user(uid)
            u.pop("unpaid_join", None)
            st = await self._member_status(uid)
            if st is None:
                raise RuntimeError("get_chat_member failed")
            if st in ("member", "administrator", "creator", "restricted"):
                # уже в канале (в т.ч. зашёл не по нашей ссылке) — просто продлеваем, ссылка не нужна
                pend = u.get("pending") or 0
                if seconds == FOREVER or u["until"] == FOREVER or pend == FOREVER:
                    u["until"] = FOREVER
                else:
                    u["until"] = max(time.time(), float(u["until"] or 0)) + seconds + float(pend)
                u["pending"], u["in_chat"] = 0, True
                if inv is not None:
                    inv["granted"] = True      # отметка «выдано» — в том же сохранении, что и время
                await self.save(backup=True)
                return f"{reason}\nПодписка продлена: осталось <b>{_fmt_left(u['until'])}</b>."
            # сначала ссылка: если создать не вышло — ничего не меняем (повтор не накрутит время)
            link = await self.bot.create_chat_invite_link(
                sub_chat_id(), name=f"sub {uid}"[:32], member_limit=1,
                expire_date=int(time.time() + LINK_TTL))
            await self._revoke_links(uid)       # старые ссылки этого человека больше не работают
            u["pending"] = FOREVER if (seconds == FOREVER or u.get("pending") == FOREVER) \
                else float(u.get("pending") or 0) + seconds
            self.state["links"][link.invite_link] = {"uid": uid, "ts": time.time()}
            if inv is not None:
                inv["granted"] = True
            await self.save(backup=True)
        if u["pending"] == FOREVER or u.get("until") == FOREVER:
            dur = "навсегда"
        else:
            # при входе ожидающее время добавляется к ещё не истёкшему — показываем итог
            dur = _fmt_left(time.time() + float(u["pending"] or 0)
                            + max(0.0, float(u.get("until") or 0) - time.time()))
        starts = ("время пойдёт с момента входа" if u["pending"] and float(u.get("until") or 0) <= time.time()
                  else "время уже идёт")
        return (f"{reason}\n\n"
                f"Ваша одноразовая ссылка для входа:\n{link.invite_link}\n\n"
                f"Доступ: <b>{dur}</b>, {starts}.\n"
                "Ссылка работает один раз и удаляется сразу после входа.")

    async def _revoke_links(self, uid: int) -> None:
        for link in [l for l, r in self.state["links"].items() if int(r.get("uid", 0)) == uid]:
            try:
                await self.bot.revoke_chat_invite_link(sub_chat_id(), link)
            except Exception:
                pass
            self.state["links"].pop(link, None)

    async def on_join(self, ev: ChatMemberUpdated) -> None:
        try:
            await self._on_join(ev)
        finally:
            await self.flush_backup()

    async def _on_join(self, ev: ChatMemberUpdated) -> None:
        if ev.chat.id != sub_chat_id():
            return
        new, old = ev.new_chat_member.status, ev.old_chat_member.status
        uid = ev.new_chat_member.user.id
        if new in ("left", "kicked"):
            async with self._lock:
                u = self.state["users"].get(str(uid))
                if u and u.get("in_chat"):
                    u["in_chat"] = False
                    await self.save()
            return
        was_in = old in ("member", "administrator", "creator") or (
            old == "restricted" and getattr(ev.old_chat_member, "is_member", True))
        if new not in ("member", "restricted") or was_in:
            return
        if new == "restricted" and not getattr(ev.new_chat_member, "is_member", True):
            return
        link = ev.invite_link.invite_link if ev.invite_link else None
        rec = self.state["links"].get(link) if link else None
        user = ev.new_chat_member.user
        if not rec:
            adder = getattr(getattr(ev, "from_user", None), "id", None)
            if adder in settings.admin_ids and adder != uid:
                return            # админ добавил человека вручную — не трогаем
            await self._unknown_join(uid, user)
            return
        # ссылку удаляем навсегда в любом случае
        try:
            await self.bot.revoke_chat_invite_link(sub_chat_id(), link)
        except Exception as e:
            log.warning("Revoke invite link failed: %s", e)
        owner = int(rec["uid"])
        async with self._lock:
            self.state["links"].pop(link, None)
            if owner != uid and uid not in settings.admin_ids:
                # по чужой ссылке зашёл другой человек — выкидываем, владельцу выдаём новую
                await self.save()
                log.warning("User %s joined with invite link of %s — checking access", uid, owner)
                again = True
            else:
                u = self.user(uid)
                self._remember_who(u, user)
                pend = u.get("pending") or 0
                if pend == FOREVER or u["until"] == FOREVER:
                    u["until"] = FOREVER
                else:
                    u["until"] = max(time.time(), float(u["until"] or 0)) + float(pend or 0)
                u["pending"] = 0
                u["in_chat"] = True
                u["started"] = time.time()
                await self.save(backup=True)
                again = False
        if again:
            await self._unknown_join(uid, user)
            u = self.user(owner)
            if await self._is_member(owner):
                return                  # владелец уже в канале — новая ссылка ему не нужна
            try:
                text = await self.grant(owner, 0,
                                        "По вашей ссылке зашёл кто-то другой, держите новую.")
                await self.bot.send_message(owner, text)
            except Exception as e:
                log.warning("Re-issue link to %s failed: %s", owner, e)
            return
        try:
            await self.bot.send_message(uid, f"Вы в канале! Доступ: <b>{_fmt_left(self.user(uid)['until'])}</b>.")
        except Exception:
            pass

    @staticmethod
    def _remember_who(u: dict, user) -> None:
        """Запоминаем, кто именно зашёл: id, юз и имя (для /subs и для проверки)."""
        if user is None:
            return
        u["username"] = getattr(user, "username", None) or u.get("username")
        u["name"] = (" ".join(x for x in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if x)
                     or u.get("name"))

    def _has_access(self, u: dict | None) -> bool:
        if not u:
            return False
        until = u.get("until")
        return until == FOREVER or (bool(until) and float(until) > time.time())

    async def _unknown_join(self, uid: int, user) -> None:
        """Зашёл не по нашей одноразовой ссылке (общая ссылка, чужая ссылка админа, «папка» и т.п.).
        Если у человека есть оплаченный доступ — учитываем; иначе — сразу выкидываем."""
        if uid in settings.admin_ids or getattr(user, "is_bot", False):
            return
        async with self._lock:
            u = self.state["users"].get(str(uid))
            if u is not None:
                self._remember_who(u, user)
            if u and u.get("pending"):
                pend = u["pending"]
                u["until"] = FOREVER if FOREVER in (pend, u.get("until")) else max(time.time(), float(u.get("until") or 0)) + float(pend)
                u["pending"], u["in_chat"], u["started"] = 0, True, time.time()
                await self._revoke_links(uid)        # выданная ссылка больше не нужна — чтобы её не передали
                await self.save(backup=True)
                return
            if self._has_access(u):
                u["in_chat"] = True
                await self._revoke_links(uid)
                await self.save()
                return
        owed = [i for i in self.state["invoices"].values()
                if i.get("paid") and i.get("granted") is not True and int(i.get("uid") or 0) == uid]
        if owed:
            for i in owed:
                if i.get("granted") is False:
                    await self._deliver(i)
            return
        if self.degraded:
            # копия подписок не прочитана — не кикаем сразу (вдруг оплачено), но запоминаем:
            # после чтения копии без оплаченного времени его уберут обычной проверкой
            log.warning("User %s joined, subscriptions backup not restored — not kicking yet", uid)
            async with self._lock:
                u = self.user(uid)
                u["in_chat"] = True
                if not u.get("until") and not u.get("pending"):
                    u["until"] = time.time()
                    u["unpaid_join"] = True          # если в копии оплаты не окажется — это вход без подписки
                await self.save()
            return
        if not await self.kick(uid):
            async with self._lock:
                u = self.user(uid)
                u["until"], u["in_chat"] = float(u.get("until") or 0) or time.time(), True
                await self.save()
            return
        if True:                                      # выкинут — сообщаем админам
            who = f"@{user.username}" if getattr(user, "username", None) else getattr(user, "first_name", "") or uid
            log.warning("User %s (%s) joined without subscription — kicked", uid, who)
            for admin in settings.admin_ids:
                try:
                    await self.bot.send_message(admin, f"Зашёл без подписки и выкинут: "
                                                       f"{html.escape(str(who))} (<code>{uid}</code>)")
                except Exception:
                    pass

    async def _cleanup_old_invoices(self) -> int:
        """Удалить старые invoices из state: неоплаченные старше 7 дней, оплаченные старше 60 дней.

        Без этой чистки state['invoices'] растёт бесконечно — через месяц работы становится
        сотни-тысячи записей, каждая попадает в backup (который сжимается и шифруется, но
        упирается в Bot API 20MB download limit; переполнение → трекер при деплое не
        восстановит аккаунты).

        Хранить оплаченные 60 дней нужно чтобы 'повторный перевод по старому коду' всё
        ещё засчитался. Это окно явно заложено в код (check_payments, 7*86400 там же).
        60 дней — с запасом х8.

        Отдельно трогаем seen_tx: deque capped 2000 в _merge, но на всякий случай capped и тут."""
        now = time.time()
        UNPAID_TTL = 7 * 86400
        PAID_TTL = 60 * 86400
        async with self._lock:
            before = len(self.state["invoices"])
            drop = []
            for code, inv in self.state["invoices"].items():
                age = now - float(inv.get("ts") or 0)
                if inv.get("paid"):
                    # оплаченный счёт убираем через PAID_TTL от момента оплаты (paid_at) или ts
                    paid_age = now - float(inv.get("paid_at") or inv.get("ts") or 0)
                    if paid_age > PAID_TTL and inv.get("granted") is True:
                        drop.append(code)
                else:
                    if age > UNPAID_TTL:
                        drop.append(code)
            for code in drop:
                self.state["invoices"].pop(code, None)
            # seen_tx — на всякий случай ограничим 2000 (может прилететь больше через merge)
            if isinstance(self.state.get("seen_tx"), list) and len(self.state["seen_tx"]) > 2000:
                self.state["seen_tx"] = self.state["seen_tx"][-2000:]
            if drop:
                await self.save(backup=True)
                log.info("Subscriptions cleanup: removed %d old invoices (%d → %d)",
                         len(drop), before, len(self.state["invoices"]))
        return len(drop)

    async def _reconcile(self) -> None:
        """Страховка на случай, если Telegram не прислал событие входа/выхода: всех, у кого доступ
        закончился, проверяем по факту — если человек всё ещё в канале, выкидываем."""
        now = time.time()
        checks = 0          # не больше 60 проверок «ждёт входа» за проход — иначе проход тянется минутами
        for k, u in list(self.state["users"].items()):
            uid = int(k)
            if uid in settings.admin_ids or self._has_access(u) or (self.degraded and u.get("tmp")):
                continue
            if u.get("pending"):
                if checks < 60 and now - float(u.get("checked") or 0) >= 600:
                    checks += 1
                    u["checked"] = now
                    if await self._member_status(uid) in ("member", "restricted"):
                        await self._unknown_join(uid, None)
                continue
            if not u.get("until"):
                continue          # ещё не заходил / никогда не было доступа
            if now - float(u["until"]) > 7 * 86400:
                continue          # давно закончилась — уже проверяли много раз
            if now - float(u.get("checked") or 0) < 600:
                continue          # одного человека проверяем не чаще раза в 10 минут
            u["checked"] = now
            status = await self._member_status(uid)
            if status in ("member", "restricted"):
                if await self.kick(uid):
                    async with self._lock:
                        u["in_chat"] = False
                        await self.save()
                    log.warning("Reconcile: user %s still in channel after subscription ended — kicked", uid)

    async def kick(self, uid: int) -> bool:
        """Удалить из канала без ЧС: ban → сразу unban. → True, если человека в канале больше нет."""
        chat = sub_chat_id()
        try:
            await self.bot.ban_chat_member(chat, uid, until_date=int(time.time()) + 60)
        except Exception as e:
            es = str(e).lower()
            if "participant" in es or "user not found" in es or "user_not_participant" in es:
                return True      # его и так нет в канале
            log.warning("Kick %s failed: %s", uid, e)
            return False
        await asyncio.sleep(1)
        try:
            await self.bot.unban_chat_member(chat, uid, only_if_banned=True)
        except Exception as e:
            log.warning("Unban %s failed: %s", uid, e)
        return True

    # ------------------------------------------------------------------ payments (TON)
    def invoice_url(self, code: str) -> str:
        inv = self.state["invoices"][code]
        return (f"https://app.tonkeeper.com/transfer/{settings.sub_ton_address}"
                f"?amount={int(inv['nano'])}&text={quote(code)}")

    async def new_invoice(self, uid: int, plan: str) -> str:
        async with self._lock:
            # один активный счёт на тариф — не плодим новые при повторном нажатии
            for code, inv in self.state["invoices"].items():
                if (inv["uid"] == uid and inv["plan"] == plan and not inv.get("paid")
                        and inv.get("kind", "ton") == "ton"
                        and time.time() - inv["ts"] < INVOICE_TTL):
                    return code
            code = "TRK" + secrets.token_hex(4).upper()
            self.state["invoices"][code] = {"uid": uid, "plan": plan, "nano": int(round(PLANS[plan][1] * 1e9)),
                                            "ts": time.time(), "paid": False, "kind": "ton"}
            await self.save()
        if self.on_change:
            # счёт — и в резервную копию, чтобы оплата после деплоя не потерялась (вне общего лока)
            try:
                await self.on_change()
            except Exception as e:
                log.warning("Backup after new invoice failed: %s", e)
        return code

    async def new_stars_invoice(self, uid: int, plan: str) -> str:
        """Создать Stars-invoice через Bot API (createInvoiceLink). → https://t.me/$… URL.
        Один живой инвойс на (uid, plan) — повторный клик отдаёт ту же ссылку, чтобы Telegram
        не заваливали десятками активных счетов."""
        title, _price, _secs, stars = PLANS[plan]
        if not stars:
            raise ValueError(f"plan {plan!r} has no stars price")
        now = time.time()
        async with self._lock:
            for code, inv in self.state["invoices"].items():
                if (inv.get("kind") == "stars" and inv["uid"] == uid and inv["plan"] == plan
                        and not inv.get("paid") and inv.get("stars_url")
                        and now - inv["ts"] < INVOICE_TTL):
                    return inv["stars_url"]
            code = "STR" + secrets.token_hex(4).upper()
        # создание ссылки — вне лока: HTTP-вызов Bot API может висеть, не держим подписки
        url = await self.bot.create_invoice_link(
            title=f"NFT Tracker — {title}",
            description=f"Доступ к трекеру: тариф {title}",
            payload=code,
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=int(stars))],
        )
        # Bot API иногда отдаёт URL без схемы — inline-кнопка на 't.me/$…' не работает,
        # нужно 'https://t.me/$…'
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        async with self._lock:
            self.state["invoices"][code] = {
                "uid": uid, "plan": plan, "kind": "stars", "stars": int(stars),
                "stars_url": url, "ts": now, "paid": False,
            }
            await self.save()
        if self.on_change:
            try:
                await self.on_change()
            except Exception as e:
                log.warning("Backup after new stars invoice failed: %s", e)
        return url

    async def new_crypto_invoice(self, uid: int, plan: str) -> str:
        """Создать персональный Crypto Bot invoice. → pay_url (https://t.me/CryptoBot?start=...).
        Один живой invoice на (uid, plan); повторный клик отдаёт ту же ссылку.
        Основной путь: CRYPTO_BOT_API_TOKEN (merchant API, авто-проверка оплаты).
        Запасной: CryptoBot user-session (inline-mode), ссылка живая, но оплату
        придётся подтверждать админу вручную (или ждать TON-перевод по общему адресу)."""
        title, price, _secs, _stars = PLANS[plan]
        now = time.time()
        async with self._lock:
            for code, inv in self.state["invoices"].items():
                if (inv.get("kind") == "crypto" and inv["uid"] == uid and inv["plan"] == plan
                        and not inv.get("paid") and inv.get("pay_url")
                        and now - inv["ts"] < INVOICE_TTL):
                    return inv["pay_url"]
            code = "CRB" + secrets.token_hex(4).upper()

        # Основной путь: merchant API — авто-доставка после оплаты
        if settings.crypto_bot_api_token:
            payload_json = {
                "asset": "TON",
                "amount": f"{price:g}",
                "description": f"NFT Tracker — {title}",
                "payload": code,
                "allow_comments": False,
                "allow_anonymous": False,
            }
            try:
                res = await _crypto_pay_call("createInvoice", payload_json)
            except Exception as e:
                log.error("CryptoPay createInvoice failed: %s", e)
                raise
            pay_url = res.get("pay_url") or res.get("mini_app_invoice_url") or ""
            invoice_id = res.get("invoice_id")
            if not pay_url or not invoice_id:
                raise RuntimeError(f"CryptoPay вернул неполный результат: {res}")
            async with self._lock:
                self.state["invoices"][code] = {
                    "uid": uid, "plan": plan, "kind": "crypto",
                    "nano": int(round(price * 1e9)),
                    "pay_url": pay_url, "invoice_id": int(invoice_id),
                    "asset": "TON", "amount": str(price),
                    "ts": now, "paid": False,
                }
                await self.save()
            if self.on_change:
                try:
                    await self.on_change()
                except Exception as e:
                    log.warning("Backup after new crypto invoice failed: %s", e)
            return pay_url

        # Запасной путь: CryptoBot user-session (inline-mode у @CryptoBot)
        try:
            from bot.cryptobot_session import create_check, has_session
        except Exception as e:
            raise RuntimeError(f"CryptoBot session module недоступен: {e}")
        if not has_session():
            raise RuntimeError("Ни CRYPTO_BOT_API_TOKEN, ни CryptoBot-session не настроены")
        try:
            pay_url = await create_check(price, "TON")
        except Exception as e:
            log.error("CryptoBot session create_check failed: %s", e)
            raise
        async with self._lock:
            self.state["invoices"][code] = {
                "uid": uid, "plan": plan, "kind": "crypto",
                "nano": int(round(price * 1e9)),
                "pay_url": pay_url, "invoice_id": None,
                "asset": "TON", "amount": str(price),
                "ts": now, "paid": False,
                "session_mode": True,         # без merchant API — авто-доставка невозможна
            }
            await self.save()
        if self.on_change:
            try:
                await self.on_change()
            except Exception as e:
                log.warning("Backup after new crypto invoice (session) failed: %s", e)
        return pay_url

    async def check_crypto_payments(self) -> int:
        """Опрос getInvoices у CryptoPay: любой активный crypto-invoice сменивший статус
        на paid — доставляется. → число оплат, обработанных в этот проход."""
        if not settings.crypto_bot_api_token:
            return 0
        pending = [(code, int(i["invoice_id"])) for code, i in self.state["invoices"].items()
                   if i.get("kind") == "crypto" and not i.get("paid") and i.get("invoice_id")]
        if not pending:
            return 0
        ids = [str(iid) for _, iid in pending[:100]]      # Crypto Bot ограничивает 100 id за раз
        try:
            res = await _crypto_pay_call("getInvoices", {"invoice_ids": ",".join(ids), "count": len(ids)})
        except Exception as e:
            log.warning("CryptoPay getInvoices failed: %s", e)
            return 0
        items = res.get("items") or []
        by_id = {int(x.get("invoice_id") or 0): x for x in items if isinstance(x, dict)}
        paid: list[dict] = []
        async with self._lock:
            for code, iid in pending:
                row = by_id.get(iid)
                if not row or row.get("status") != "paid":
                    continue
                inv = self.state["invoices"].get(code)
                if not inv or inv.get("paid"):
                    continue
                inv["paid"] = True
                inv["paid_at"] = time.time()
                inv["granted"] = "inflight"
                inv["crypto_hash"] = row.get("hash")
                # фактически оплачено TON (на случай если asset был пересмотрен)
                try:
                    paid_ton = float(row.get("paid_amount") or row.get("amount") or 0)
                except Exception:
                    paid_ton = float(inv.get("amount") or 0)
                inv["paid_nano"] = int(round(paid_ton * 1e9))
                self.state["revenue_nano"] = int(self.state.get("revenue_nano") or 0) + inv["paid_nano"]
                self.state["paid_count"] = int(self.state.get("paid_count") or 0) + 1
                paid.append(inv)
            if paid:
                await self.save(backup=True)
        await self.flush_backup()
        for inv in paid:
            title, price, _secs, _stars = PLANS.get(inv["plan"], ("?", 0, 0, None))
            log.info("CryptoPay payment received: user=%s plan=%s", inv["uid"], inv["plan"])
            ok = await self._deliver(inv)
            for admin in settings.admin_ids:
                try:
                    await self.bot.send_message(admin,
                        f"Crypto Bot: {inv.get('paid_nano', 0) / 1e9:g} TON, "
                        f"тариф {title}, пользователь <code>{inv['uid']}</code>"
                        + ("" if ok else "\nСсылку выдать не удалось, повторим автоматически."))
                except Exception:
                    pass
        return len(paid)

    async def on_stars_paid(self, payload: str, uid: int, total_amount: int,
                            charge_id: str | None = None) -> None:
        """successful_payment.invoice_payload (= наш code) → отметить оплату и выдать доступ."""
        async with self._lock:
            inv = self.state["invoices"].get(payload)
            if not inv or inv.get("kind") != "stars":
                log.warning("Stars payment for unknown invoice %r from %s", payload, uid)
                return
            if inv.get("paid"):
                return                 # повторный webhook того же платежа
            if int(inv.get("uid") or 0) != int(uid):
                log.warning("Stars payment %s uid mismatch: inv=%s pay=%s", payload, inv.get("uid"), uid)
            inv["paid"] = True
            inv["paid_at"] = time.time()
            inv["granted"] = "inflight"
            inv["charge_id"] = charge_id
            inv["paid_stars"] = int(total_amount)
            self.state["revenue_stars"] = int(self.state.get("revenue_stars") or 0) + int(total_amount)
            self.state["paid_count_stars"] = int(self.state.get("paid_count_stars") or 0) + 1
            await self.save(backup=True)
        await self.flush_backup()
        await self._deliver(inv)
        pinfo = plan_info(inv["plan"]) or ("?", 0.0, 0.0, None)
        title = pinfo[0]
        for admin in settings.admin_ids:
            try:
                await self.bot.send_message(
                    admin, f"Оплата <b>{total_amount} Stars</b>, тариф {title}, "
                           f"пользователь <code>{uid}</code>")
            except Exception:
                pass

    async def _fetch_txs(self) -> list[dict]:
        """Последние входящие переводы. Листаем назад (до 6 страниц по 50), пока не дойдём до уже
        обработанного перевода или до времени старше самого старого открытого счёта — иначе при
        простое бота и спаме мелкими переводами настоящая оплата могла выпасть из окна."""
        url = "https://toncenter.com/api/v2/getTransactions"
        headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
        seen = set(self.state.get("seen_tx") or [])
        open_ts = [float(i.get("ts") or 0) for i in self.state["invoices"].values() if not i.get("paid")]
        oldest = min(open_ts) if open_ts else time.time()
        out: list[dict] = []
        params = {"address": settings.sub_ton_address, "limit": "50", "archival": "false"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            for _page in range(6):
                async with s.get(url, params=params, headers=headers) as r:
                    data = await r.json(content_type=None)
                if not data.get("ok"):
                    if out:
                        break
                    raise RuntimeError(str(data.get("error") or data)[:200])
                page = data.get("result") or []
                out += page
                if len(page) < 50:
                    break
                last = page[-1]
                tid = last.get("transaction_id") or {}
                if (tid.get("hash") in seen or float(last.get("utime") or 0) < oldest
                        or not tid.get("lt")):
                    break
                params = {**params, "lt": tid["lt"], "hash": tid["hash"], "archival": "true"}
        return out

    async def check_payments(self) -> int:
        """Найти оплаченные счета по комментарию и сумме. → сколько оплачено сейчас."""
        # ссылка на оплату живёт INVOICE_TTL, но опоздавший перевод по счёту всё равно засчитываем (7 дней)
        open_codes = {c for c, i in self.state["invoices"].items()
                      if not i.get("paid") and time.time() - i["ts"] < 7 * 86400}
        if not open_codes or not settings.sub_ton_address:
            return 0
        txs = await self._fetch_txs()
        paid, underpaid, extra = [], [], []
        async with self._lock:
            seen = set(self.state["seen_tx"])
            for tx in txs:
                h = (tx.get("transaction_id") or {}).get("hash")
                im = tx.get("in_msg") or {}
                if not h or h in seen or not im.get("source"):
                    continue
                # перевод отскочил обратно (кошелёк не активирован / bounce) — деньги ушли назад, это не оплата
                outs = tx.get("out_msgs") or []
                back = sum(int(o.get("value") or 0) for o in outs if isinstance(o, dict))
                if back > 0 and (any(o.get("destination") == im.get("source") for o in outs if isinstance(o, dict))
                                 or back * 2 >= int(im.get("value") or 0)):
                    self.state["seen_tx"].append(h)
                    seen.add(h)
                    log.warning("Incoming transfer %s bounced back to sender — not counted as payment", h[:12])
                    continue
                comment = (im.get("message") or "").strip().upper()
                code = next((c for c in open_codes if c in comment), None)
                if not code:
                    again = next((c for c, i in self.state["invoices"].items()
                                  if i.get("paid") and c in comment), None)
                    if again:
                        # второй перевод по уже оплаченному счёту — деньги не теряем молча
                        self.state["seen_tx"].append(h)
                        seen.add(h)
                        extra.append((self.state["invoices"][again]["uid"], again, int(im.get("value") or 0)))
                    continue
                inv = self.state["invoices"][code]
                value = int(im.get("value") or 0)
                self.state["seen_tx"].append(h)
                seen.add(h)
                # оплату можно сделать несколькими переводами с тем же комментарием — суммируем
                inv["got"] = int(inv.get("got") or 0) + value
                if inv["got"] < inv["nano"]:
                    log.warning("Invoice %s underpaid: %s < %s nanoTON", code, inv["got"], inv["nano"])
                    underpaid.append((inv["uid"], code, inv["got"], inv["nano"]))
                    continue
                inv["paid"], inv["tx"], inv["granted"] = True, h, "inflight"
                inv["paid_at"] = time.time()
                self.state["revenue_nano"] = int(self.state.get("revenue_nano") or 0) + int(inv["nano"])
                self.state["paid_count"] = int(self.state.get("paid_count") or 0) + 1
                open_codes.discard(code)
                paid.append(inv)
            self.state["seen_tx"] = self.state["seen_tx"][-2000:]
            if paid or underpaid or extra:
                await self.save(backup=True)     # оплата — сразу и в резервную копию (деплой не потеряет)
        await self.flush_backup()
        for uid, code, value in extra:
            for admin in settings.admin_ids:
                try:
                    await self.bot.send_message(admin, f"Повторный перевод {value / 1e9:g} TON по уже оплаченному "
                                                       f"счёту <code>{code}</code> от <code>{uid}</code>. "
                                                       f"Выдать время: /grant {uid} часы")
                except Exception:
                    pass
        for uid, code, value, need in underpaid:
            try:
                await self.bot.send_message(int(uid), f"Пришло {value / 1e9:g} из {need / 1e9:g} TON.\n"
                                                      f"Доплатите <b>{(need - value) / 1e9:g} TON</b> с тем же "
                                                      f"комментарием <code>{code}</code> — доступ придёт сам.")
            except Exception:
                pass
            for admin in settings.admin_ids:
                try:
                    await self.bot.send_message(admin, f"Недоплата по счёту <code>{code}</code> от "
                                                       f"<code>{uid}</code>: пришло {value / 1e9:g} TON из "
                                                       f"{need / 1e9:g}. Выдать вручную: /grant {uid} часы")
                except Exception:
                    pass
        for inv in paid:
            pinfo = plan_info(inv["plan"]) or ("?", 0.0, 0.0, None)
            title, price, secs, _stars = pinfo
            log.info("Payment received: %s TON from user %s (%s)", price, inv["uid"], inv["plan"])
            ok = await self._deliver(inv)
            for admin in settings.admin_ids:
                try:
                    await self.bot.send_message(admin, f"Оплата {price:g} TON, тариф {title}, "
                                                       f"пользователь <code>{inv['uid']}</code>"
                                                       + ("" if ok else "\nСсылку выдать не удалось, бот "
                                                          "повторит сам. Проверьте права бота в канале."))
                except Exception:
                    pass
        return len(paid)

    async def _deliver(self, inv: dict) -> bool:
        """Выдать доступ по оплаченному счёту. Если не вышло — повторим в фоне (оплата не теряется)."""
        pinfo = plan_info(inv["plan"])
        if pinfo is None:
            log.warning("Unknown plan %r on invoice %s; cannot deliver", inv.get("plan"),
                        inv.get("code") or inv.get("uid"))
            return False
        title, price, secs, _stars = pinfo
        kind = inv.get("kind") or "ton"
        if kind == "stars":
            amount = inv.get("stars") or 0
            reason = f"Оплата {amount} Stars пришла, тариф {title}."
        else:
            reason = f"Оплата {price:g} TON пришла, тариф {title}."
        inv["granted"] = "inflight"
        try:
            text = await self.grant(int(inv["uid"]), secs, reason, inv=inv)
        except Exception as e:
            inv["granted"] = False          # повторит _retry_undelivered
            await self.save()
            log.error("Grant after payment failed for %s: %s (will retry)", inv["uid"], e)
            return False
        try:
            await self.bot.send_message(int(inv["uid"]), text)
        except Exception:
            pass
        return True

    async def _retry_undelivered(self) -> None:
        # только явно неудавшиеся выдачи (старые счета без флага не трогаем — они уже выданы)
        for inv in [i for i in self.state["invoices"].values() if i.get("paid") and i.get("granted") is False]:
            await self._deliver(inv)

    # ------------------------------------------------------------------ background loop
    async def run(self) -> None:
        await self.ready.wait()
        await self.load()
        tick = 0
        while True:
            try:
                await self._expire()
                await self._remind()
                if tick % 12 == 0:
                    await self._reconcile()
                # раз в сутки — чистка старых invoices (защита от роста state)
                # 8640 ticks × 10с = 86400с = сутки
                if tick > 0 and tick % 8640 == 0:
                    try:
                        await self._cleanup_old_invoices()
                    except Exception as e:
                        log.warning("Old invoices cleanup failed: %s", e)
                await self.flush_backup()
            except Exception as e:
                log.warning("Subscription expiry check failed: %s", e)
            if tick % 6 == 0:
                try:
                    await self._retry_undelivered()
                except Exception as e:
                    log.warning("Retry of paid invoices failed: %s", e)
            if tick % 2 == 0:
                try:
                    await self.check_payments()
                except Exception as e:
                    log.warning("TON payment check failed: %s", e)
                if settings.crypto_bot_api_token:
                    try:
                        await self.check_crypto_payments()
                    except Exception as e:
                        log.warning("CryptoPay check failed: %s", e)
            tick += 1
            await asyncio.sleep(10)

    async def _remind(self) -> None:
        """За час до конца подписки — напоминание с кнопкой «Продлить» (один раз на срок)."""
        now = time.time()
        for k, u in list(self.state["users"].items()):
            until = u.get("until")
            if (not u.get("in_chat") or until in (FOREVER, None) or not until
                    or not (0 < float(until) - now <= REMIND_BEFORE) or u.get("reminded") == until
                    or float(until) - float(u.get("started") or 0) <= REMIND_BEFORE):
                continue          # пробный час и короткие сроки не напоминаем
            u["reminded"] = until
            try:
                await self.bot.send_message(int(k), f"Подписка закончится через {_fmt_left(float(until))}. "
                                                    "Продлите, чтобы не потерять доступ.",
                                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                                InlineKeyboardButton(text="Продлить", callback_data="sub:menu")]]))
            except Exception:
                pass
            await self.save()

    def stats(self) -> dict:
        now = time.time()
        paid = [i for i in self.state["invoices"].values() if i.get("paid")]
        day = [i for i in paid if now - float(i.get("paid_at") or i.get("ts", 0)) < 86400]
        day_ton = [i for i in day if i.get("kind", "ton") == "ton"]
        day_stars = [i for i in day if i.get("kind") == "stars"]
        users = self.state["users"].values()
        return {
            "active": sum(1 for u in users if u.get("in_chat") and (u.get("until") == FOREVER
                                                                     or float(u.get("until") or 0) > now)),
            "forever": sum(1 for u in users if u.get("until") == FOREVER),
            "trials": sum(1 for u in users if u.get("trial")),
            "paid_total": int(self.state.get("revenue_nano") or 0) / 1e9,
            "paid_count": int(self.state.get("paid_count") or 0),
            "paid_day": sum(int(i.get("nano") or 0) for i in day_ton) / 1e9,
            "paid_day_count": len(day_ton),
            "stars_total": int(self.state.get("revenue_stars") or 0),
            "stars_count": int(self.state.get("paid_count_stars") or 0),
            "stars_day": sum(int(i.get("paid_stars") or i.get("stars") or 0) for i in day_stars),
            "stars_day_count": len(day_stars),
        }

    async def _expire(self) -> None:
        now = time.time()
        due = [int(k) for k, u in self.state["users"].items()
               if u.get("in_chat") and u.get("until") not in (FOREVER, None) and 0 < float(u["until"]) <= now
               and int(k) not in settings.admin_ids and not (self.degraded and u.get("tmp"))]
        for uid in due:
            async with self._lock:
                if self._has_access(self.user(uid)) or self.user(uid).get("pending"):
                    continue      # пока шли по списку, человек продлил подписку — не трогаем
            if not await self.kick(uid):
                continue          # не получилось выгнать — попробуем на следующем круге
            async with self._lock:
                u = self.user(uid)
                u["in_chat"] = False
                await self.save()
                renewed = self._has_access(u) or u.get("pending")
            if renewed:
                # продлил прямо во время кика — выдаём новую ссылку, а не «подписка закончилась»
                try:
                    text = await self.grant(uid, 0, "Подписка продлена, держите ссылку для входа.")
                    await self.bot.send_message(uid, text)
                except Exception as e:
                    log.warning("Re-link after renewal during kick failed for %s: %s", uid, e)
                continue
            if u.pop("unpaid_join", None):
                # зашёл без подписки, пока копия не читалась — это не «подписка закончилась»
                log.warning("User %s joined without subscription (while backup was unread) — kicked", uid)
                for admin in settings.admin_ids:
                    try:
                        await self.bot.send_message(admin, f"Зашёл без подписки и выкинут: <code>{uid}</code>")
                    except Exception:
                        pass
                continue
            log.info("Subscription ended — user %s removed from channel", uid)
            try:
                await self.bot.send_message(uid, "Ваша подписка закончилась. Продлить можно здесь:",
                                            reply_markup=self.main_kb())
            except Exception:
                pass
        # старые счета и неиспользованные ссылки — чистим
        for code, i in list(self.state["invoices"].items()):
            if i.get("paid") and i.get("granted") is not True:
                continue                  # оплачен, но доступ ещё не выдан — не удаляем, пока не выдадим
            if not i.get("paid") and i.get("got"):
                continue                  # частичная оплата — ждём доплату, деньги не теряем
            age_from = float(i.get("paid_at") or i.get("ts") or 0)
            if now - age_from > 7 * 86400:
                self.state["invoices"].pop(code, None)
        for link in [l for l, r in self.state["links"].items() if now - r["ts"] > LINK_TTL + 3600]:
            self.state["links"].pop(link, None)


async def _safe_edit(msg, text: str, reply_markup=None) -> None:
    """edit_text, который не падает на «message is not modified» / старом сообщении."""
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        if "not modified" in str(e):
            return
        try:
            await msg.answer(text, reply_markup=reply_markup)
        except Exception:
            pass


# ---------------------------------------------------------------------------- router
def build_sub_router(app: "App", subs: Subscriptions, is_manager: Callable[[int | None], bool]) -> tuple[Router, Router]:
    """→ (router подписки, catch-all для не-админов — подключать ПОСЛЕДНИМ)."""
    r = Router(name="subscription")
    tail = Router(name="subscription-tail")

    def closed() -> bool:
        # /close — закрыто только для пользователей (лоты идут); /off — выключен весь парсер
        return bool(getattr(app, "users_closed", False)
                    or getattr(getattr(app, "coordinator", None), "paused", False))

    async def welcome(m: Message) -> None:
        if m.chat.type != "private":
            return                  # меню подписки и ссылки — только в личке, не в группе
        await subs.load()
        if closed():
            # закрыто для новых, но у кого подписка есть — должен иметь возможность взять новую ссылку
            await m.answer(CLOSED_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Моя подписка", callback_data="sub:my")]]))
            return
        await m.answer(WELCOME, reply_markup=subs.main_kb())

    @r.chat_member()
    async def on_member(ev: ChatMemberUpdated) -> None:
        await subs.ready.wait()
        await subs.load()
        await subs.on_join(ev)

    @r.callback_query(F.data == "sub:home")
    async def cb_home(q: CallbackQuery) -> None:
        if closed():
            await _safe_edit(q.message, CLOSED_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Моя подписка", callback_data="sub:my")]]))
        else:
            await _safe_edit(q.message, WELCOME, reply_markup=subs.main_kb())
        await q.answer()

    @r.callback_query(F.data == "sub:menu")
    async def cb_menu(q: CallbackQuery) -> None:
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        if closed():
            await q.answer(CLOSED_TEXT, show_alert=True)
            return
        await subs.load()
        await _safe_edit(q.message,
            "<b>Подписка</b>\n\nВыберите тариф",
            reply_markup=subs.plans_kb(q.from_user.id))
        await q.answer()

    @r.callback_query(F.data == "sub:my")
    async def cb_my(q: CallbackQuery) -> None:
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        await subs.load()
        uid = q.from_user.id
        u = subs.state["users"].get(str(uid))
        now = time.time()
        left = (u or {}).get("until") or 0
        status = await subs._member_status(uid)
        if status is None:
            await q.answer("Telegram не ответил, попробуйте через минуту", show_alert=True)
            return
        if status in ("member", "administrator", "creator", "restricted"):
            if u and not subs._has_access(u) and not u.get("pending") and uid not in settings.admin_ids:
                await q.answer("Подписка закончилась — продлить можно в «Подписка»", show_alert=True)
            elif u and u.get("in_chat"):
                await q.answer(f"Подписка активна, осталось: {_fmt_left(left)}"[:200], show_alert=True)
            else:
                await q.answer("Вы в канале" + (f", осталось: {_fmt_left(left)}" if left else ""), show_alert=True)
            return
        # оплачено, но не зашёл (ссылка могла истечь) или вышел, а время ещё осталось → новая ссылка
        if u and (u.get("pending") or left == FOREVER or (left and left > now)):
            # время подписки идёт и вне канала (иначе выход из канала = бесконечная «пауза»)
            async with subs._lock:
                u["in_chat"] = False
            try:
                text = await subs.grant(uid, 0, "Ваш доступ ещё действует, держите новую ссылку.")
                await subs.bot.send_message(uid, text)          # ссылка — только в личку
                await q.answer()
            except Exception as e:
                log.error("New link for %s failed: %s", uid, e)
                await q.answer("Не удалось создать ссылку, попробуйте позже", show_alert=True)
            return
        await q.answer("У вас нет активной подписки.", show_alert=True)

    @r.callback_query(F.data == "sub:trial")
    async def cb_trial(q: CallbackQuery) -> None:
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        if closed():
            await q.answer(CLOSED_TEXT, show_alert=True)
            return
        await subs.load()
        uid = q.from_user.id
        if subs.degraded:
            await q.answer("Пробный период временно недоступен, попробуйте позже", show_alert=True)
            return
        if subs.user(uid).get("trial"):
            await q.answer("Пробный период уже использован", show_alert=True)
            return
        subs.user(uid)["trial"] = True
        subs._remember_who(subs.user(uid), q.from_user)
        try:
            text = await subs.grant(uid, TRIAL_SECONDS, "Пробные 30 минут активированы.")
        except Exception as e:
            subs.user(uid)["trial"] = False
            log.error("Trial link failed for %s: %s", uid, e)
            await q.answer("Не удалось создать ссылку. Попробуйте позже.", show_alert=True)
            return
        try:
            await subs.bot.send_message(uid, text)              # ссылка — только в личку
        except Exception:
            await q.answer("Откройте бота в личке и нажмите «Старт»", show_alert=True)
            return
        await q.answer()

    @r.callback_query(F.data.startswith("sub:buy:"))
    async def cb_buy(q: CallbackQuery) -> None:
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        if closed():
            await q.answer(CLOSED_TEXT, show_alert=True)
            return
        await subs.load()
        plan = q.data.split(":", 2)[2]
        if plan not in PLANS:
            await q.answer("Тариф не найден", show_alert=True)
            return
        title, price, _secs, stars = PLANS[plan]
        if not stars and not settings.sub_ton_address:
            await q.answer("Оплата временно недоступна", show_alert=True)
            return
        subs._remember_who(subs.user(q.from_user.id), q.from_user)
        # Шаг 1: выбор валюты. Карточку оплаты (URL) рисуем ТОЛЬКО когда способ уже выбран.
        # Bot API не поддерживает custom emoji в тексте кнопок — только в сообщении.
        # Поэтому в кнопках обычные /TON, а в тексте сверху — кастомки от Maker.
        rows: list[list[InlineKeyboardButton]] = []
        if stars:
            rows.append([InlineKeyboardButton(text=f"Stars · {stars}",
                                              callback_data=f"sub:pay:{plan}:stars")])
        if settings.sub_ton_address:
            rows.append([InlineKeyboardButton(text=f"Tonkeeper · {price:g} TON",
                                              callback_data=f"sub:pay:{plan}:ton")])
        rows.append([InlineKeyboardButton(text=f"Crypto Bot · {price:g} TON",
                                          callback_data=f"sub:pay:{plan}:wallet")])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data="sub:menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        stars_part = f" или <b>{stars} Stars</b>" if stars else ""
        txt = (f"<b>Тариф {title}</b>\n"
               f"Цена: <b>{price:g} TON</b>{stars_part}\n\n"
               "Выберите способ оплаты:")
        await _safe_edit(q.message, txt, reply_markup=kb)
        await q.answer()

    @r.callback_query(F.data.startswith("sub:pay:"))
    async def cb_pay(q: CallbackQuery) -> None:
        """Шаг 2: карточка выбранного способа оплаты со ссылкой."""
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        if closed():
            await q.answer(CLOSED_TEXT, show_alert=True)
            return
        await subs.load()
        parts = q.data.split(":")
        if len(parts) != 4:
            await q.answer("Ошибка")
            return
        _, _, plan, method = parts
        if plan not in PLANS:
            await q.answer("Тариф не найден", show_alert=True)
            return
        title, price, _secs, stars = PLANS[plan]
        subs._remember_who(subs.user(q.from_user.id), q.from_user)
        back_row = [InlineKeyboardButton(text="« Другой способ", callback_data=f"sub:buy:{plan}")]

        if method == "stars":
            if not stars:
                await q.answer("Этот тариф Stars не оплачивается", show_alert=True)
                return
            try:
                url = await subs.new_stars_invoice(q.from_user.id, plan)
            except Exception as e:
                log.error("Create stars invoice failed for %s: %s", q.from_user.id, e)
                await q.answer("Не удалось создать счёт в Stars, попробуйте позже", show_alert=True)
                return
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Оплатить {stars} Stars", url=url)],
                back_row,
            ])
            await _safe_edit(q.message,
                f"<b>Тариф {title} — {stars} Stars</b>\n\n"
                "Нажмите кнопку ниже и подтвердите оплату в Telegram. "
                "Как только Telegram подтвердит, ссылка на вход придёт сама.",
                reply_markup=kb)
            await q.answer()
            return

        if method == "ton":
            if not settings.sub_ton_address:
                await q.answer("TON-оплата временно недоступна", show_alert=True)
                return
            code = await subs.new_invoice(q.from_user.id, plan)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Оплатить {price:g} TON (Tonkeeper)",
                                      url=subs.invoice_url(code))],
                [InlineKeyboardButton(text="Проверить оплату",
                                      callback_data=f"sub:check:{code}")],
                back_row,
            ])
            await _safe_edit(q.message,
                f"<b>Тариф {title} — {price:g} TON (Tonkeeper)</b>\n\n"
                "Нажмите «Оплатить в Tonkeeper», сумма и комментарий подставятся сами.\n\n"
                f"Или вручную:\nАдрес: <code>{settings.sub_ton_address}</code>\n"
                f"Сумма: <code>{price:g}</code> TON\n"
                f"Комментарий: <code>{code}</code> (обязательно!)\n\n"
                "Как только оплата придёт, бот сам пришлёт ссылку для входа.",
                reply_markup=kb)
            await q.answer()
            return

        if method == "wallet":
            # есть merchant API ИЛИ есть user-session — пробуем новый invoice
            try:
                from bot.cryptobot_session import has_session as _cb_has
                has_cb_session = _cb_has()
            except Exception:
                has_cb_session = False
            if settings.crypto_bot_api_token or has_cb_session:
                try:
                    url = await subs.new_crypto_invoice(q.from_user.id, plan)
                except Exception as e:
                    log.error("Create crypto invoice failed for %s: %s", q.from_user.id, e)
                    await q.answer("Не удалось создать счёт в Crypto Bot, попробуйте позже",
                                   show_alert=True)
                    return
                is_session = (not settings.crypto_bot_api_token) and has_cb_session
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"Оплатить {price:g} TON в Crypto Bot", url=url)],
                    back_row,
                ])
                note = ("Как только оплата пройдёт, бот сам пришлёт ссылку для входа."
                        if not is_session else
                        "После оплаты пришлите скриншот админу — выдадут доступ вручную "
                        "(auto-проверка оплаты работает только с merchant API).")
                await _safe_edit(q.message,
                    f"<b>Тариф {title} — {price:g} TON (Crypto Bot)</b>\n\n"
                    "Нажмите кнопку — откроется чек в @CryptoBot на вашу сумму.\n" + note,
                    reply_markup=kb)
                await q.answer()
                return
            # ни API-токена, ни session — статическая ссылка
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Оплатить {price:g} TON в Crypto Bot",
                                      url=CRYPTO_PAY_FALLBACK_URL)],
                back_row,
            ])
            await _safe_edit(q.message,
                f"<b>Тариф {title} — {price:g} TON (Crypto Bot)</b>\n\n"
                "Нажмите кнопку — откроется встроенный кошелёк.\n"
                "Отправьте точную сумму и пришлите скриншот оплаты админу — "
                "выдадут доступ вручную.",
                reply_markup=kb)
            await q.answer()
            return
        await q.answer("Неизвестный способ оплаты")

    @r.pre_checkout_query()
    async def pre_checkout(q: PreCheckoutQuery) -> None:
        """Telegram спрашивает «можем принять?» — отвечаем да, если инвойс ещё живой.
        Отказ сюда = отказ в оплате у клиента, так что отклоняем только явно чужие payload'ы."""
        payload = q.invoice_payload or ""
        inv = subs.state["invoices"].get(payload)
        if not inv or inv.get("kind") != "stars":
            try:
                await q.answer(ok=False, error_message="Счёт не найден. Откройте бот ещё раз.")
            except Exception:
                pass
            return
        if inv.get("paid"):
            try:
                await q.answer(ok=False, error_message="Этот счёт уже оплачен.")
            except Exception:
                pass
            return
        if time.time() - float(inv.get("ts") or 0) > 7 * 86400:
            try:
                await q.answer(ok=False, error_message="Счёт устарел, создайте новый.")
            except Exception:
                pass
            return
        try:
            await q.answer(ok=True)
        except Exception as e:
            log.warning("pre_checkout answer failed: %s", e)

    @r.message(F.successful_payment)
    async def on_successful_payment(m: Message) -> None:
        """Telegram подтвердил оплату Stars — выдаём доступ."""
        sp = m.successful_payment
        if sp is None:
            return
        try:
            await subs.on_stars_paid(sp.invoice_payload, m.from_user.id, int(sp.total_amount),
                                     charge_id=getattr(sp, "telegram_payment_charge_id", None))
        except Exception as e:
            log.exception("Stars delivery failed")
            try:
                await m.answer(f"Оплата прошла, но выдача доступа споткнулась. "
                               f"Напишите админу с чеком Stars (payload <code>{html.escape(sp.invoice_payload)}</code>).")
            except Exception:
                pass
            for admin in settings.admin_ids:
                try:
                    await subs.bot.send_message(admin,
                        f"Stars оплата от <code>{m.from_user.id}</code> пришла, но выдача упала: "
                        f"{html.escape(str(e))}\npayload <code>{html.escape(sp.invoice_payload)}</code>")
                except Exception:
                    pass

    @r.callback_query(F.data.startswith("sub:check:"))
    async def cb_check(q: CallbackQuery) -> None:
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        await subs.load()
        code = q.data.split(":", 2)[2]
        inv = subs.state["invoices"].get(code)
        if not inv or inv["uid"] != q.from_user.id:
            await q.answer("Счёт не найден", show_alert=True)
            return
        # 1) TON — перечекиваем toncenter прямо сейчас (не ждём фоновый цикл)
        if not inv.get("paid"):
            try:
                if inv.get("kind") == "crypto":
                    await subs.check_crypto_payments()
                else:
                    await subs.check_payments()
            except Exception as e:
                log.warning("Manual payment check failed: %s", e)
            inv = subs.state["invoices"].get(code, {})
        # 2) Если оплачено, но доступ ещё не выдан — выдаём СИНХРОННО и возвращаем ссылку
        if inv.get("paid") and inv.get("granted") is not True:
            try:
                await subs._deliver(inv)
            except Exception as e:
                log.warning("Manual deliver failed: %s", e)
            inv = subs.state["invoices"].get(code, {})
        # 3) Отчитаться юзеру: alert кратко + сообщение в чат с реальной ссылкой, если выдали
        if inv.get("paid"):
            if inv.get("granted") is True:
                await q.answer("Оплата пришла — ссылка отправлена в этот чат",
                               show_alert=True)
            else:
                await q.answer("Оплата пришла, выдаю доступ…", show_alert=True)
                # повторная попытка через мгновение — если grant отвалился от rate-limit
                asyncio.get_running_loop().create_task(subs._retry_undelivered())
        else:
            await q.answer("Оплата пока не пришла. Если уже оплатили, подождите "
                           "~минуту (toncenter) и нажмите ещё раз.",
                           show_alert=True)

    # ---------------- админам: список и ручная выдача
    # те же админы, что видят эти команды в /help (в т.ч. назначенные через /claim, если ADMIN_IDS пуст)
    def _sub_admin(uid: int) -> bool:
        if settings.admin_ids:
            return uid in settings.admin_ids
        from bot.admin import all_admins      # назначенные через /claim, но НЕ владельцы аккаунтов
        return uid in all_admins()
    _ADM = F.from_user.func(lambda u: u is not None and _sub_admin(u.id))
    @r.message(Command("subs"), _ADM)
    async def cmd_subs(m: Message) -> None:
        await subs.load()
        rows = []
        for k, u in subs.state["users"].items():
            if u.get("in_chat") or u.get("pending"):
                st = _fmt_left(u["until"]) if u.get("in_chat") else "ждёт входа"
                who = f"@{u['username']}" if u.get("username") else html.escape(u.get("name") or "")
                rows.append(f"<code>{k}</code> {who} — {st}")
        st = subs.stats()
        from bot.admin import _answer_long
        await _answer_long(m, "<b>Подписчики</b>\n" + ("\n".join(rows) or "нет") +
                       f"\n\nАктивных: {st['active']} (навсегда: {st['forever']}) · пробных всего: {st['trials']}\n"
                       f"TON: {st['paid_total']:g} ({st['paid_count']} оплат) · "
                       f"сутки: {st['paid_day']:g} ({st['paid_day_count']})\n"
                       f"Stars: {st['stars_total']} ({st['stars_count']} оплат) · "
                       f"сутки: {st['stars_day']} ({st['stars_day_count']})\n"
                       "Выдать вручную: <code>/grant ID часы</code> или <code>/grant ID навсегда</code>\n"
                       "Забрать: <code>/revoke ID</code>")

    @r.message(Command("grant"), _ADM)
    async def cmd_grant(m: Message, command: CommandObject) -> None:
        await subs.load()
        usage = ("Выдать подписку:\n"
                 "<code>/grant ID 24</code> — на 24 часа\n"
                 "<code>/grant @username 3д</code> — на 3 дня (д — дни, ч — часы, м — минуты)\n"
                 "<code>/grant ID навсегда</code>\n"
                 "ID человек может узнать, написав боту /start — он покажется у вас в /subs после нажатия кнопки.")
        parts = (command.args or "").split()
        if len(parts) != 2:
            await m.answer(usage)
            return
        uid = _resolve_user(parts[0])
        if uid is None:
            await m.answer("Не нашёл такого пользователя. Укажите числовой ID, или пусть он сначала "
                           "напишет боту /start и нажмёт «Подписка».\n\n" + usage)
            return
        secs = _parse_duration(parts[1])
        if secs is None:
            await m.answer("Не понял срок.\n\n" + usage)
            return
        try:
            text = await subs.grant(uid, secs, "Вам выдан доступ администратором.")
        except Exception as e:
            await m.answer(f"Не удалось выдать: {html.escape(str(e))}\nПроверьте, что бот — админ в канале.")
            return
        try:
            await app.bot.send_message(uid, text)
            await m.answer(f"Выдано пользователю <code>{uid}</code>, ссылка отправлена ему в личку.")
        except Exception:
            # человек ещё не писал боту — отдаём ссылку админу, чтобы переслать вручную
            await m.answer(f"Выдано <code>{uid}</code>, но написать ему бот не может (он не нажимал /start).\n"
                           f"Перешлите ему это:\n\n{text}")

    def _resolve_user(arg: str) -> int | None:
        arg = arg.strip()
        if re.fullmatch(r"[1-9]\d{0,15}", arg, re.ASCII):
            return int(arg)                    # ID человека — положительный (-100… это группы/каналы)
        name = arg.lstrip("@").lower()
        for k, u in subs.state["users"].items():
            if (u.get("username") or "").lower() == name:
                return int(k)
        return None

    def _parse_duration(arg: str) -> float | None:
        a = arg.strip().lower()
        if a in ("навсегда", "forever", "∞"):
            return FOREVER
        mult = 3600
        for suf, k in (("дн", 86400), ("д", 86400), ("d", 86400), ("ч", 3600), ("h", 3600),
                       ("мин", 60), ("м", 60), ("m", 60)):
            if a.endswith(suf):
                a, mult = a[: -len(suf)], k
                break
        try:
            v = float(a.replace(",", ".")) * mult
        except ValueError:
            return None
        # inf / nan / 100 лет — не срок (inf ломал запись подписчика навсегда)
        return v if math.isfinite(v) and 60 <= v <= 10 * 365 * 86400 else None

    @r.message(Command("subs_cleanup"), _ADM)
    async def cmd_subs_cleanup(m: Message) -> None:
        """Ручная чистка старых invoices (без ожидания суточной автоматической).
        Удаляет unpaid старше 7 дней и paid старше 60 дней."""
        await subs.load()
        before = {
            "invoices": len(subs.state.get("invoices", {})),
            "users": len(subs.state.get("users", {})),
            "seen_tx": len(subs.state.get("seen_tx", [])),
        }
        n = await subs._cleanup_old_invoices()
        after = {
            "invoices": len(subs.state.get("invoices", {})),
            "users": len(subs.state.get("users", {})),
            "seen_tx": len(subs.state.get("seen_tx", [])),
        }
        await m.answer(
            f"<b>Чистка старых invoices</b>\n\n"
            f"Удалено: <b>{n}</b>\n\n"
            f"Было: invoices={before['invoices']}, users={before['users']}, "
            f"seen_tx={before['seen_tx']}\n"
            f"Стало: invoices={after['invoices']}, users={after['users']}, "
            f"seen_tx={after['seen_tx']}\n\n"
            "<i>Эта чистка автоматически выполняется раз в сутки. Используй /subs_cleanup "
            "только если подозреваешь переполнение backup.</i>")

    @r.message(Command("revoke"), _ADM)
    async def cmd_revoke(m: Message, command: CommandObject) -> None:
        await subs.load()
        arg = (command.args or "").strip()
        uid = _resolve_user(arg) if arg else None
        if not arg:
            await m.answer("Формат: <code>/revoke ID</code> или <code>/revoke @username</code>")
            return
        if uid is None:
            await m.answer(f"Не нашёл {html.escape(arg[:64])} среди тех, кто писал боту. Укажите числовой ID.")
            return
        cur = subs.state["users"].get(str(uid))
        if (not cur or not (cur.get("until") or cur.get("pending") or cur.get("in_chat"))) \
                and not await subs._is_member(uid):
            await m.answer(f"У <code>{uid}</code> нет подписки — забирать нечего.")
            return
        ok = await subs.kick(uid)
        async with subs._lock:
            await subs._revoke_links(uid)
            u = subs.user(uid)
            u.update({"until": 0 if ok else time.time(), "in_chat": not ok, "pending": 0})
            await subs.save(backup=True)
        await subs.flush_backup()
        await m.answer(f"Доступ {uid} забран" if ok else
                       f"Доступ {uid} снят, но выгнать из канала сейчас не вышло — бот повторит сам")

    # ---------------- шорткат: "<ID> а" в личке от админа → подписка навсегда
    #     (а|н|навсегда|forever|∞ — любое из этих слов). Это ускорение для /grant <id> навсегда.
    _SHORTCUT_RE = re.compile(r"^\s*(\d{1,16})\s+(?:а|н|навсегда|forever|∞)\s*$", re.IGNORECASE)
    @r.message(F.chat.type == "private", _ADM, StateFilter(None),
               F.text.regexp(_SHORTCUT_RE))
    async def cmd_grant_shortcut(m: Message) -> None:
        mt = _SHORTCUT_RE.match(m.text or "")
        if not mt:
            return
        uid = int(mt.group(1))
        if uid <= 0 or uid > 10**15:
            return
        await subs.load()
        try:
            text = await subs.grant(uid, FOREVER, "Вам выдан доступ администратором.")
        except Exception as e:
            await m.answer(f"Не удалось выдать: {html.escape(str(e))}\nПроверьте, что бот — админ в канале.")
            return
        try:
            await app.bot.send_message(uid, text)
            await m.answer(f"Навсегда выдано <code>{uid}</code>, ссылка отправлена ему в личку.")
        except Exception:
            await m.answer(f"Навсегда выдано <code>{uid}</code>, но написать ему бот не может (он не нажимал /start).\n"
                           f"Перешлите ему это:\n\n{text}")

    # ---------------- всё остальное от не-админов → приветствие
    @tail.message(F.chat.type == "private", F.from_user.func(lambda u: u is not None and not is_manager(u.id)))
    async def any_msg(m: Message) -> None:
        await welcome(m)

    r.welcome = welcome   # type: ignore[attr-defined]
    return r, tail
