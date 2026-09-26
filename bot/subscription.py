"""
Подписка на канал с лотами (для всех, кроме ADMIN_IDS).

• /start → приветствие и кнопка «💎 Подписка».
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
import json
import secrets
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

from config import settings
from utils.logger import get_logger

if TYPE_CHECKING:
    from main import App

log = get_logger("subs")

WELCOME = ("<b>Добро Пожаловать в лучший Telegram Tracker NFT</b>\n\n"
           "Tracker - отслеживает все лоты выставленные в настоящее время")

TRIAL_SECONDS = 60 * 60
FOREVER = -1.0
# код тарифа → (название, цена TON, длительность в секундах или FOREVER)
PLANS: dict[str, tuple[str, float, float]] = {
    "12h": ("12ч", 1.5, 12 * 3600),
    "24h": ("24ч", 2.6, 24 * 3600),
    "48h": ("48ч", 4.9, 48 * 3600),
    "72h": ("72ч", 8.0, 72 * 3600),
    "forever": ("Навсегда", 15.0, FOREVER),
}
INVOICE_TTL = 60 * 60           # счёт ждёт оплату 1 час
LINK_TTL = 24 * 3600            # ссылка на вход действует сутки
KV_KEY = "subs_state"


def sub_chat_id() -> int:
    return settings.sub_chat_id or settings.target_chat_id


def _fmt_left(until: float) -> str:
    if until == FOREVER:
        return "навсегда"
    left = int(until - time.time())
    if left <= 0:
        return "закончилась"
    h, m = divmod(left // 60, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


class Subscriptions:
    def __init__(self, app: "App"):
        self.app = app
        self.bot = app.bot
        self.state: dict[str, Any] = {"users": {}, "invoices": {}, "links": {}, "seen_tx": []}
        self._lock = asyncio.Lock()
        self._loaded = False
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
        for k in ("users", "invoices", "links"):
            if isinstance(data.get(k), dict):
                self.state[k].update(data[k])
        if isinstance(data.get("seen_tx"), list):
            self.state["seen_tx"] = list(dict.fromkeys(self.state["seen_tx"] + data["seen_tx"]))[-1000:]

    async def save(self, backup: bool = False) -> None:
        try:
            await self.app.db.kv_set(KV_KEY, json.dumps(self.state))
        except Exception as e:
            log.warning("Subscriptions save failed: %s", e)
        if backup and self.on_change:
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
            await self.save()
        return len(self.state["users"])

    def user(self, uid: int) -> dict:
        return self.state["users"].setdefault(str(uid), {"until": 0, "trial": False, "in_chat": False,
                                                         "pending": 0})

    # ------------------------------------------------------------------ keyboards
    def main_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Подписка", callback_data="sub:menu")],
            [InlineKeyboardButton(text="📊 Моя подписка", callback_data="sub:my")],
        ])

    def plans_kb(self, uid: int) -> InlineKeyboardMarkup:
        rows = []
        if not self.user(uid).get("trial"):
            rows.append([InlineKeyboardButton(text="🎁 Пробный час бесплатно", callback_data="sub:trial")])
        for code, (title, price, _) in PLANS.items():
            rows.append([InlineKeyboardButton(text=f"{title} · {price:g} TON", callback_data=f"sub:buy:{code}")])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data="sub:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ------------------------------------------------------------------ invite links
    async def _is_member(self, uid: int) -> bool:
        try:
            m = await self.bot.get_chat_member(sub_chat_id(), uid)
            return m.status in ("member", "administrator", "creator", "restricted")
        except Exception:
            return False

    async def grant(self, uid: int, seconds: float, reason: str) -> str:
        """Выдать доступ: если человек уже в канале — продлить; иначе — одноразовая ссылка."""
        async with self._lock:
            u = self.user(uid)
            if await self._is_member(uid) and u.get("in_chat"):
                if seconds == FOREVER or u["until"] == FOREVER:
                    u["until"] = FOREVER
                else:
                    u["until"] = max(time.time(), float(u["until"] or 0)) + seconds
                await self.save(backup=True)
                return f"✅ {reason}\nПодписка продлена: осталось <b>{_fmt_left(u['until'])}</b>."
            u["pending"] = FOREVER if (seconds == FOREVER or u.get("pending") == FOREVER) \
                else float(u.get("pending") or 0) + seconds
            link = await self.bot.create_chat_invite_link(
                sub_chat_id(), name=f"sub {uid}"[:32], member_limit=1,
                expire_date=int(time.time() + LINK_TTL))
            self.state["links"][link.invite_link] = {"uid": uid, "ts": time.time()}
            await self.save(backup=True)
        dur = "навсегда" if u["pending"] == FOREVER else _fmt_left(time.time() + u["pending"])
        return (f"✅ {reason}\n\n"
                f"🔗 Ваша одноразовая ссылка для входа:\n{link.invite_link}\n\n"
                f"Доступ: <b>{dur}</b>, время пойдёт с момента входа.\n"
                "Ссылка работает один раз и удаляется сразу после входа.")

    async def on_join(self, ev: ChatMemberUpdated) -> None:
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
        if new != "member" or old in ("member", "administrator", "creator", "restricted"):
            return
        link = ev.invite_link.invite_link if ev.invite_link else None
        rec = self.state["links"].get(link) if link else None
        if not rec:
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
                await self.kick(uid)
                log.warning("User %s joined with invite link of %s — kicked", uid, owner)
                again = True
            else:
                u = self.user(uid)
                pend = u.get("pending") or 0
                if pend == FOREVER or u["until"] == FOREVER:
                    u["until"] = FOREVER
                else:
                    u["until"] = max(time.time(), float(u["until"] or 0)) + float(pend or TRIAL_SECONDS)
                u["pending"] = 0
                u["in_chat"] = True
                await self.save(backup=True)
                again = False
        if again:
            u = self.user(owner)
            try:
                text = await self.grant(owner, 0 if u.get("pending") else TRIAL_SECONDS,
                                        "По вашей ссылке зашёл кто-то другой, держите новую.")
                await self.bot.send_message(owner, text)
            except Exception as e:
                log.warning("Re-issue link to %s failed: %s", owner, e)
            return
        try:
            await self.bot.send_message(uid, f"🎉 Вы в канале! Доступ: <b>{_fmt_left(self.user(uid)['until'])}</b>.")
        except Exception:
            pass

    async def kick(self, uid: int) -> None:
        """Удалить из канала без ЧС: ban → сразу unban."""
        chat = sub_chat_id()
        try:
            await self.bot.ban_chat_member(chat, uid, until_date=int(time.time()) + 60)
        except Exception as e:
            log.warning("Kick %s failed: %s", uid, e)
            return
        await asyncio.sleep(1)
        try:
            await self.bot.unban_chat_member(chat, uid, only_if_banned=True)
        except Exception as e:
            log.warning("Unban %s failed: %s", uid, e)

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
                        and time.time() - inv["ts"] < INVOICE_TTL):
                    return code
            code = "TRK" + secrets.token_hex(4).upper()
            self.state["invoices"][code] = {"uid": uid, "plan": plan, "nano": int(round(PLANS[plan][1] * 1e9)),
                                            "ts": time.time(), "paid": False}
            await self.save()
            return code

    async def _fetch_txs(self) -> list[dict]:
        url = "https://toncenter.com/api/v2/getTransactions"
        params = {"address": settings.sub_ton_address, "limit": "50", "archival": "false"}
        headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, params=params, headers=headers) as r:
                data = await r.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or data)[:200])
        return data.get("result") or []

    async def check_payments(self) -> int:
        """Найти оплаченные счета по комментарию и сумме. → сколько оплачено сейчас."""
        open_codes = {c for c, i in self.state["invoices"].items()
                      if not i.get("paid") and time.time() - i["ts"] < INVOICE_TTL * 24}
        if not open_codes or not settings.sub_ton_address:
            return 0
        txs = await self._fetch_txs()
        paid = []
        async with self._lock:
            seen = set(self.state["seen_tx"])
            for tx in txs:
                h = (tx.get("transaction_id") or {}).get("hash")
                im = tx.get("in_msg") or {}
                if not h or h in seen or not im.get("source"):
                    continue
                comment = (im.get("message") or "").strip().upper()
                code = next((c for c in open_codes if c in comment), None)
                if not code:
                    continue
                inv = self.state["invoices"][code]
                value = int(im.get("value") or 0)
                if value < inv["nano"]:
                    log.warning("Invoice %s underpaid: %s < %s nanoTON", code, value, inv["nano"])
                    self.state["seen_tx"].append(h)
                    continue
                inv["paid"], inv["tx"] = True, h
                self.state["seen_tx"].append(h)
                open_codes.discard(code)
                paid.append(inv)
            self.state["seen_tx"] = self.state["seen_tx"][-1000:]
            if paid:
                await self.save()
        for inv in paid:
            title, price, secs = PLANS[inv["plan"]]
            log.info("Payment received: %s TON from user %s (%s)", price, inv["uid"], inv["plan"])
            try:
                text = await self.grant(int(inv["uid"]), secs, f"Оплата {price:g} TON пришла, тариф {title}.")
                await self.bot.send_message(int(inv["uid"]), text)
            except Exception as e:
                log.error("Grant after payment failed for %s: %s", inv["uid"], e)
            for admin in settings.admin_ids:
                try:
                    await self.bot.send_message(admin, f"💰 Оплата {price:g} TON — тариф {title}, "
                                                       f"пользователь <code>{inv['uid']}</code>")
                except Exception:
                    pass
        return len(paid)

    # ------------------------------------------------------------------ background loop
    async def run(self) -> None:
        await self.load()
        tick = 0
        while True:
            try:
                await self._expire()
            except Exception as e:
                log.warning("Subscription expiry check failed: %s", e)
            if tick % 2 == 0:
                try:
                    await self.check_payments()
                except Exception as e:
                    log.warning("TON payment check failed: %s", e)
            tick += 1
            await asyncio.sleep(10)

    async def _expire(self) -> None:
        now = time.time()
        due = [int(k) for k, u in self.state["users"].items()
               if u.get("in_chat") and u.get("until") not in (FOREVER, None) and 0 < float(u["until"]) <= now
               and int(k) not in settings.admin_ids]
        for uid in due:
            await self.kick(uid)
            async with self._lock:
                self.user(uid)["in_chat"] = False
                await self.save()
            log.info("Subscription ended — user %s removed from channel", uid)
            try:
                await self.bot.send_message(uid, "⌛ Ваша подписка закончилась. Продлить можно здесь 👇",
                                            reply_markup=self.main_kb())
            except Exception:
                pass
        # старые счета и неиспользованные ссылки — чистим
        for code in [c for c, i in self.state["invoices"].items() if now - i["ts"] > 7 * 86400]:
            self.state["invoices"].pop(code, None)
        for link in [l for l, r in self.state["links"].items() if now - r["ts"] > LINK_TTL + 3600]:
            self.state["links"].pop(link, None)


# ---------------------------------------------------------------------------- router
def build_sub_router(app: "App", subs: Subscriptions, is_manager: Callable[[int | None], bool]) -> tuple[Router, Router]:
    """→ (router подписки, catch-all для не-админов — подключать ПОСЛЕДНИМ)."""
    r = Router(name="subscription")
    tail = Router(name="subscription-tail")

    async def welcome(m: Message) -> None:
        await subs.load()
        await m.answer(WELCOME, reply_markup=subs.main_kb())

    @r.chat_member()
    async def on_member(ev: ChatMemberUpdated) -> None:
        await subs.load()
        await subs.on_join(ev)

    @r.callback_query(F.data == "sub:home")
    async def cb_home(q: CallbackQuery) -> None:
        await q.message.edit_text(WELCOME, reply_markup=subs.main_kb())
        await q.answer()

    @r.callback_query(F.data == "sub:menu")
    async def cb_menu(q: CallbackQuery) -> None:
        await subs.load()
        await q.message.edit_text("💎 <b>Подписка</b>\n\nВыберите тариф. Платите в TON через Tonkeeper, "
                                  "ссылка на вход придёт сама сразу после оплаты.",
                                  reply_markup=subs.plans_kb(q.from_user.id))
        await q.answer()

    @r.callback_query(F.data == "sub:my")
    async def cb_my(q: CallbackQuery) -> None:
        await subs.load()
        u = subs.state["users"].get(str(q.from_user.id))
        if not u or (not u.get("in_chat") and not u.get("pending")):
            text = "У вас нет активной подписки."
        elif u.get("pending") and not u.get("in_chat"):
            text = "Доступ оплачен. Зайдите по ссылке, которую прислал бот, время пойдёт с момента входа."
        else:
            text = f"✅ Подписка активна, осталось: {_fmt_left(u['until'])}"
        await q.answer(text[:200], show_alert=True)

    @r.callback_query(F.data == "sub:trial")
    async def cb_trial(q: CallbackQuery) -> None:
        await subs.load()
        uid = q.from_user.id
        if subs.user(uid).get("trial"):
            await q.answer("Пробный период уже использован", show_alert=True)
            return
        subs.user(uid)["trial"] = True
        try:
            text = await subs.grant(uid, TRIAL_SECONDS, "Пробный час активирован.")
        except Exception as e:
            subs.user(uid)["trial"] = False
            log.error("Trial link failed for %s: %s", uid, e)
            await q.answer("Не удалось создать ссылку. Попробуйте позже.", show_alert=True)
            return
        await q.message.answer(text)
        await q.answer()

    @r.callback_query(F.data.startswith("sub:buy:"))
    async def cb_buy(q: CallbackQuery) -> None:
        await subs.load()
        plan = q.data.split(":", 2)[2]
        if plan not in PLANS or not settings.sub_ton_address:
            await q.answer("Оплата временно недоступна", show_alert=True)
            return
        title, price, _ = PLANS[plan]
        code = await subs.new_invoice(q.from_user.id, plan)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💎 Оплатить {price:g} TON в Tonkeeper", url=subs.invoice_url(code))],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"sub:check:{code}")],
            [InlineKeyboardButton(text="« Назад", callback_data="sub:menu")],
        ])
        await q.message.edit_text(
            f"💎 <b>Тариф {title}, {price:g} TON</b>\n\n"
            "Нажмите «Оплатить в Tonkeeper», сумма и комментарий подставятся сами.\n\n"
            f"Или вручную:\nАдрес: <code>{settings.sub_ton_address}</code>\n"
            f"Сумма: <code>{price:g}</code> TON\n"
            f"Комментарий: <code>{code}</code> (обязательно!)\n\n"
            "Как только оплата придёт, бот сам пришлёт ссылку для входа (обычно до минуты).",
            reply_markup=kb)
        await q.answer()

    @r.callback_query(F.data.startswith("sub:check:"))
    async def cb_check(q: CallbackQuery) -> None:
        await subs.load()
        code = q.data.split(":", 2)[2]
        inv = subs.state["invoices"].get(code)
        if not inv or inv["uid"] != q.from_user.id:
            await q.answer("Счёт не найден", show_alert=True)
            return
        if not inv.get("paid"):
            try:
                await subs.check_payments()
            except Exception as e:
                log.warning("Manual payment check failed: %s", e)
        if subs.state["invoices"].get(code, {}).get("paid"):
            await q.answer("✅ Оплата пришла, ссылка в чате", show_alert=True)
        else:
            await q.answer("Оплата пока не пришла. Если уже оплатили, подождите минуту и нажмите ещё раз.",
                           show_alert=True)

    # ---------------- админам: список и ручная выдача
    @r.message(Command("subs"), F.from_user.func(lambda u: is_manager(u.id)))
    async def cmd_subs(m: Message) -> None:
        await subs.load()
        rows = []
        for k, u in subs.state["users"].items():
            if u.get("in_chat") or u.get("pending"):
                st = _fmt_left(u["until"]) if u.get("in_chat") else "ждёт входа"
                rows.append(f"<code>{k}</code> — {st}")
        paid = sum(1 for i in subs.state["invoices"].values() if i.get("paid"))
        await m.answer("<b>Подписчики</b>\n" + ("\n".join(rows[:80]) or "нет") +
                       f"\n\nОплаченных счетов: {paid}\n"
                       "Выдать вручную: <code>/grant ID часы</code> или <code>/grant ID навсегда</code>\n"
                       "Забрать: <code>/revoke ID</code>")

    @r.message(Command("grant"), F.from_user.func(lambda u: is_manager(u.id)))
    async def cmd_grant(m: Message, command: CommandObject) -> None:
        await subs.load()
        parts = (command.args or "").split()
        if len(parts) != 2 or not parts[0].isdigit():
            await m.answer("Формат: <code>/grant ID часы</code> или <code>/grant ID навсегда</code>")
            return
        secs = FOREVER if parts[1].lower() in ("навсегда", "forever", "0") else float(parts[1]) * 3600
        uid = int(parts[0])
        try:
            text = await subs.grant(uid, secs, "Вам выдан доступ администратором.")
            await app.bot.send_message(uid, text)
            await m.answer(f"✅ Выдано пользователю {uid}")
        except Exception as e:
            await m.answer(f"❌ {e}")

    @r.message(Command("revoke"), F.from_user.func(lambda u: is_manager(u.id)))
    async def cmd_revoke(m: Message, command: CommandObject) -> None:
        await subs.load()
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await m.answer("Формат: <code>/revoke ID</code>")
            return
        uid = int(arg)
        await subs.kick(uid)
        async with subs._lock:
            u = subs.user(uid)
            u.update({"until": 0, "in_chat": False, "pending": 0})
            await subs.save(backup=True)
        await m.answer(f"✅ Доступ {uid} забран")

    # ---------------- всё остальное от не-админов → приветствие
    @tail.message(F.chat.type == "private", F.from_user.func(lambda u: not is_manager(u.id)))
    async def any_msg(m: Message) -> None:
        await welcome(m)

    r.welcome = welcome   # type: ignore[attr-defined]
    return r, tail
