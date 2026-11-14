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
from utils import emoji as E
from utils.emoji import icon as _icon
from utils.logger import get_logger

if TYPE_CHECKING:
    from main import App

log = get_logger("subs")

WELCOME = ("<b>Добро пожаловать в лучший Telegram Tracker NFT</b>\n\n"
           "Tracker отслеживает все лоты, выставленные в настоящее время")

CLOSED_TEXT = "Парсер закрыт"
GATE_TEXT = ('<tg-emoji emoji-id="6030563507299160824">❗️</tg-emoji> '
             "<b>Для использование бота нужно подписаться на канал</b>")

TRIAL_SECONDS = 30 * 60          # Maker: пробный час → 30 минут
REMIND_BEFORE = 60 * 60          # напомнить о продлении за час до конца

# Кастомные Premium-эмодзи Telegram. В inline-кнопках Bot API custom emoji не
# поддерживает (всегда plain text), поэтому emoji_id-эмодзи только в тексте
# сообщений, через HTML-тег <tg-emoji emoji-id="…">fallback</tg-emoji>
# (Premium-юзеры видят кастомку, остальные — fallback). Maker прислал ID:
EMOJI_DIAMOND = "6032742198179532882"     # — для 'Подписка', 'Тариф'
EMOJI_STAR    = "6028338546736107668"     # — цена в Stars
EMOJI_TON     = "5956405141677548170"     # 💎 цена в TON (Maker)
EMOJI_GIFT    = "6032644646587338669"     # — '30 минут бесплатно'
EMOJI_PAID    = "6039486778597970865"     # — 'Оплата получена, ожидайте.'
EMOJI_SUB     = "5927118708873892465"     # — 'Подписка' (меню)


def _tg_emoji(eid: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


PAID_WAIT_TEXT = f'{_tg_emoji(EMOJI_PAID, "⏳")} Оплата пришла'
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


GAME_PLAN = "game"                 # старый билет (цена/выигрыш из настроек GAME_*)
GAME_PREFIX = "game_"              # билет на тариф: game_6h — выигрыш 6ч
GAME_FACTOR = 0.7 / 1.2            # Maker: тариф 1.2 TON → билет 0.7 TON (так же для остальных)
# Telegram сам решает исход броска (sendDice) — бот на результат не влияет.
# Maker: шанс 40–50%. ⚽: 4,5 — гол (2 из 5 = 40%); 🏀: 4,5 — попал (2 из 5 = 40%).
GAME_BALLS = {"soccer": ("⚽", (3, 4, 5)), "basket": ("🏀", (4, 5))}  # Telegram: ⚽ гол 3-5, 🏀 попал 4-5


def is_game(code: str | None) -> bool:
    return bool(code) and (code == GAME_PLAN or code.startswith(GAME_PREFIX))


def game_ticket(plan: str) -> tuple[float, int | None]:
    """Цена билета «Испытать удачу» на тариф: (TON, Stars или None)."""
    _t, price, _s, stars = PLANS[plan]
    return (max(0.1, round(price * GAME_FACTOR, 1)),
            max(1, round(stars * GAME_FACTOR)) if stars else None)


def plan_info(code: str) -> tuple[str, float, float, int | None] | None:
    """PLANS + старые коды (для доставки оплат по счёту, выставленному до смены тарифов) + билет игры."""
    if code == GAME_PLAN:
        return (f"Билет: {settings.game_prize_hours} ч", float(settings.game_ton),
                settings.game_prize_hours * 3600.0, int(settings.game_stars) or None)
    if code and code.startswith(GAME_PREFIX) and code[len(GAME_PREFIX):] in PLANS:
        base = code[len(GAME_PREFIX):]
        title, _p, secs, _st = PLANS[base]
        ton, stars = game_ticket(base)
        return (f"Билет: {title}", ton, secs, stars)
    return PLANS.get(code) or _LEGACY_PLANS.get(code)


def btn(text: str, style: str | None = None, icon: str | None = None, **kw) -> InlineKeyboardButton:
    """Кнопка с цветом: 'success' — зелёная, 'primary' — синяя, 'danger' — красная, None — обычная.
    icon — ID кастомного эмодзи слева от текста (utils.emoji)."""
    if style:
        kw["style"] = style
    kw.update(_icon(icon))
    return InlineKeyboardButton(text=text, **kw)


def _disc(value: float, pct: int) -> float:
    return value * (100 - pct) / 100 if pct else value
INVOICE_TTL = 30 * 60           # Maker: 'ссылка на оплату действует 30 мин'
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


_BG_TASKS: set[asyncio.Task] = set()


def sub_chat_id() -> int:
    return settings.sub_chat_id or settings.target_chat_id


def _fmt_secs(secs: float) -> str:
    mins = int(round(secs / 60))
    h, m = divmod(mins, 60)
    return " ".join(x for x in (f"{h} ч" if h else "", f"{m} мин" if m or not h else "") if x)


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
        # self.bot — бот для операций в канале подписки (создание invite-ссылок, кик).
        # Это ВСЕГДА основной BOT_TOKEN: он уже админ в канале лотов, SUB_CHAT_ID по умолчанию
        # = TARGET_CHAT_ID, поэтому links работают. Sub-bot (siqveoabot) для Stars-invoice
        # используется отдельно через self.stars_bot (см. main.py).
        self.bot = app.bot
        self.stars_bot = app.bot            # перезаписывается на sub_bot в main.py если задан
        self.state: dict[str, Any] = {"users": {}, "invoices": {}, "links": {}, "seen_tx": [],
                                      "seen_charges": [],   # charge_id Stars-платежей — идемпотентность при ретрае getUpdates
                                      "trials_used": [],    # uid тех, кто УЖЕ брал пробный — durable, не теряется при деплое
                                      "link_dm": {},        # uid → message_id DM со ссылкой (удаляем при входе)
                                      "tariffs": {},        # code → [ton, stars] — цены, заданные через /tarif
                                      "sale": {}}           # {pct, until} — распродажа на все тарифы (/sale)
        self._lock = asyncio.Lock()
        self._loaded = False
        # пока копия подписок не восстановлена после деплоя — никого не кикаем и не выдаём пробный час
        self.ready = asyncio.Event()
        self._kick_grace_until = 0.0     # до этого времени после старта никого не кикаем (выставляется в run())
        self._backup_wanted = False
        self._backup_task: asyncio.Task | None = None
        self.degraded = False          # копия не восстановилась → не кикаем незнакомых, пробный час не даём
        self.on_change: Callable[[], Awaitable[Any]] | None = None   # сохранить копию после оплаты/входа
        # Per-invoice locks: не даём двум одновременным _deliver работать параллельно.
        # Maker: "когда много раз нажимаешь 'проверить оплату', там повторяется, а надо
        # чтоб один раз писало 'оплата получена, ожидайте'". Lock сериализует _deliver
        # на один invoice-код, второй клик ждёт первого и видит уже обновлённое состояние.
        self._deliver_locks: dict[str, asyncio.Lock] = {}
        # Rate-limit callback'а "проверить оплату": uid → ts последнего выхова. Между
        # кликами < 3 сек — тихий ответ без повторной проверки сети.
        self._check_last_ts: dict[int, float] = {}
        self._ref_busy: set[int] = set()
        self._chan_ok: dict[int, float] = {}          # uid → когда подтвердили подписку на канал
        self._chan_err_logged = 0.0

    def _user_bots(self) -> list:
        """Все боты, через которые юзеру может прийти DM со ссылкой (main + sub, если задан).
        В таком порядке: сначала главный (он создавал invite-link), потом sub как фоллбек."""
        bots = [self.bot]
        sb = getattr(self.app, "sub_bot", None)
        if sb is not None and sb is not self.bot:
            bots.append(sb)
        # stars_bot может быть тем же, что main — не дублируем
        if self.stars_bot is not None and self.stars_bot not in bots:
            bots.append(self.stars_bot)
        return bots

    async def _dm_user(self, uid: int, text: str, **kw) -> bool:
        """Отправить DM пользователю через любой доступный бот. → True если дошло.
        Если в тексте ссылка для входа — запоминаем message_id, чтобы удалить его при входе."""
        last_err = None
        is_link = ("t.me/+" in text) or ("ссылк" in text.lower())
        for bot in self._user_bots():
            try:
                msg = await bot.send_message(int(uid), text, **kw)
                if is_link and getattr(msg, "message_id", None):
                    self.state.setdefault("link_dm", {})[str(uid)] = msg.message_id
                return True
            except Exception as e:
                last_err = e
                continue
        if last_err is not None:
            log.info("DM to %s failed on all bots: %s", uid, last_err)
        return False

    def _uref(self, uid) -> str:
        """«@username (id)» для уведомлений об оплате — @ник если известен, иначе только id."""
        try:
            u = self.state["users"].get(str(uid)) or {}
        except Exception:
            u = {}
        un = u.get("username")
        return (f"@{un} (<code>{uid}</code>)" if un else f"<code>{uid}</code>")

    async def add_taken(self, uid: int, title: str) -> None:
        """Записать взятый лот в профиль пользователя (показывается в «Логи»)."""
        if not uid or not title:
            return
        try:
            async with self._lock:
                u = self.user(uid)
                lst = u.setdefault("taken", [])
                lst.append(title)
                if len(lst) > 30:
                    del lst[:-30]
            await self.save()
        except Exception as e:
            log.info("add_taken %s failed: %s", uid, e)

    async def _delete_link_dm(self, uid: int) -> None:
        """Удалить ранее отправленный DM со ссылкой для входа (человек уже зашёл — ссылка не нужна)."""
        mid = (self.state.get("link_dm") or {}).pop(str(uid), None)
        if not mid:
            return
        for bot in self._user_bots():
            try:
                await bot.delete_message(int(uid), mid)
                break
            except Exception:
                continue

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
        self._apply_tariffs()        # переприменяем сохранённые цены тарифов (переживают деплой)
        self._loaded = True

    def _apply_tariffs(self) -> None:
        """Применяет сохранённые через /tarif цены поверх PLANS (title и длительность — из кода)."""
        ovr = self.state.get("tariffs") or {}
        for code, pr in ovr.items():
            if code in PLANS and isinstance(pr, (list, tuple)) and len(pr) >= 2:
                title, _ton, secs, _stars = PLANS[code]
                try:
                    ton = float(pr[0])
                    stars = int(pr[1]) if pr[1] not in (None, "", "-") else None
                    PLANS[code] = (title, ton, secs, stars)
                except (TypeError, ValueError):
                    continue

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
                # Реф-программа переживает деплой: реф/партнёр-поля НЕ теряем при мердже —
                # счётчики берём по максимуму, флаги/привязку — если своего нет (у бэкапа есть).
                for cf in ("ref_count", "ref_paid", "ref_revenue_nano"):
                    if v.get(cf):
                        cur[cf] = max(int(cur.get(cf) or 0), int(v.get(cf) or 0))
                if v.get("ref_bonus"):
                    cur["ref_bonus"] = max(float(cur.get("ref_bonus") or 0), float(v.get("ref_bonus") or 0))
                for ff in ("ref_by", "partner", "partner_name", "partner_pct", "promo", "promos_used",
                           "offer_access"):
                    if not cur.get(ff) and v.get(ff):
                        cur[ff] = v[ff]
                if v.get("taken") and not cur.get("taken"):
                    cur["taken"] = v["taken"]            # логи взятых лотов переживают деплой
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
        if isinstance(data.get("seen_charges"), list):
            self.state["seen_charges"] = list(dict.fromkeys(
                data["seen_charges"] + (self.state.get("seen_charges") or [])))[-2000:]
        # реестр использованных пробных — только добавляем, никогда не теряем (иначе после деплоя
        # человек берёт пробный повторно → новая ссылка → снова вход; это и есть «постоянно заходят»)
        if isinstance(data.get("tariffs"), dict):
            self.state["tariffs"] = {**(self.state.get("tariffs") or {}), **data["tariffs"]}
        if isinstance(data.get("sale"), dict):
            self.state["sale"] = data["sale"]        # распродажа переживает деплой
        if isinstance(data.get("trials_used"), list):
            self.state["trials_used"] = list(dict.fromkeys(
                (self.state.get("trials_used") or []) + data["trials_used"]))
        # подстрахуемся: все, у кого в записи стоит trial=True, тоже попадают в durable-реестр
        for _k, _u in self.state.get("users", {}).items():
            if isinstance(_u, dict) and _u.get("trial"):
                try:
                    _iu = int(_k)
                except (TypeError, ValueError):
                    continue
                if _iu not in self.state["trials_used"]:
                    self.state["trials_used"].append(_iu)
        # оплата пришла, а выдача оборвалась перезапуском → повторить выдачу
        for i in self.state["invoices"].values():
            if i.get("paid") and i.get("granted") == "inflight":
                if i.get("paid_at"):
                    i["granted"] = False          # новая версия: время и отметка сохраняются вместе
                else:
                    # счёт старой версии: она могла успеть выдать время, но не отметить счёт —
                    # если у человека есть время после оплаты, считаем выданным (иначе выдали бы дважды)
                    u = self.state["users"].get(str(i.get("uid"))) or {}
                    secs = (plan_info(i.get("plan")) or (None, None, 0, None))[2]
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
            self._apply_tariffs()             # цены из /tarif переживают деплой и после восстановления копии
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
        # по две кнопки в ряд, у каждой свой цвет: зелёная, синяя, красная, обычная
        # (Telegram даёт кнопкам только эти 4 варианта цвета)
        # «Испытать удачу» — не здесь, а на экране выбора способа оплаты тарифа
        buttons = [
            btn("Оформить подписку", "success", "6037175527846975726", callback_data="sub:menu"),
            btn("Профиль", "primary", "6032693626394382504", callback_data="sub:profile"),
            btn("Реферальная система", "primary", E.SHOP, callback_data="sub:ref"),
            btn("Промокод", "danger", E.TAG, callback_data="sub:promo"),
        ]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def renew_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("Продлить подписку", "success", "6028315147754278596", callback_data="sub:menu"),
             btn("В меню", "primary", "6032742198179532882", callback_data="sub:home")],
        ])

    async def _notify_expired(self, uid: int, u: dict | None) -> None:
        """ЛС после кика: срок закончился + кнопка «Продлить подписку».
        Один раз на каждый срок (expired_notified = until), чтобы _expire и _reconcile
        не слали одно и то же дважды. Если человек ни разу не открывал бота, Telegram не даёт
        написать первым — это нормально, просто пишем в лог."""
        u = u if u is not None else self.state["users"].get(str(uid))
        if u is None:
            return
        until = u.get("until")
        if u.get("expired_notified") == until:
            return
        u["expired_notified"] = until
        started = float(u.get("started") or 0)
        was_trial = bool(u.get("trial")) and until not in (None, FOREVER) and started \
            and float(until) - started <= TRIAL_SECONDS + 60
        if was_trial:
            text = ("<b>Пробный период закончился</b>\n\n"
                    "Вы удалены из канала, оформите подписку ниже чтоб вернуться.")
        else:
            text = ("Срок вашей подписки окончен " + _tg_emoji("5778570255555105942", "❗️") + "\n\n"
                    "<b>Вы удалены из чата, продлите подписку чтоб вернуться.</b>")
        if not await self._dm_user(uid, text, reply_markup=self.renew_kb()):
            log.info("Expired-notice to %s not delivered (бот не может написать первым)", uid)

    def plans_kb(self, uid: int) -> InlineKeyboardMarkup:
        rows = []
        if not self.user(uid).get("trial"):
            rows.append([btn("30 мин бесплатно", "success", E.GIFT, callback_data="sub:trial")])
        pct = max(self.promo_pct(uid), self.sale_pct())      # распродажа или промокод — что больше
        styles = ("primary", "danger", "success")            # тарифы — разными цветами, без «пустых»
        for i, (code, (title, price, secs_, stars)) in enumerate(PLANS.items()):
            p_ton = _disc(price, pct)
            p_st = max(1, round(_disc(stars, pct))) if stars else None
            # кастомные эмодзи внутри текста кнопки Telegram не показывает — тут обычные символы
            # Maker: в тарифах эмодзи не нужны вообще — ни иконки, ни ⭐ в тексте
            label = f"{title} • {p_ton:.4g} TON" + (f" / {p_st} Stars" if p_st else "")
            rows.append([btn(label, styles[i % len(styles)], None,
                             callback_data=f"sub:buy:{code}")])
        rows.append([btn("Назад", "danger", E.BACK, callback_data="sub:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def plans_text(self, uid: int) -> str:
        """«Подписка» + цены тарифов: 1.2 <TON> / 150 <Stars> (кастомные эмодзи Maker'а)."""
        sale = self.sale_pct()
        promo = self.promo_pct(uid)
        pct = max(promo, sale)
        t, st = _tg_emoji(EMOJI_TON, "💎"), _tg_emoji(EMOJI_STAR, "⭐️")
        lines = []
        for _code, (title, price, _, stars) in PLANS.items():
            p_ton = _disc(price, pct)
            p_st = max(1, round(_disc(stars, pct))) if stars else None
            lines.append(f"<b>{title}</b> — {p_ton:.4g} {t}" + (f" / {p_st} {st}" if p_st else ""))
        if sale and sale >= promo:
            disc = f"\n\n🔥 <b>РАСПРОДАЖА −{sale}% на все тарифы!</b>"
        elif promo:
            disc = f"\n\nСкидка по промокоду: <b>−{promo}%</b>"
        else:
            disc = ""
        return (f'{_tg_emoji(EMOJI_SUB, "💎")} <b>Подписка</b>\n\n' + "\n".join(lines) + disc
                + "\n\nВыберите тариф")

    # ------------------------------------------------------------------ промокоды
    def promo_pct(self, uid: int) -> int:
        """Скидка активированного пользователем промокода (0 — нет)."""
        u = self.state["users"].get(str(uid)) or {}
        code = u.get("promo")
        p = (self.state.get("promos") or {}).get(code) if code else None
        # Лимит промокода проверяется при активации (по вводам). Если пользователь уже активировал
        # код — скидка действует, пока код не выключен (не гейтим по оплатам uses — это был баг).
        if not p or p.get("off"):
            return 0
        return max(0, min(95, int(p.get("pct") or 0)))

    async def activate_promo(self, uid: int, code: str) -> str:
        code = (code or "").strip().upper()
        async with self._lock:
            p = (self.state.get("promos") or {}).get(code)
            if not p or p.get("off"):
                return "Такого промокода нет."
            u = self.user(uid)
            al = p.setdefault("activated_by", [])
            # этот пользователь уже активировал код — не считаем повторно, но и не отказываем
            if uid in al:
                u["promo"] = code
                return "Вы уже активировали промокод, оплачивайте подписку."
            # лимит по АКТИВАЦИЯМ (вводам), а не по оплатам — после N вводов код недействителен
            mx = int(p.get("max") or 0)
            if mx and len(al) >= mx:
                return "Промокод недействителен — закончились активации."
            if code in (u.get("promos_used") or []):
                return "Вы уже использовали этот промокод."
            u["promo"] = code
            u.pop("await_promo", None)
            al.append(uid)                       # запоминаем, кто вводил (для /promo_users и лимита)
            await self.save(backup=True)
        return f"Промокод <b>{html.escape(code)}</b> активирован: скидка <b>−{int(p.get('pct') or 0)}%</b> на следующую оплату."

    # ------------------------------------------------------------------ рефералы
    def ref_info(self, uid: int) -> dict:
        u = self.state["users"].get(str(uid)) or {}
        return {"count": int(u.get("ref_count") or 0), "paid": int(u.get("ref_paid") or 0),
                "bonus": float(u.get("ref_bonus") or 0)}

    async def register_ref(self, uid: int, ref: int, user=None) -> bool:
        """Новый пользователь пришёл по реф-ссылке. Засчитываем один раз и только новым."""
        if not ref or ref == uid:
            return False
        async with self._lock:
            if str(uid) in self.state["users"] or str(ref) not in self.state["users"]:
                return False                     # уже был в боте / пригласивший неизвестен
            u = self.user(uid)
            u["ref_by"] = int(ref)
            self._remember_who(u, user)
            r = self.user(ref)
            r["ref_count"] = int(r.get("ref_count") or 0) + 1
            await self.save(backup=True)
        await self._dm_user(ref, "По вашей ссылке пришёл новый пользователь. Когда он оплатит подписку, "
                                 f"вам начислится <b>{settings.ref_bonus_minutes} мин</b>.")
        return True

    async def _credit_ref(self, inv: dict) -> None:
        """Оплата подписки приглашённым → засчитываем пригласившему/партнёру (кол-во оплат + выручка),
        и, если задан REF_BONUS_MINUTES, начисляем бонус-минуты. Один раз на счёт."""
        if inv.get("ref_credited") or is_game(inv.get("plan")):
            return
        uid = int(inv.get("uid") or 0)
        u = self.state["users"].get(str(uid)) or {}
        ref = int(u.get("ref_by") or 0)
        if not ref or ref == uid:
            return
        async with self._lock:
            inv["ref_credited"] = True
            r = self.user(ref)
            r["ref_paid"] = int(r.get("ref_paid") or 0) + 1
            r["ref_revenue_nano"] = int(r.get("ref_revenue_nano") or 0) + int(inv.get("nano") or 0)
            if settings.ref_bonus_minutes:
                r["ref_bonus"] = float(r.get("ref_bonus") or 0) + settings.ref_bonus_minutes * 60
            await self.save(backup=True)
            total = float(r.get("ref_bonus") or 0)
        if settings.ref_bonus_minutes:
            need = settings.ref_min_minutes * 60
            tail = (" — можно активировать в «Реферальная система»." if total >= need
                    else f" Активация от {_fmt_secs(need)}.")
            await self._dm_user(ref, f"Ваш реферал оплатил подписку: <b>+{settings.ref_bonus_minutes} мин</b>.\n"
                                     f"Накоплено: <b>{_fmt_secs(total)}</b>." + tail)

    async def activate_ref_bonus(self, uid: int) -> str:
        if uid in self._ref_busy:
            return ""
        self._ref_busy.add(uid)
        try:
            async with self._lock:
                bonus = float(self.user(uid).get("ref_bonus") or 0)
            if bonus < max(60, settings.ref_min_minutes * 60):
                return ""
            text = await self.grant(uid, bonus, f"Реферальный бонус активирован: {_fmt_secs(bonus)}.")
            async with self._lock:
                u = self.user(uid)
                u["ref_bonus"] = max(0.0, float(u.get("ref_bonus") or 0) - bonus)
                await self.save(backup=True)
            return text
        finally:
            self._ref_busy.discard(uid)

    # ------------------------------------------------------------------ бан / обязательная подписка
    def is_banned(self, uid: int) -> bool:
        return bool((self.state["users"].get(str(uid)) or {}).get("banned"))

    async def in_channel(self, bot, uid: int) -> bool:
        """Подписан ли на REQUIRED_CHANNEL. Если бот не может проверить (не админ канала) —
        не блокируем людей, а раз в 10 минут пишем в лог."""
        ch = settings.required_channel
        if not ch:
            return True
        now = time.time()
        if now - self._chan_ok.get(uid, 0) < 600:
            return True
        last_err = None
        for b in [x for x in (bot, self.bot) if x is not None]:
            try:
                m = await b.get_chat_member(ch, uid)
                status = m.status
            except Exception as e:
                last_err = e
                continue
            ok = status in ("member", "administrator", "creator") or (
                status == "restricted" and getattr(m, "is_member", False))
            if ok:
                self._chan_ok[uid] = now
                if len(self._chan_ok) > 50_000:
                    self._chan_ok = {k: v for k, v in self._chan_ok.items() if now - v < 600}
            return ok
        if now - self._chan_err_logged > 600:
            self._chan_err_logged = now
            log.warning("Обязательная подписка: не могу проверить %s (%s) — бот должен быть админом "
                        "канала. Пока пропускаю всех.", ch, last_err)
        return True

    def channel_gate_kb(self) -> InlineKeyboardMarkup:
        ch = settings.required_channel
        url = f"https://t.me/{ch.lstrip('@')}" if ch.startswith("@") else "https://t.me/"
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("Подписаться", "primary", E.POINT, url=url)],
            [btn("Проверить подписку", "success", E.ROBOT, callback_data="sub:chk")],
        ])

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
        # снимаем бан (если был кикнут за истечение) — иначе забаненный не зайдёт по новой ссылке
        try:
            await self.bot.unban_chat_member(sub_chat_id(), uid, only_if_banned=True)
        except Exception as e:
            log.info("Unban before grant for %s skipped: %s", uid, e)
        async with self._lock:
            u = self.user(uid)
            u.pop("unpaid_join", None)
            st = await self._member_status(uid)
            if st is None:
                raise RuntimeError("get_chat_member failed")
            if st in ("member", "administrator", "creator", "restricted"):
                # уже в канале — продлеваем. Но ссылку ВСЁ РАВНО даём (Maker: 'жмёшь
                # 30 мин, пишет что активировано, а ссылку не даёт'). Админ/действующий
                # член может пригодиться для теста / поделиться с другом.
                pend = u.get("pending") or 0
                if seconds == FOREVER or u["until"] == FOREVER or pend == FOREVER:
                    u["until"] = FOREVER
                else:
                    u["until"] = max(time.time(), float(u["until"] or 0)) + seconds + float(pend)
                u["pending"], u["in_chat"] = 0, True
                if inv is not None:
                    inv["granted"] = True      # отметка «выдано» — в том же сохранении, что и время
                # попытка создать ссылку — если не получилось, просто возвращаем подтверждение
                link_text = ""
                try:
                    link = await self.bot.create_chat_invite_link(
                        sub_chat_id(), name=f"sub {uid}"[:32], member_limit=1,
                        expire_date=int(time.time() + LINK_TTL))
                    self.state["links"][link.invite_link] = {"uid": uid, "ts": time.time()}
                    link_text = f"\n\nСсылка для входа:\n{link.invite_link}"
                except Exception as e:
                    log.info("extend-only grant: invite link creation skipped (%s)", e)
                await self.save(backup=True)
                return f"{reason}\nПодписка продлена: осталось <b>{_fmt_left(u['until'])}</b>." + link_text
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
        return (f"{reason}\n\n"
                f"Ваша одноразовая ссылка для входа:\n{link.invite_link}")

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
        await self._delete_link_dm(uid)     # человек зашёл — удаляем DM со ссылкой
        try:
            await self._dm_user(uid, f"Вы в канале! Доступ: <b>{_fmt_left(self.user(uid)['until'])}</b>.")
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
        # выкинут — только в лог, админам НЕ шлём (dj: не флудить; остаются только оплаты)
        who = f"@{user.username}" if getattr(user, "username", None) else getattr(user, "first_name", "") or uid
        log.warning("User %s (%s) joined without subscription — kicked", uid, who)

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
            if isinstance(self.state.get("seen_charges"), list) and len(self.state["seen_charges"]) > 2000:
                self.state["seen_charges"] = self.state["seen_charges"][-2000:]
            if drop:
                await self.save(backup=True)
                log.info("Subscriptions cleanup: removed %d old invoices (%d → %d)",
                         len(drop), before, len(self.state["invoices"]))
        return len(drop)

    async def _reconcile(self) -> None:
        """Страховка на случай, если Telegram не прислал событие входа/выхода: всех, у кого доступ
        закончился, проверяем по факту — если человек всё ещё в канале, выкидываем."""
        if time.time() < getattr(self, "_kick_grace_until", 0):
            return                      # grace после старта — не кикаем, пока копия не устоялась
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
                        renewed = self._has_access(u) or u.get("pending")
                    if renewed:
                        # продлил, пока мы его кикали — новая ссылка вместо «срок окончен»
                        try:
                            text = await self.grant(uid, 0, "Подписка продлена, держите ссылку для входа.")
                            await self._dm_user(uid, text)
                        except Exception as e:
                            log.warning("Re-link after renewal during reconcile failed for %s: %s", uid, e)
                        continue
                    log.warning("Reconcile: user %s still in channel after subscription ended — kicked", uid)
                    if not u.get("unpaid_join"):
                        await self._notify_expired(uid, u)
                        await self.save()

    async def kick(self, uid: int) -> bool:
        """Удалить из канала и НЕ дать перезайти: бан ДЕРЖИМ, пока человек не получит доступ снова
        (его снимает grant() при выдаче/оплате). Раньше был ban → сразу unban «чтоб не было ЧС» —
        но тогда любой с рабочей ссылкой (общая t.me/+…, сохранённая invite) мгновенно заходил
        обратно, reconcile снова кикал → бесконечный цикл «кик ↔ вход». Теперь цикла нет."""
        chat = sub_chat_id()
        try:
            await self.bot.ban_chat_member(chat, uid)      # без until_date = бан держится до grant()/оплаты
        except Exception as e:
            es = str(e).lower()
            if "participant" in es or "user not found" in es or "user_not_participant" in es:
                return True      # его и так нет в канале
            if "chat owner" in es or "administrator" in es or "admin" in es:
                # владельца / админа канала удалить нельзя — не повторяем попытку бесконечно
                log.info("Kick %s skipped: owner/admin of the channel", uid)
                return True
            log.warning("Kick %s failed: %s", uid, e)
            return False
        await self._revoke_links(uid)        # гасим его одноразовые ссылки, чтобы точно не перезашёл
        return True

    # ------------------------------------------------------------------ payments (TON)
    def invoice_url(self, code: str) -> str:
        """Tonkeeper universal link — открывает Tonkeeper с подставленными адресом,
        суммой и комментом. ВНИМАНИЕ: Bot API inline-кнопки принимают ТОЛЬКО http/https
        URL — custom-scheme (ton://) Telegram обрабатывает как «контент временно
        недоступен» и кнопка не работает. Поэтому только https://app.tonkeeper.com/."""
        inv = self.state["invoices"][code]
        return (f"https://app.tonkeeper.com/transfer/{settings.sub_ton_address}"
                f"?amount={int(inv['nano'])}&text={quote(code)}")

    def sale_pct(self) -> int:
        """Текущая распродажа (/sale) — скидка на ВСЕ тарифы для всех. 0 если нет/истекла."""
        s = self.state.get("sale") or {}
        pct = int(s.get("pct") or 0)
        if pct <= 0:
            return 0
        until = float(s.get("until") or 0)
        if until and time.time() > until:
            return 0
        return min(90, pct)

    def _promo_for(self, uid: int, plan: str) -> tuple[int, str | None]:
        if is_game(plan):
            return 0, None                     # на билет игры промокод/распродажа не действуют
        pct = self.promo_pct(uid)
        sale = self.sale_pct()
        if sale > pct:                          # распродажа лучше промокода — применяем её (без стака)
            return sale, "SALE"
        return (pct, (self.state["users"].get(str(uid)) or {}).get("promo")) if pct else (0, None)

    async def new_invoice(self, uid: int, plan: str, ball: str | None = None) -> str:
        pct, promo = self._promo_for(uid, plan)
        nano = int(round(_disc(plan_info(plan)[1], pct) * 1e9))
        async with self._lock:
            # один активный счёт на тариф (и ту же скидку / тот же мяч) — не плодим новые при повторном нажатии
            for code, inv in self.state["invoices"].items():
                if (inv["uid"] == uid and inv["plan"] == plan and not inv.get("paid")
                        and inv.get("kind", "ton") == "ton" and int(inv.get("pct") or 0) == pct
                        and inv.get("game") == ball and not inv.get("got")
                        and time.time() - inv["ts"] < INVOICE_TTL):
                    return code
            code = "TRK" + secrets.token_hex(4).upper()
            self.state["invoices"][code] = {"code": code, "uid": uid, "plan": plan, "nano": nano,
                                            "ts": time.time(), "paid": False, "kind": "ton",
                                            "pct": pct, "promo": promo, "game": ball}
            await self.save()
        if self.on_change:
            # счёт — и в резервную копию, чтобы оплата после деплоя не потерялась (вне общего лока)
            try:
                await self.on_change()
            except Exception as e:
                log.warning("Backup after new invoice failed: %s", e)
        return code

    async def new_stars_invoice(self, uid: int, plan: str, ball: str | None = None) -> str:
        """Создать Stars-invoice через Bot API (createInvoiceLink). → https://t.me/$… URL.
        Один живой инвойс на (uid, plan) — повторный клик отдаёт ту же ссылку, чтобы Telegram
        не заваливали десятками активных счетов."""
        title, _price, _secs, stars = plan_info(plan)
        if not stars:
            raise ValueError(f"plan {plan!r} has no stars price")
        pct, promo = self._promo_for(uid, plan)
        stars = max(1, int(round(_disc(stars, pct))))
        now = time.time()
        async with self._lock:
            for code, inv in self.state["invoices"].items():
                if (inv.get("kind") == "stars" and inv["uid"] == uid and inv["plan"] == plan
                        and not inv.get("paid") and inv.get("stars_url") and int(inv.get("stars") or 0) == stars
                        and inv.get("game") == ball
                        and now - inv["ts"] < INVOICE_TTL):
                    return inv["stars_url"]
            code = "STR" + secrets.token_hex(4).upper()
        # создание ссылки — вне лока: HTTP-вызов Bot API может висеть, не держим подписки.
        # Используем stars_bot (может быть sub_bot = @siqveoabot), чтобы Stars-платёж открывался
        # в правильном боте, а pre_checkout/successful_payment приходили туда.
        url = await self.stars_bot.create_invoice_link(
            title=f"NFT Tracker — {title}",
            description=(f"Испытать удачу: {GAME_BALLS.get(ball, ('?',))[0]} — {title}"
                         if is_game(plan) else f"Доступ к трекеру: тариф {title}"),
            payload=f"{code}:{plan}",     # тариф в payload → автовыдача даже если счёт потерян
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=int(stars))],
        )
        # Bot API иногда отдаёт URL без схемы — inline-кнопка на 't.me/$…' не работает,
        # нужно 'https://t.me/$…'
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        async with self._lock:
            self.state["invoices"][code] = {
                "code": code, "uid": uid, "plan": plan, "kind": "stars", "stars": int(stars),
                "stars_url": url, "ts": now, "paid": False, "pct": pct, "promo": promo, "game": ball,
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
        title, price, _secs, _stars = plan_info(plan)
        pct, promo = self._promo_for(uid, plan)
        price = round(_disc(price, pct), 4)
        now = time.time()
        async with self._lock:
            for code, inv in self.state["invoices"].items():
                if (inv.get("kind") == "crypto" and int(inv.get("pct") or 0) == pct and inv["uid"] == uid and inv["plan"] == plan
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
                    "code": code, "uid": uid, "plan": plan, "kind": "crypto",
                    "nano": int(round(price * 1e9)),
                    "pay_url": pay_url, "invoice_id": int(invoice_id),
                    "asset": "TON", "amount": str(price),
                    "ts": now, "paid": False, "pct": pct, "promo": promo,
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
                "code": code, "uid": uid, "plan": plan, "kind": "crypto",
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
        now = time.time()
        # свежие первыми и только за 7 дней: раньше брались первые 100 по порядку вставки —
        # при >100 висящих счетах новые не проверялись никогда и оплата терялась
        pending = sorted(((code, int(i["invoice_id"]), float(i.get("ts") or 0))
                          for code, i in self.state["invoices"].items()
                          if i.get("kind") == "crypto" and not i.get("paid") and i.get("invoice_id")
                          and now - float(i.get("ts") or 0) < 7 * 86400),
                         key=lambda x: -x[2])
        pending = [(c, iid) for c, iid, _ in pending[:300]]
        if not pending:
            return 0
        by_id: dict[int, dict] = {}
        for k in range(0, len(pending), 100):           # Crypto Bot отдаёт не больше 100 id за раз
            ids = [str(iid) for _, iid in pending[k:k + 100]]
            try:
                res = await _crypto_pay_call("getInvoices", {"invoice_ids": ",".join(ids), "count": len(ids)})
            except Exception as e:
                log.warning("CryptoPay getInvoices failed: %s", e)
                break
            for x in res.get("items") or []:
                if isinstance(x, dict):
                    by_id[int(x.get("invoice_id") or 0)] = x
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
            title, price, _secs, _stars = plan_info(inv["plan"]) or ("?", 0, 0, None)
            log.info("CryptoPay payment received: user=%s plan=%s", inv["uid"], inv["plan"])
            ok = await self._deliver(inv)
            for admin in settings.admin_ids:
                try:
                    await self.bot.send_message(admin,
                        f"Crypto Bot: {inv.get('paid_nano', 0) / 1e9:g} TON, "
                        f"тариф {title}, пользователь {self._uref(inv['uid'])}"
                        + ("" if ok else "\nСсылку выдать не удалось, повторим автоматически."))
                except Exception:
                    pass
        return len(paid)

    async def on_stars_paid(self, payload: str, uid: int, total_amount: int,
                            charge_id: str | None = None) -> None:
        """successful_payment.invoice_payload (= «code:plan») → отметить оплату и выдать доступ."""
        code, _, plan_hint = (payload or "").partition(":")      # payload = «code:plan»
        async with self._lock:
            # Идемпотентность по charge_id: ретрай getUpdates (после краха до коммита offset)
            # не должен выдать доступ дважды — особенно на синтетическом пути, где code мог
            # потеряться и каждый повтор плодил бы новый случайный счёт.
            seen_ch = self.state.setdefault("seen_charges", [])
            if charge_id and charge_id in set(seen_ch):
                return                 # этот платёж уже обработан
            inv = self.state["invoices"].get(code)
            if inv and inv.get("kind") == "stars" and inv.get("paid") and inv.get("charge_id") == charge_id:
                return                 # повторный апдейт того же платежа
            # invoice-ссылку можно оплатить повторно (другой charge_id) — это НОВЫЕ деньги, не теряем молча
            unknown = not inv or inv.get("kind") != "stars" or bool(inv.get("paid"))
            if not unknown:
                if int(inv.get("uid") or 0) != int(uid):
                    log.warning("Stars payment %s uid mismatch: inv=%s pay=%s", payload, inv.get("uid"), uid)
                inv["paid"] = True
                inv["paid_at"] = time.time()
                inv["granted"] = "inflight"
                inv["charge_id"] = charge_id
                inv["paid_stars"] = int(total_amount)
                if charge_id:
                    seen_ch.append(charge_id)
                    self.state["seen_charges"] = seen_ch[-2000:]
                self.state["revenue_stars"] = int(self.state.get("revenue_stars") or 0) + int(total_amount)
                self.state["paid_count_stars"] = int(self.state.get("paid_count_stars") or 0) + 1
                await self.save(backup=True)
        if unknown:
            # Счёт потерян (напр. сброс состояния), но тариф зашит в payload — выдаём АВТОМАТИЧЕСКИ.
            # Игру (бросок мяча) так не восстановить — её по-прежнему добиваем вручную.
            if plan_hint in PLANS and not is_game(plan_hint):
                syn_code = code or ("STR" + secrets.token_hex(4).upper())
                syn = {"code": syn_code, "uid": int(uid), "plan": plan_hint, "kind": "stars",
                       "stars": int(total_amount), "paid": True, "paid_at": time.time(),
                       "granted": "inflight", "charge_id": charge_id, "ts": time.time(),
                       "paid_stars": int(total_amount)}
                async with self._lock:
                    self.state["invoices"][syn_code] = syn
                    if charge_id:
                        sc = self.state.setdefault("seen_charges", [])
                        sc.append(charge_id)
                        self.state["seen_charges"] = sc[-2000:]
                    self.state["revenue_stars"] = int(self.state.get("revenue_stars") or 0) + int(total_amount)
                    self.state["paid_count_stars"] = int(self.state.get("paid_count_stars") or 0) + 1
                    await self.save(backup=True)
                await self._deliver(syn)
                log.warning("Stars payment %s: счёт потерян, авто-выдал по payload (plan=%s) для %s",
                            total_amount, plan_hint, uid)
                return
            log.warning("Stars payment %s for unknown/paid invoice %r from %s", total_amount, payload, uid)
            for admin in settings.admin_ids:
                try:
                    await self.bot.send_message(
                        admin, f"Пришла оплата <b>{total_amount} Stars</b> от <code>{uid}</code>, но счёт "
                               f"<code>{html.escape(str(payload)[:64])}</code> не найден или уже оплачен.\n"
                               f"Выдать вручную: <code>/grant {uid} 24</code>"
                               + (f"\nchargeID: <code>{html.escape(str(charge_id))}</code>" if charge_id else ""))
                except Exception:
                    pass
            await self._dm_user(uid, "Оплата получена. Админ проверит её и выдаст доступ вручную.")
            return
        await self.flush_backup()
        await self._deliver(inv)
        pinfo = plan_info(inv["plan"]) or ("?", 0.0, 0.0, None)
        title = pinfo[0]
        for admin in settings.admin_ids:
            try:
                await self.bot.send_message(
                    admin, f"Оплата <b>{total_amount} Stars</b>, тариф {title}, "
                           f"пользователь {self._uref(uid)}")
            except Exception:
                pass

    async def _fetch_txs(self) -> list[dict]:
        """Последние входящие переводы. Листаем назад (до 6 страниц по 50), пока не дойдём до уже
        обработанного перевода или до времени старше самого старого открытого счёта.
        Если toncenter (бесплатный, 1 rps) не отвечает — фоллбек на tonapi.io."""
        seen = set(self.state.get("seen_tx") or [])
        open_ts = [float(i.get("ts") or 0) for i in self.state["invoices"].values()
                   if not i.get("paid") and i.get("kind", "ton") == "ton"]
        oldest = min(open_ts) if open_ts else time.time()
        out: list[dict] = []
        tc_err: str | None = None
        # toncenter
        try:
            url = "https://toncenter.com/api/v2/getTransactions"
            headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
            # archival=true: спрашиваем архивный узел — у не-архивного часто «lt not in db»
            # (cannot find block) на свежих переводах, из-за чего оплата не находилась
            params = {"address": settings.sub_ton_address, "limit": "50", "archival": "true"}
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
            if out:
                return out
            tc_err = "toncenter вернул пусто"
        except Exception as e:
            tc_err = str(e)[:200]
            log.warning("toncenter fetch failed, пробую tonapi: %s", tc_err)
        # фоллбек: tonapi.io (/v2/blockchain/accounts/{addr}/transactions)
        try:
            tonapi_url = (f"https://tonapi.io/v2/blockchain/accounts/"
                          f"{settings.sub_ton_address}/transactions?limit=50")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(tonapi_url) as r:
                    data = await r.json(content_type=None)
            # tonapi отдаёт свою структуру — приводим к tonapi-формату совместимому с
            # остальным кодом (in_msg.source/value/message + transaction_id.hash/lt)
            for tx in (data.get("transactions") or []):
                im = (tx.get("in_msg") or {})
                src = ((im.get("source") or {}).get("address")
                       if isinstance(im.get("source"), dict) else im.get("source"))
                msg_text = ""
                dec = im.get("decoded_body") or {}
                if isinstance(dec, dict):
                    msg_text = dec.get("text") or ""
                normalized = {
                    "transaction_id": {"hash": tx.get("hash"), "lt": str(tx.get("lt") or "")},
                    "utime": tx.get("utime"),
                    "in_msg": {
                        "source": src,
                        "message": msg_text,
                        "value": im.get("value") or 0,
                    },
                    "out_msgs": [{
                        "destination": ((o.get("destination") or {}).get("address")
                                        if isinstance(o.get("destination"), dict) else o.get("destination")),
                        "value": o.get("value") or 0,
                    } for o in (tx.get("out_msgs") or []) if isinstance(o, dict)],
                }
                out.append(normalized)
            return out
        except Exception as e:
            log.warning("tonapi fallback failed: %s", e)
            raise RuntimeError(tc_err or str(e)[:200])

    async def check_payments(self) -> int:
        """Найти оплаченные счета по комментарию и сумме. → сколько оплачено сейчас."""
        # ссылка на оплату живёт INVOICE_TTL, но опоздавший перевод по счёту всё равно засчитываем (7 дней)
        # только TON-счета: у Stars/Crypto нет суммы в nano (KeyError ронял всю проверку), и TON-перевод
        # с их кодом не должен закрывать счёт, который человек может оплатить ещё раз в CryptoBot/Stars
        open_codes = {c for c, i in self.state["invoices"].items()
                      if not i.get("paid") and i.get("kind", "ton") == "ton" and i.get("nano")
                      and time.time() - float(i.get("ts") or 0) < 7 * 86400}
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
                                                       f"пользователь {self._uref(inv['uid'])}"
                                                       + ("" if ok else "\nСсылку выдать не удалось, бот "
                                                          "повторит сам. Проверьте права бота в канале."))
                except Exception:
                    pass
        return len(paid)

    async def _deliver(self, inv: dict) -> bool:
        """Выдать доступ по оплаченному счёту. Если не вышло — повторим в фоне (оплата не теряется).
        Поток Maker'а:
          1) DM «Оплата получена, ожидайте» — сразу после фиксации платежа.
          2) grant() → ссылка на вход.
          3) DM с текстом «тариф Xч. … ссылка …».
          4) Реакция 🎉 на это сообщение (одна эмодзи-реакция от имени бота).

        Per-invoice lock: параллельные клики "проверить оплату" сериализуются,
        второй видит notice_sent=True и не шлёт повторный pre-notice."""
        # ключ — пользователь: два разных счёта одного человека тоже не должны выдаваться параллельно
        # (оба grant() иначе создают по ссылке и дважды шлют «оплата получена»)
        code = f"uid:{inv.get('uid')}"
        lock = self._deliver_locks.setdefault(code, asyncio.Lock())
        try:
            async with lock:
                # Повторный вход (второй клик после первого успешного _deliver) — быстрый выход.
                if inv.get("granted") is True:
                    return True
                return await self._deliver_locked(inv)
        finally:
            if (not lock.locked() and not getattr(lock, "_waiters", None)
                    and self._deliver_locks.get(code) is lock):
                self._deliver_locks.pop(code, None)      # словарь локов не растёт бесконечно

    async def _deliver_locked(self, inv: dict) -> bool:
        pinfo = plan_info(inv["plan"])
        if pinfo is None:
            log.warning("Unknown plan %r on invoice %s; cannot deliver", inv.get("plan"),
                        inv.get("code") or inv.get("uid"))
            return False
        title, price, secs, _stars = pinfo
        uid = int(inv["uid"])
        if is_game(inv.get("plan")):
            return await self._play(inv)
        # Maker: короткая строка — только «тариф 6ч.», без 'Оплата 2.2 TON пришла, '.
        reason = f"тариф {title}."
        # 1) «Оплата получена, ожидайте.» — pre-notice (пока создаём invite-link)
        if not inv.get("notice_sent"):
            try:
                await self._dm_user(uid, PAID_WAIT_TEXT)
                inv["notice_sent"] = True
            except Exception as e:
                log.info("pre-notice 'оплата получена' failed for %s: %s", uid, e)
        inv["granted"] = "inflight"
        try:
            text = await self.grant(uid, secs, reason, inv=inv)
        except Exception as e:
            inv["granted"] = False          # повторит _retry_undelivered
            await self.save()
            log.error("Grant after payment failed for %s: %s (will retry)", uid, e)
            return False
        await self._after_paid(inv)
        # 2+3) DM с ссылкой — пробуем ОБА бота. Получаем (bot, Message) — нужен message_id
        # для реакции.
        sent_bot, sent_msg = None, None
        for bot in self._user_bots():
            try:
                sent_msg = await bot.send_message(uid, text)
                sent_bot = bot
                if ("t.me/+" in text) and getattr(sent_msg, "message_id", None):
                    self.state.setdefault("link_dm", {})[str(uid)] = sent_msg.message_id
                break
            except Exception as e:
                log.info("DM via bot failed: %s", e)
                continue
        if sent_msg is None:
            log.warning("DM with invite link NOT delivered to %s (ни один бот не достучался)", uid)
            return True
        # 4) реакция 🎉 на наше же сообщение (Bot API: setMessageReaction)
        try:
            from aiogram.types import ReactionTypeEmoji
            await sent_bot.set_message_reaction(
                chat_id=uid, message_id=sent_msg.message_id,
                reaction=[ReactionTypeEmoji(emoji="🎉")], is_big=True)
        except Exception as e:
            log.info("reaction 🎉 skipped for %s: %s", uid, e)
        return True

    async def _after_paid(self, inv: dict) -> None:
        """После выдачи доступа по оплате: погасить промокод и начислить бонус пригласившему."""
        try:
            code = inv.get("promo")
            if code and not inv.get("promo_used"):
                async with self._lock:
                    inv["promo_used"] = True
                    p = (self.state.get("promos") or {}).get(code)
                    if p is not None:
                        p["uses"] = int(p.get("uses") or 0) + 1
                    u = self.user(int(inv["uid"]))
                    used = list(u.get("promos_used") or [])
                    if code not in used:
                        used.append(code)
                    u["promos_used"] = used[-50:]
                    if u.get("promo") == code:
                        u.pop("promo", None)
                    await self.save(backup=True)
            await self._credit_ref(inv)
        except Exception as e:
            log.warning("after-paid (promo/ref) failed for %s: %s", inv.get("code"), e)

    async def _play(self, inv: dict) -> bool:
        """Билет «Испытать удачу» оплачен → бросок мяча (Telegram sendDice — исход решает Telegram).
        Результат броска сохраняется ДО выдачи: повторная доставка не перебрасывает мяч."""
        uid = int(inv["uid"])
        emoji, wins = GAME_BALLS.get(inv.get("game") or "soccer", GAME_BALLS["soccer"])
        pinfo = plan_info(inv.get("plan") or GAME_PLAN) or plan_info(GAME_PLAN)
        prize = pinfo[2]
        prize_txt = "Навсегда" if prize == FOREVER else _fmt_secs(prize)
        retry_cb = (f"sub:game:{inv['plan'][len(GAME_PREFIX):]}"
                    if str(inv.get("plan") or "").startswith(GAME_PREFIX) else "sub:menu")
        if inv.get("dice") is None:
            msg = None
            for bot in self._user_bots():
                try:
                    msg = await bot.send_dice(uid, emoji=emoji)
                    break
                except Exception as e:
                    log.info("send_dice via bot failed for %s: %s", uid, e)
            if msg is None or getattr(msg, "dice", None) is None:
                inv["granted"] = False                  # повторим позже (_retry_undelivered)
                await self.save()
                return False
            async with self._lock:
                inv["dice"] = int(msg.dice.value)
                inv["win"] = inv["dice"] in wins
                await self.save(backup=True)
            await asyncio.sleep(4.5)                     # даём анимации броска доиграть
        if not inv.get("win"):
            inv["granted"] = True
            await self.save()
            await self._dm_user(uid, "Мимо — билет сгорел. Попробуй в следующий раз!",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [btn("Ещё попытка", "danger", E.CLOUD, callback_data=retry_cb)],
                                    [btn("Оформить подписку", "success", E.SUB, callback_data="sub:menu")]]))
            return True
        inv["granted"] = "inflight"
        try:
            text = await self.grant(uid, prize, f"{'Гол' if emoji == '⚽' else 'Попадание'}! "
                                                f"Выигрыш: {prize_txt} подписки.", inv=inv)
        except Exception as e:
            inv["granted"] = False
            await self.save()
            log.error("Game prize grant failed for %s: %s (will retry)", uid, e)
            return False
        await self._dm_user(uid, text)
        return True

    async def _retry_undelivered(self) -> None:
        # только явно неудавшиеся выдачи (старые счета без флага не трогаем — они уже выданы)
        for inv in [i for i in self.state["invoices"].values() if i.get("paid") and i.get("granted") is False]:
            await self._deliver(inv)

    # ------------------------------------------------------------------ background loop
    async def run(self) -> None:
        await self.ready.wait()
        await self.load()
        # grace после запуска/восстановления: 3 минуты НИКОГО не кикаем — даём копии полностью
        # устояться (оплаты восстановятся, unpaid_join снимется). Иначе после рестарта reconcile/
        # _expire выдавали всплеск киков легитимных людей (выигравших/с доступом), и они перезаходили.
        self._kick_grace_until = time.time() + 180
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
            await self._dm_user(
                int(k),
                f"Подписка закончится через {_fmt_left(float(until))}. "
                "Продлите, чтобы не потерять доступ.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    btn("Продлить", "success", E.BOLT, callback_data="sub:menu")]]))
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
        if now < getattr(self, "_kick_grace_until", 0):
            return                      # grace после старта — не кикаем, пока копия не устоялась
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
                    await self._dm_user(uid, text)
                except Exception as e:
                    log.warning("Re-link after renewal during kick failed for %s: %s", uid, e)
                continue
            if u.pop("unpaid_join", None):
                # зашёл без подписки, пока копия не читалась — это не «подписка закончилась»
                # (админам НЕ шлём — dj: не флудить; остаются только уведомления об оплатах)
                log.warning("User %s joined without subscription (while backup was unread) — kicked", uid)
                continue
            log.info("Subscription ended — user %s removed from channel", uid)
            await self._notify_expired(uid, u)
            await self.save()
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
        uid = m.from_user.id if m.from_user else 0
        # /start ref_<id> — реферальная ссылка (засчитывается только новым пользователям)
        mt = re.match(r"^/start(?:@\w+)?\s+ref_?(\d{1,16})\s*$", m.text or "")
        if mt and subs.ready.is_set():
            try:
                await subs.register_ref(uid, int(mt.group(1)), m.from_user)
            except Exception as e:
                log.warning("register ref failed: %s", e)
        mt_promo = re.match(r"^/start(?:@\w+)?\s+promo_([A-Za-z0-9_\-]{3,32})\s*$", m.text or "")
        if subs.is_banned(uid):
            await m.answer("Доступ к боту заблокирован.")
            return
        # запоминаем всех, кто запускал бота (для рассылки и чтобы список переживал деплой через бэкап)
        async with subs._lock:
            is_new = str(uid) not in subs.state["users"]
            u = subs.user(uid)
            subs._remember_who(u, m.from_user)
            u.setdefault("first_seen", time.time())
            u["last_start"] = time.time()
        # Пишем состояние ТОЛЬКО для нового юзера (durable-копия в Telegram переживает деплой).
        # Повторный /start не трогает БД — иначе на спаме /start лишняя нагрузка json.dumps на event loop.
        if is_new:
            await subs.save(backup=True)
        if not await subs.in_channel(m.bot, uid):
            await m.answer(GATE_TEXT, reply_markup=subs.channel_gate_kb())
            return
        if closed():
            # закрыто для новых, но у кого подписка есть — должен иметь возможность взять новую ссылку
            await m.answer(CLOSED_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                btn("Моя подписка", "primary", "5893168654551355607", callback_data="sub:my")]]))
            return
        # пришёл по ссылке «Активировать» из канала — сразу применяем промокод
        if mt_promo and subs.ready.is_set():
            await _promo_reply(m, mt_promo.group(1))
            return
        await m.answer(WELCOME, reply_markup=subs.main_kb())

    async def _blocked(q: CallbackQuery) -> bool:
        """Колбэк меню подписки от забаненного или не подписанного на канал → перехватываем."""
        if not (q.data or "").startswith("sub:") or q.data == "sub:chk" or q.from_user is None:
            return False
        uid = q.from_user.id
        if is_manager(uid):
            return False
        return subs.is_banned(uid) or not await subs.in_channel(q.bot, uid)

    @r.callback_query(_blocked)
    async def cb_gate(q: CallbackQuery) -> None:
        if subs.is_banned(q.from_user.id):
            await q.answer("Доступ к боту заблокирован.", show_alert=True)
            return
        await q.answer("Сначала подпишитесь на канал", show_alert=True)
        await _safe_edit(q.message, GATE_TEXT, reply_markup=subs.channel_gate_kb())

    @r.callback_query(F.data == "sub:chk")
    async def cb_chk(q: CallbackQuery) -> None:
        uid = q.from_user.id
        if subs.is_banned(uid):
            await q.answer("Доступ к боту заблокирован.", show_alert=True)
            return
        subs._chan_ok.pop(uid, None)
        if not await subs.in_channel(q.bot, uid):
            await q.answer("Подписка не найдена. Подпишитесь и нажмите ещё раз.", show_alert=True)
            return
        await q.answer("Готово!")
        await _safe_edit(q.message, WELCOME, reply_markup=subs.main_kb())

    # ---------------- реферальная система
    _me_cache: dict[int, str] = {}

    async def _bot_username(bot) -> str:
        if bot.id not in _me_cache:
            _me_cache[bot.id] = (await bot.get_me()).username or ""
        return _me_cache[bot.id]

    @r.callback_query(F.data == "sub:ref")
    async def cb_ref(q: CallbackQuery) -> None:
        await subs.load()
        uid = q.from_user.id
        subs._remember_who(subs.user(uid), q.from_user)
        info = subs.ref_info(uid)
        need = settings.ref_min_minutes * 60
        try:
            link = f"https://t.me/{await _bot_username(q.bot)}?start=ref_{uid}"
        except Exception:
            await q.answer("Telegram не ответил, попробуйте ещё раз", show_alert=True)
            return
        text = ("<b>Реферальная система</b>\n\n"
                "Приглашайте друзей по вашей ссылке и получайте "
                f"<b>{settings.ref_bonus_minutes} мин</b> подписки с каждой их оплаты!\n\n"
                f"Приглашено пользователей: <b>{info['count']}</b>\n"
                f"Оплат от приглашённых: <b>{info['paid']}</b>\n"
                f"У вас накопилось <b>{_fmt_secs(info['bonus'])}</b>"
                + (f" из <b>{_fmt_secs(need)}</b>" if info["bonus"] < need else "") + "\n\n"
                f"<i>Минимальный порог для активации — {_fmt_secs(max(60, need))}.</i>\n\n"
                f"<b>Ваша реферальная ссылка:</b>\n<code>{link}</code>")
        share = "https://t.me/share/url?" + "url=" + quote(link, safe="") + "&text=" + quote(
            "Трекер NFT-маркета Telegram — лоты в реальном времени", safe="")
        rows = [[btn("Поделиться ссылкой", "primary", E.SHARE, url=share)]]
        if info["bonus"] >= max(60, need):
            rows.append([btn(f"Активировать {_fmt_secs(info['bonus'])}", "success", E.PAID, callback_data="sub:refuse")])
        rows.append([btn("В меню", "danger", E.BACK, callback_data="sub:home")])
        await _safe_edit(q.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await q.answer()

    @r.callback_query(F.data == "sub:refuse")
    async def cb_ref_use(q: CallbackQuery) -> None:
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        try:
            text = await subs.activate_ref_bonus(q.from_user.id)
        except Exception as e:
            log.error("ref bonus activation failed for %s: %s", q.from_user.id, e)
            await q.answer("Не удалось активировать, попробуйте позже", show_alert=True)
            return
        if not text:
            await q.answer("Бонуса пока недостаточно", show_alert=True)
            return
        await q.answer("Бонус активирован!")
        try:
            await q.bot.send_message(q.from_user.id, text)
        except Exception:
            await subs._dm_user(q.from_user.id, text)

    # ---------------- промокоды
    @r.callback_query(F.data == "sub:promo")
    async def cb_promo(q: CallbackQuery) -> None:
        await subs.load()
        async with subs._lock:
            subs.user(q.from_user.id)["await_promo"] = time.time()
        pct = subs.promo_pct(q.from_user.id)
        cur = f"\n\nСейчас активна скидка <b>−{pct}%</b> на следующую оплату." if pct else ""
        await _safe_edit(q.message, "<b>Промокод</b>\n\nВведите промокод" + cur,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("В меню", "danger", E.BACK, callback_data="sub:home")]]))
        await q.answer()

    _PROMO_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_-]{2,32}$")

    def _awaiting_promo(m: Message) -> bool:
        if m.chat.type != "private" or not m.text or m.text.startswith("/") or m.from_user is None:
            return False
        # кодом считаем только «слово» (буквы/цифры/_/-): кнопки админ-панели и обычные фразы
        # не перехватываются и уходят своим обработчикам
        if not _PROMO_RE.match(m.text.strip()):
            return False
        u = subs.state["users"].get(str(m.from_user.id)) or {}
        return time.time() - float(u.get("await_promo") or 0) < 600

    async def _promo_reply(m: Message, code: str) -> None:
        uid = m.from_user.id
        if subs.is_banned(uid):
            return
        if not await subs.in_channel(m.bot, uid):
            await m.answer(GATE_TEXT, reply_markup=subs.channel_gate_kb())
            return
        res = await subs.activate_promo(uid, code)
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("Оформить подписку", "success", E.SUB, callback_data="sub:menu")],
                                                   [btn("В меню", "primary", E.BACK, callback_data="sub:home")]])
        await m.answer(res, reply_markup=kb)

    @r.message(F.text, _awaiting_promo)
    async def msg_promo(m: Message) -> None:
        await _promo_reply(m, (m.text or "").strip().split()[0][:32])

    @r.message(Command("promo"), F.chat.type == "private")
    async def cmd_promo(m: Message, command: CommandObject) -> None:
        await subs.load()
        if not (command.args or "").strip():
            await m.answer("Формат: <code>/promo КОД</code>")
            return
        await _promo_reply(m, command.args.strip().split()[0][:32])

    # ---------------- «Испытать удачу» (с экрана оплаты тарифа: билет дешевле тарифа, выигрыш — тариф)
    def _game_text(plan: str) -> str:
        title = PLANS[plan][0]
        ton, stars = game_ticket(plan)
        star_part = f" / {stars} {_tg_emoji(EMOJI_STAR, '⭐')}" if stars else ""
        return (f"<b>Испытать удачу: тариф {title}</b>\n\n"
                f"<b>Билет: {ton:g} {_tg_emoji(EMOJI_TON, '💎')}{star_part}</b>\n"
                "<b>Шанс 50/50</b>\n\n"
                "<b>Забьёте: получаете тариф</b>\n"
                "<b>Не забьёте: деньги сгорают</b>\n\n"
                "<b>Выберите мяч:</b>")

    def _game_plan(q: CallbackQuery, idx: int) -> str | None:
        parts = (q.data or "").split(":")
        plan = parts[idx] if len(parts) > idx else "6h"      # старые кнопки без тарифа → 6ч
        return plan if plan in PLANS else None

    @r.callback_query((F.data == "sub:game") | F.data.startswith("sub:game:"))
    async def cb_game(q: CallbackQuery) -> None:
        if not settings.game_enabled:
            await q.answer("Игра сейчас выключена", show_alert=True)
            return
        if closed():
            await q.answer(CLOSED_TEXT, show_alert=True)
            return
        plan = _game_plan(q, 2)
        if plan is None:
            await q.answer("Тариф не найден", show_alert=True)
            return
        await _safe_edit(q.message, _game_text(plan), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("⚽", "primary", callback_data=f"sub:gball:soccer:{plan}"),
             btn("🏀", "danger", callback_data=f"sub:gball:basket:{plan}")],
            [btn("Назад", "success", E.BACK, callback_data=f"sub:buy:{plan}")]]))
        await q.answer()

    @r.callback_query(F.data.startswith("sub:gball:"))
    async def cb_game_ball(q: CallbackQuery) -> None:
        parts = q.data.split(":")
        ball = parts[2] if len(parts) > 2 else ""
        plan = _game_plan(q, 3)
        if ball not in GAME_BALLS or plan is None or not settings.game_enabled:
            await q.answer("Ошибка", show_alert=True)
            return
        emoji = GAME_BALLS[ball][0]
        ton, stars = game_ticket(plan)
        rows = []
        if stars:
            rows.append([btn(f"{stars} звёзд", "primary", None, callback_data=f"sub:gpay:{ball}:stars:{plan}")])
        if settings.sub_ton_address:
            rows.append([btn(f"{ton:g} TON", "success", None, callback_data=f"sub:gpay:{ball}:ton:{plan}")])
        rows.append([btn("Назад", "danger", E.BACK, callback_data=f"sub:game:{plan}")])
        await _safe_edit(q.message, f"<b>Мяч: {emoji}</b>\n\nВыберите способ оплаты билета:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await q.answer()

    @r.callback_query(F.data.startswith("sub:gpay:"))
    async def cb_game_pay(q: CallbackQuery) -> None:
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        if closed() or not settings.game_enabled:
            await q.answer(CLOSED_TEXT, show_alert=True)
            return
        parts = q.data.split(":")
        plan = _game_plan(q, 4)
        if len(parts) < 4 or parts[2] not in GAME_BALLS or plan is None:
            await q.answer("Ошибка")
            return
        ball, method = parts[2], parts[3]
        gplan = GAME_PREFIX + plan
        ton, stars = game_ticket(plan)
        await subs.load()
        subs._remember_who(subs.user(q.from_user.id), q.from_user)
        back = [btn("Назад", "danger", E.BACK, callback_data=f"sub:gball:{ball}:{plan}")]
        if method == "stars" and stars:
            try:
                url = await subs.new_stars_invoice(q.from_user.id, gplan, ball=ball)
            except Exception as e:
                log.error("Game stars invoice failed for %s: %s", q.from_user.id, e)
                await q.answer("Не удалось создать счёт, попробуйте позже", show_alert=True)
                return
            await _safe_edit(q.message, f"<b>Билет {GAME_BALLS[ball][0]} — {stars} {_tg_emoji(EMOJI_STAR, '⭐')}</b>\n\n"
                                        "Оплатите — и бот сразу бросит мяч.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [btn("Оплатить", "success", None, url=url)], back]))
            await q.answer()
            return
        if method == "ton" and settings.sub_ton_address:
            code = await subs.new_invoice(q.from_user.id, gplan, ball=ball)
            t = _tg_emoji(EMOJI_TON, "💎")
            await _safe_edit(q.message,
                f"<b>Билет {GAME_BALLS[ball][0]} — {ton:g} {t}</b>\n\n"
                "Нажмите «Оплатить» — откроется Tonkeeper для оплаты\n\n"
                f"Вручную: отправьте <b>{ton:g} {t}</b> на\n<code>{settings.sub_ton_address}</code>\n"
                f"с комментарием <code>{code}</code>.\n\n"
                "Счёт действует 30 минут. После оплаты бот сам бросит мяч.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [btn("Оплатить", "success", None, url=subs.invoice_url(code))],
                    [btn("Проверить оплату", "primary", E.CHECK, callback_data=f"sub:check:{code}")], back]))
            await q.answer()
            return
        await q.answer("Способ оплаты недоступен", show_alert=True)

    @r.chat_member()
    async def on_member(ev: ChatMemberUpdated) -> None:
        await subs.ready.wait()
        await subs.load()
        await subs.on_join(ev)

    @r.callback_query(F.data == "sub:home")
    async def cb_home(q: CallbackQuery) -> None:
        if closed():
            await _safe_edit(q.message, CLOSED_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                btn("Моя подписка", "primary", "5893168654551355607", callback_data="sub:my")]]))
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
            subs.plans_text(q.from_user.id),
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
                # сначала пробуем бот, в котором юзер сейчас — он точно работает
                try:
                    await q.bot.send_message(uid, text)
                except Exception:
                    await subs._dm_user(uid, text)
                await q.answer()
            except Exception as e:
                log.error("New link for %s failed: %s", uid, e)
                await q.answer("Не удалось создать ссылку, попробуйте позже", show_alert=True)
            return
        await q.answer("У вас нет активной подписки.", show_alert=True)

    @r.callback_query(F.data == "sub:profile")
    async def cb_profile(q: CallbackQuery) -> None:
        if not subs.ready.is_set():
            await q.answer("Бот перезапускается, попробуйте через минуту", show_alert=True)
            return
        await subs.load()
        uid = q.from_user.id
        u = subs.state["users"].get(str(uid)) or {}
        now = time.time()
        left = u.get("until") or 0
        active = subs._has_access(u)
        taken = u.get("taken") or []
        logs = "\n".join(f"• {html.escape(str(t))}" for t in taken[-10:]) if taken else "—"
        e_prof = _tg_emoji("6032693626394382504", "👤")
        e_id   = _tg_emoji("5927118708873892465", "🆔")
        e_log  = _tg_emoji("6030400221232501136", "📋")
        e_sub  = _tg_emoji("5769126056262898415", "⭐️")
        if active:
            if left == FOREVER:
                sub_line = f"<b>{e_sub} Подписка: Активна</b>\n<i>навсегда</i>"
            else:
                secs = max(0, int(left - now))
                h, mm = secs // 3600, (secs % 3600) // 60
                sub_line = f"<b>{e_sub} Подписка: Активна</b>\n<i>{h}ч {mm}м</i>"
        else:
            sub_line = f"<b>{e_sub} Подписка: неактивна</b>"
        text = (f"<b>{e_prof} Профиль</b>\n\n"
                f"<b>{e_id} id:</b> <code>{uid}</code>\n\n"
                f"<b>{e_log} Логи:</b>\n<b>{logs}</b>\n\n"
                f"{sub_line}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("Подписка", "success", "6037175527846975726", callback_data="sub:menu")],
            [btn("Меню", "primary", "6032742198179532882", callback_data="sub:home")]])
        await _safe_edit(q.message, text, reply_markup=kb)
        await q.answer()

    @r.message(Command("my"), F.chat.type == "private")
    async def cmd_my(m: Message) -> None:
        """Кнопку «Моя подписка» убрали из меню — статус и новая ссылка доступны по /my."""
        await m.answer("<b>Моя подписка</b>\n\nНажмите кнопку — бот покажет остаток или пришлёт новую ссылку в канал.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                           btn("Моя подписка", "primary", "5893168654551355607", callback_data="sub:my"),
                           btn("В меню", "danger", E.BACK, callback_data="sub:home")]]))

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
        if subs.user(uid).get("trial") or uid in (subs.state.get("trials_used") or []):
            await q.answer("Пробный период уже использован", show_alert=True)
            return
        subs.user(uid)["trial"] = True
        subs.state.setdefault("trials_used", [])
        if uid not in subs.state["trials_used"]:
            subs.state["trials_used"].append(uid)   # durable — переживёт деплой
        subs._remember_who(subs.user(uid), q.from_user)
        try:
            text = await subs.grant(uid, TRIAL_SECONDS, "Пробные 30 мин активированы.")
        except Exception as e:
            subs.user(uid)["trial"] = False
            log.error("Trial link failed for %s: %s", uid, e)
            await q.answer("Не удалось создать ссылку. Попробуйте позже.", show_alert=True)
            return
        # сначала через бот юзера, потом фоллбек через любой
        delivered = False
        try:
            await q.bot.send_message(uid, text)
            delivered = True
        except Exception:
            delivered = await subs._dm_user(uid, text)
        if not delivered:
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
        pct = subs.promo_pct(q.from_user.id)
        price = _disc(price, pct)
        stars = max(1, round(_disc(stars, pct))) if stars else stars
        if not stars and not settings.sub_ton_address:
            await q.answer("Оплата временно недоступна", show_alert=True)
            return
        subs._remember_who(subs.user(q.from_user.id), q.from_user)
        # Шаг 1: выбор валюты. Карточку оплаты (URL) рисуем ТОЛЬКО когда способ уже выбран.
        # кастомные эмодзи в кнопках — через icon_custom_emoji_id (utils.emoji)
        rows: list[list[InlineKeyboardButton]] = []
        if stars:
            rows.append([btn(f"{stars} звёзд", "primary", None, callback_data=f"sub:pay:{plan}:stars")])
        if settings.sub_ton_address:
            rows.append([btn(f"{price:.4g} TON", "success", None, callback_data=f"sub:pay:{plan}:ton")])
        if settings.game_enabled and (settings.sub_ton_address or game_ticket(plan)[1]):
            rows.append([btn("Испытать удачу", "danger", E.CLOUD, callback_data=f"sub:game:{plan}")])
        rows.append([btn("Назад", "primary", E.BACK, callback_data="sub:menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        stars_part = f" или <b>{stars}</b> {_tg_emoji(EMOJI_STAR, '⭐')}" if stars else ""
        txt = (f"<b>Тариф {title}</b>\n"
               f"Цена: <b>{price:.4g}</b> {_tg_emoji(EMOJI_TON, '💎')}{stars_part}"
               + (f" (скидка −{pct}% по промокоду)" if pct else "") + "\n\n"
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
        pct = subs.promo_pct(q.from_user.id)
        price = round(_disc(price, pct), 4)
        stars = max(1, round(_disc(stars, pct))) if stars else stars
        subs._remember_who(subs.user(q.from_user.id), q.from_user)
        back_row = [btn("Другой способ", "danger", E.BACK, callback_data=f"sub:buy:{plan}")]

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
                [btn("Оплатить", "success", None, url=url)],
                back_row,
            ])
            await _safe_edit(q.message,
                f"<b>Тариф {title} — {stars}</b> {_tg_emoji(EMOJI_STAR, '⭐')}\n\n"
                "Нажмите кнопку и подтвердите оплату в Telegram.",
                reply_markup=kb)
            await q.answer()
            return

        if method == "ton":
            if not settings.sub_ton_address:
                await q.answer("TON-оплата временно недоступна", show_alert=True)
                return
            code = await subs.new_invoice(q.from_user.id, plan)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [btn("Оплатить", "success", None, url=subs.invoice_url(code))],
                [btn("Проверить оплату", "primary", E.CHECK, callback_data=f"sub:check:{code}")],
                back_row,
            ])
            await _safe_edit(q.message,
                f"<b>Тариф {title} — {price:g}</b> {_tg_emoji(EMOJI_TON, '💎')}\n\n"
                "Нажмите «Оплатить» — откроется Tonkeeper для оплаты\n\n"
                f"Вручную: отправьте <b>{price:g}</b> {_tg_emoji(EMOJI_TON, '💎')} на\n"
                f"<code>{settings.sub_ton_address}</code>\n"
                f"с комментарием <code>{code}</code>.\n\n"
                "Счёт действует 30 минут. После оплаты бот сам пришлёт ссылку.",
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
                    [btn(f"Оплатить {price:g} TON в Crypto Bot", "success", None, url=url)],
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
                [btn(f"Оплатить {price:g} TON в Crypto Bot", "success", None, url=CRYPTO_PAY_FALLBACK_URL)],
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
        code = (q.invoice_payload or "").split(":", 1)[0]     # payload = «code:plan»
        inv = subs.state["invoices"].get(code)
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

        # ANTI-SPAM: если юзер жмёт "проверить" чаще чем раз в 3 сек — не долбим сеть,
        # показываем текущий статус. Maker: "много раз нажимает — повторяется".
        import time as _t
        now_ts = _t.monotonic()
        last_ts = subs._check_last_ts.get(q.from_user.id, 0.0)
        skip_network = (now_ts - last_ts) < 3.0
        subs._check_last_ts[q.from_user.id] = now_ts
        if len(subs._check_last_ts) > 5000:
            subs._check_last_ts = {k: v for k, v in subs._check_last_ts.items() if now_ts - v < 60}

        # 1) Прочекать toncenter / CryptoPay прямо сейчас (не ждём фоновый цикл).
        #    Ошибку НЕ прячем — показываем юзеру, чтобы было понятно, что сети лежит.
        check_err = None
        if not inv.get("paid") and not skip_network:
            try:
                if inv.get("kind") == "crypto":
                    await subs.check_crypto_payments()
                else:
                    await subs.check_payments()
            except Exception as e:
                check_err = str(e)[:100]
                log.warning("Manual payment check failed for %s: %s", code, e)
            inv = subs.state["invoices"].get(code, {})
        elif skip_network and not inv.get("paid"):
            # быстрый повторный клик — тихий alert без спама в сеть
            await q.answer("Жду подтверждение сети, подождите немного", show_alert=False)
            return
        # 2) Если оплачено, но доступ ещё не выдан — выдаём СИНХРОННО
        if inv.get("paid") and inv.get("granted") is not True:
            try:
                await subs._deliver(inv)
            except Exception as e:
                log.warning("Manual deliver failed: %s", e)
            inv = subs.state["invoices"].get(code, {})
        # 3) Отчитаться юзеру коротким alert-ом
        if inv.get("paid"):
            if inv.get("granted") is True:
                # у игры «Испытать удачу» в чат уходит бросок мяча, а не ссылка
                msg = ("Оплата пришла — попытка в чате" if is_game(inv.get("plan"))
                       else "Оплата пришла — ссылка в чате")
                await q.answer(msg, show_alert=True)
            else:
                await q.answer("Оплата пришла, выдаю доступ…", show_alert=True)
                t = asyncio.get_running_loop().create_task(subs._retry_undelivered())
                _BG_TASKS.add(t)                     # сильная ссылка — иначе задачу может съесть GC
                t.add_done_callback(_BG_TASKS.discard)
        elif inv.get("got"):
            # пришла НЕПОЛНАЯ сумма — комментарий совпал, но меньше чем нужно
            got_ton = int(inv.get("got") or 0) / 1e9
            need_ton = int(inv.get("nano") or 0) / 1e9
            await q.answer(
                f"Пришло {got_ton:g} из {need_ton:g} TON. Доплатите остаток с тем же "
                f"комментарием {code}.", show_alert=True)
        else:
            if check_err:
                await q.answer(f"TON-сеть не ответила: {check_err}", show_alert=True)
            else:
                await q.answer("Оплата не получена, попробуйте через минуту",
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

    @r.message(Command("broadcast"), _ADM)
    async def cmd_broadcast(m: Message, command: CommandObject) -> None:
        """Рассылка всем, кто когда-либо запускал бота. HTML и кастом-эмодзи поддерживаются."""
        await subs.load()
        text = (command.args or "").strip()
        # можно ответить /broadcast на сообщение — разошлётся его текст (с форматированием)
        if not text and m.reply_to_message:
            text = m.reply_to_message.html_text or m.reply_to_message.text or ""
        if not text:
            await m.answer(
                "<b>Рассылка всем пользователям бота</b>\n\n"
                "<code>/broadcast текст</code> — отправить текст всем, кто запускал бота.\n"
                "Поддерживается HTML и кастом-эмодзи. Либо ответьте <code>/broadcast</code> на "
                "сообщение, которое нужно разослать.")
            return
        # получатели: все, кто запускал бота (кроме забаненных)
        uids = [int(k) for k, u in subs.state["users"].items()
                if str(k).lstrip("-").isdigit() and int(k) > 0
                and not (isinstance(u, dict) and u.get("banned"))]
        total = len(uids)
        if not total:
            await m.answer("Пока некому рассылать — ни один пользователь не запускал бота.")
            return
        log.warning("AUDIT [broadcast] админ=%s получателей=%s", m.from_user.id if m.from_user else "?", total)
        await m.answer(f"📣 Рассылка запущена. Получателей: <b>{total}</b>.\n\n"
                       "<b>Текст, который получат пользователи:</b>\n\n" + text)

        async def _run() -> None:
            from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
            bot = app.bot
            sent = blocked = failed = 0
            for uid in uids:
                try:
                    await bot.send_message(uid, text)
                    sent += 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
                    try:
                        await bot.send_message(uid, text)
                        sent += 1
                    except Exception:
                        failed += 1
                except TelegramForbiddenError:
                    blocked += 1           # пользователь заблокировал бота / удалил чат
                except Exception as e:
                    failed += 1
                    log.info("broadcast to %s failed: %s", uid, e)
                await asyncio.sleep(0.05)  # ~20 сообщений/с — в пределах лимитов Telegram
            try:
                await bot.send_message(
                    m.chat.id,
                    f"✅ Рассылка завершена.\n"
                    f"Доставлено: <b>{sent}</b> из {total}\n"
                    f"Заблокировали бота: {blocked}\n"
                    f"Ошибки: {failed}")
            except Exception:
                pass

        t = asyncio.get_running_loop().create_task(_run(), name="broadcast")
        _BG_TASKS.add(t)
        t.add_done_callback(_BG_TASKS.discard)

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
        log.warning("AUDIT [grant] админ=%s → uid=%s срок=%s", m.from_user.id if m.from_user else "?", uid, parts[1])
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

    @r.message(Command("offer_grant", "offer_access", "ff"), _ADM)
    async def cmd_offer_grant(m: Message, command: CommandObject) -> None:
        """Доступ к офферу (независимо от подписки): после «Занять» в личной копии лота
        у такого пользователя кнопка «Написать с оффером», у остальных — обычная.
        Формат: /ff @username  или  /ff ID."""
        await subs.load()
        arg = (command.args or "").strip()
        if not arg:
            await m.answer("Выдать доступ к офферу:\n<code>/ff @username</code> или <code>/ff ID</code>\n\n"
                           "У кого есть доступ — после «Занять» в личной копии лота кнопка «Написать с оффером». "
                           "Снять: /offer_revoke · список: /offer_list")
            return
        arg0 = arg.split()[0]
        uid = _resolve_user(arg0)
        if uid is None and re.fullmatch(r"[1-9]\d{0,15}", arg0, re.ASCII):
            uid = int(arg0)          # по числовому ID — даже если он ещё не писал боту
        if uid is None:
            await m.answer("Не нашёл пользователя. Укажите числовой ID или @username того, кто писал боту.")
            return
        log.warning("AUDIT [offer_grant] админ=%s → uid=%s", m.from_user.id if m.from_user else "?", uid)
        async with subs._lock:
            subs.user(uid)["offer_access"] = True
        await subs.save(backup=True)
        await m.answer(f"✅ Доступ к офферу выдан <code>{uid}</code>. Снять: <code>/offer_revoke {uid}</code>")

    @r.message(Command("offer_revoke"), _ADM)
    async def cmd_offer_revoke(m: Message, command: CommandObject) -> None:
        await subs.load()
        arg = (command.args or "").strip()
        uid = _resolve_user(arg.split()[0]) if arg else None
        if uid is None:
            await m.answer("Снять доступ к офферу: <code>/offer_revoke @username</code> или ID")
            return
        async with subs._lock:
            subs.user(uid).pop("offer_access", None)
        await subs.save(backup=True)
        await m.answer(f"Доступ к офферу снят с <code>{uid}</code>.")

    @r.message(Command("tarif", "tariff", "price"), _ADM)
    async def cmd_tarif(m: Message, command: CommandObject) -> None:
        """Менять цены тарифов: /tarif <код> <TON> [звёзды]. Сохраняется, переживает деплой."""
        await subs.load()
        args = (command.args or "").split()
        if not args:
            lines = ["<b>Цены тарифов</b>\n", "Изменить: <code>/tarif код TON [звёзды]</code>",
                     "Коды: " + ", ".join(f"<code>{c}</code>" for c in PLANS) + "\n"]
            for code, (title, ton, _secs, stars) in PLANS.items():
                lines.append(f"<code>{code}</code> — {title}: <b>{ton:g} TON</b>"
                             + (f" / <b>{stars} ⭐</b>" if stars else " (только TON)"))
            lines.append("\nПример: <code>/tarif 12h 2.5 300</code>")
            await m.answer("\n".join(lines))
            return
        code = args[0].strip()
        if code not in PLANS:
            await m.answer(f"Нет такого тарифа «{html.escape(code)}». Коды: "
                           + ", ".join(PLANS) + ".\nЦены и помощь: /tarif")
            return
        if len(args) < 2:
            await m.answer("Укажи цену: <code>/tarif " + code + " <TON> [звёзды]</code>")
            return
        try:
            ton = float(args[1].replace(",", "."))
            if ton <= 0:
                raise ValueError
        except ValueError:
            await m.answer("Цена TON должна быть положительным числом, например <code>2.5</code>.")
            return
        stars = None
        if len(args) >= 3 and args[2] not in ("-", "0"):
            try:
                stars = int(args[2])
                if stars <= 0:
                    stars = None
            except ValueError:
                await m.answer("Звёзды — целое число или <code>-</code> (только TON).")
                return
        elif len(args) >= 3 and args[2] in ("-", "0"):
            stars = None
        else:
            stars = PLANS[code][3]            # звёзды не указаны — оставляем прежние
        title, _ton, secs, _stars = PLANS[code]
        PLANS[code] = (title, ton, secs, stars)
        async with subs._lock:
            subs.state.setdefault("tariffs", {})[code] = [ton, stars]
        await subs.save(backup=True)
        log.warning("AUDIT [tarif] админ=%s %s → %g TON / %s звёзд",
                    m.from_user.id if m.from_user else "?", code, ton, stars)
        await m.answer(f"Тариф <b>{title}</b> обновлён: <b>{ton:g} TON</b>"
                       + (f" / <b>{stars} ⭐</b>" if stars else " (только TON)")
                       + "\n\nВсе цены: /tarif")

    @r.message(Command("sale", "skidka"), _ADM)
    async def cmd_sale(m: Message, command: CommandObject) -> None:
        """Распродажа на ВСЕ тарифы: /sale 20 [срок] — −20%. /sale off — отключить.
        Срок: 2h / 30m / 1d (или без срока — бессрочно). Авто-пост в канал."""
        await subs.load()
        args = (command.args or "").split()
        if not args:
            cur = subs.sale_pct()
            if cur:
                s = subs.state.get("sale") or {}
                until = float(s.get("until") or 0)
                tail = (f", до окончания: {_fmt_secs(max(0, until - time.time()))}" if until else ", бессрочно")
                await m.answer(f"Сейчас активна распродажа <b>−{cur}%</b> на все тарифы{tail}.\n\n"
                               "Изменить: <code>/sale 20 2h</code> · отключить: <code>/sale off</code>")
            else:
                await m.answer("Распродажи нет.\n\nВключить: <code>/sale 20 2h</code> (−20% на 2 часа) "
                               "или <code>/sale 20</code> (бессрочно). Отключить: <code>/sale off</code>")
            return
        if args[0].lower() in ("off", "стоп", "0", "-"):
            async with subs._lock:
                subs.state["sale"] = {}
            await subs.save(backup=True)
            await m.answer("Распродажа отключена. Цены вернулись к обычным.")
            return
        try:
            pct = int(args[0].rstrip("%"))
            if not (1 <= pct <= 90):
                raise ValueError
        except ValueError:
            await m.answer("Процент 1–90, например <code>/sale 20</code>.")
            return
        until = 0.0
        dur_txt = "бессрочно"
        if len(args) >= 2:
            md = re.fullmatch(r"(\d+)\s*([mhdмчд])", args[1].lower())
            if not md:
                await m.answer("Срок: <code>30m</code> / <code>2h</code> / <code>1d</code> (или без срока).")
                return
            n, unit = int(md.group(1)), md.group(2)
            mult = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}[unit]
            until = time.time() + n * mult
            dur_txt = _fmt_secs(n * mult)
        async with subs._lock:
            subs.state["sale"] = {"pct": pct, "until": until}
        await subs.save(backup=True)
        log.warning("AUDIT [sale] админ=%s −%s%% срок=%s", m.from_user.id if m.from_user else "?", pct, dur_txt)
        # авто-пост в канал
        posted = ""
        ch = settings.required_channel
        if ch:
            try:
                bot_un = await _bot_username(m.bot)
                tail = f" на {dur_txt}" if until else ""
                txt = (f"{_tg_emoji('6037175527846975726', '🔥')} <b>РАСПРОДАЖА −{pct}% на все тарифы!</b>{tail}\n\n"
                       f"Успей оформить подписку со скидкой 👇")
                kb = InlineKeyboardMarkup(inline_keyboard=[[btn("Оформить со скидкой", "success",
                        "6037175527846975726", url=f"https://t.me/{bot_un}?start=sale")]])
                await m.bot.send_message(ch, txt, reply_markup=kb)
                posted = "\nПост в канал отправлен."
            except Exception as e:
                posted = f"\nВ канал не отправилось: {e}"
        await m.answer(f"Распродажа включена: <b>−{pct}%</b> на все тарифы ({dur_txt}). "
                       f"Скидка видна в ценах, кнопках и счёте.{posted}")

    @r.message(Command("partner"), _ADM)
    async def cmd_partner(m: Message, command: CommandObject) -> None:
        """Партнёрская ссылка: даёшь человеку, он кидает её в свою группу, покупки пришедших
        по ней засчитываются ему (кол-во оплат + выручка). Работает поверх реф-системы."""
        await subs.load()
        raw = (command.args or "").strip()
        if not raw:
            await m.answer("Партнёрская ссылка:\n<code>/partner @username</code> или "
                           "<code>/partner ID [имя] [%]</code>\n\nСоздаёт реф-ссылку для партнёра — он кидает её "
                           f"в свою группу, покупки пришедших засчитываются ему. Доля по умолчанию "
                           f"{settings.partner_percent}%, можно задать свою: <code>/partner 123 Вася 40%</code>.\n"
                           "Список и выручка: /partners")
            return
        # вытащим необязательный процент (последний токен вида «40%» или «40проц»)
        pct_override = None
        toks = raw.split()
        mpct = re.fullmatch(r"(\d{1,3})%?", toks[-1]) if toks else None
        if len(toks) >= 2 and mpct and toks[-1].endswith("%"):
            pct_override = max(0, min(100, int(mpct.group(1))))
            toks = toks[:-1]
        parts = (" ".join(toks)).split(maxsplit=1)
        arg0 = parts[0].strip()
        uid = _resolve_user(arg0)
        if uid is None and re.fullmatch(r"[1-9]\d{0,15}", arg0, re.ASCII):
            uid = int(arg0)               # по числовому ID заведём запись, даже если он ещё не писал боту
        if uid is None:
            await m.answer("Не нашёл пользователя. Укажите числовой ID (партнёру не обязательно быть в боте) "
                           "или @username того, кто уже писал боту.")
            return
        log.warning("AUDIT [partner] админ=%s → uid=%s pct=%s", m.from_user.id if m.from_user else "?", uid, pct_override)
        async with subs._lock:
            u = subs.user(uid)
            u["partner"] = True
            if len(parts) > 1:
                u["partner_name"] = parts[1].strip()
            if pct_override is not None:
                u["partner_pct"] = pct_override
        await subs.save(backup=True)
        try:
            link = f"https://t.me/{await _bot_username(m.bot)}?start=ref_{uid}"
        except Exception:
            link = f"?start=ref_{uid}"
        info = subs.ref_info(uid)
        rev = int(subs.user(uid).get("ref_revenue_nano") or 0) / 1e9
        pct = int(subs.user(uid).get("partner_pct") or settings.partner_percent)
        payout = rev * pct / 100
        name = html.escape(parts[1].strip()) if len(parts) > 1 else str(uid)
        await m.answer(f"<b>Партнёр: {name}</b> · доля <b>{pct}%</b>\n\n"
                       f"Ссылка (дайте партнёру, он кидает её в свою группу):\n<code>{link}</code>\n\n"
                       f"Пришло по ссылке: <b>{info['count']}</b>\n"
                       f"Оплатили: <b>{info['paid']}</b>\n"
                       f"Выручка от его людей: <b>{rev:.2f} TON</b>\n"
                       f"К выплате партнёру ({pct}%): <b>{payout:.2f} TON</b>\n\n"
                       f"Все партнёры и выплаты: /partners")

    @r.message(Command("partners"), _ADM)
    async def cmd_partners(m: Message) -> None:
        await subs.load()
        rows = [(k, u) for k, u in subs.state["users"].items()
                if isinstance(u, dict) and (u.get("partner") or int(u.get("ref_count") or 0) > 0)]
        rows.sort(key=lambda kv: (-int(kv[1].get("ref_revenue_nano") or 0), -int(kv[1].get("ref_paid") or 0)))
        if not rows:
            await m.answer("Партнёров и рефереров пока нет. Создать: <code>/partner @username</code>")
            return
        total_payout = 0.0
        lines = [f"<b>Партнёры / рефереры ({len(rows)}):</b>", ""]
        for k, u in rows[:50]:
            who = u.get("partner_name") or (("@" + u["username"]) if u.get("username") else k)
            rev = int(u.get("ref_revenue_nano") or 0) / 1e9
            pct = int(u.get("partner_pct") or settings.partner_percent)
            payout = rev * pct / 100
            total_payout += payout
            mark = "⭐ " if u.get("partner") else ""
            pay_part = f", выплата {payout:.2f} TON ({pct}%)" if u.get("partner") else ""
            lines.append(f"{mark}<code>{k}</code> {html.escape(str(who))} — пришло "
                         f"{int(u.get('ref_count') or 0)}, оплат {int(u.get('ref_paid') or 0)}, "
                         f"{rev:.2f} TON{pay_part}")
        lines.append("")
        lines.append(f"<b>Итого к выплате партнёрам: {total_payout:.2f} TON</b>")
        await m.answer("\n".join(lines))

    @r.message(Command("offer_list"), _ADM)
    async def cmd_offer_list(m: Message) -> None:
        await subs.load()
        rows = [(k, u) for k, u in subs.state["users"].items()
                if isinstance(u, dict) and u.get("offer_access")]
        if not rows:
            await m.answer("Доступ к офферу пока ни у кого. Выдать: <code>/offer_grant @username</code>")
            return
        lines = [f"<b>Доступ к офферу ({len(rows)}):</b>"]
        for k, u in rows[:100]:
            who = ("@" + u["username"]) if u.get("username") else (u.get("name") or k)
            lines.append(f"<code>{k}</code> {html.escape(str(who))}")
        await m.answer("\n".join(lines))

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

    # ---------------- бан
    @r.message(Command("ban"), _ADM)
    async def cmd_ban(m: Message, command: CommandObject) -> None:
        await subs.load()
        arg = (command.args or "").strip().split()
        uid = _resolve_user(arg[0]) if arg else None
        if uid is None:
            await m.answer("Формат: <code>/ban ID</code> или <code>/ban @username</code>\n"
                           "(юз — только если человек уже писал боту)")
            return
        if uid in settings.admin_ids:
            await m.answer("Админа забанить нельзя.")
            return
        async with subs._lock:
            await subs._revoke_links(uid)
            u = subs.user(uid)
            u.update({"banned": True, "until": 0, "pending": 0, "in_chat": False})
            await subs.save(backup=True)
        await subs.flush_backup()
        kicked = True
        try:
            await subs.bot.ban_chat_member(sub_chat_id(), uid)       # без unban — в ЧС канала
        except Exception as e:
            es = str(e).lower()
            kicked = "participant" in es or "not found" in es
            if not kicked:
                log.warning("ban %s in channel failed: %s", uid, e)
        who = f"@{u['username']}" if u.get("username") else ""
        await m.answer(f"Забанен <code>{uid}</code> {html.escape(who)}: подписка снята, бот ему недоступен"
                       + ("" if kicked else ".\nИз канала удалить не вышло — проверьте права бота.")
                       + "\nСнять бан: <code>/unban " + str(uid) + "</code>")

    @r.message(Command("unban"), _ADM)
    async def cmd_unban(m: Message, command: CommandObject) -> None:
        await subs.load()
        arg = (command.args or "").strip().split()
        uid = _resolve_user(arg[0]) if arg else None
        if uid is None:
            await m.answer("Формат: <code>/unban ID</code> или <code>/unban @username</code>")
            return
        async with subs._lock:
            subs.user(uid).pop("banned", None)
            await subs.save(backup=True)
        try:
            await subs.bot.unban_chat_member(sub_chat_id(), uid, only_if_banned=True)
        except Exception as e:
            log.info("unban %s in channel: %s", uid, e)
        await m.answer(f"Бан снят: <code>{uid}</code>")

    # ---------------- промокоды (админ)
    @r.message(Command("promo_add"), _ADM)
    async def cmd_promo_add(m: Message, command: CommandObject) -> None:
        await subs.load()
        parts = (command.args or "").split()
        usage = ("Создать промокод: <code>/promo_add КОД [скидка%] [лимит]</code>\n"
                 "Пример: <code>/promo_add GIFT30 30 100</code> — −30%, 100 активаций (без лимита — не указывать)")
        if not parts or not re.fullmatch(r"[A-Za-z0-9_\-]{3,32}", parts[0]):
            await m.answer(usage)
            return
        try:
            pct = int(parts[1].rstrip("%")) if len(parts) > 1 else 30
            lim = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            await m.answer(usage)
            return
        if not 1 <= pct <= 95 or lim < 0:
            await m.answer("Скидка 1–95%, лимит ≥ 0.\n\n" + usage)
            return
        code = parts[0].upper()
        async with subs._lock:
            subs.state.setdefault("promos", {})[code] = {"pct": pct, "max": lim or None, "uses": 0,
                                                         "created": time.time(), "activated_by": []}
            await subs.save(backup=True)
        await m.answer(f"Промокод <code>{code}</code>: −{pct}%" + (f", {lim} активаций" if lim else ", без лимита")
                       + "\nПользователь вводит его кнопкой «Промокод» или <code>/promo " + code + "</code>."
                       + "\n\nКто вводил: <code>/promo_users " + code + "</code>")
        # авто-пост в канал с кнопкой «Активировать» (ведёт в бота прямо к промокоду)
        ch = settings.required_channel
        if ch:
            try:
                bot_un = await _bot_username(m.bot)
                lim_part = f", {lim} активаций" if lim else ""
                text = (f"{_tg_emoji('6037175527846975726', '🎁')} Лови скидку по промокоду <code>{code}</code>: <b>−{pct}%</b>{lim_part}\n\n"
                        "Успей — свежие лоты ждут вас!\n\n"
                        f"Активируйте в: @{bot_un}")
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    btn("Активировать", "success", url=f"https://t.me/{bot_un}?start=promo_{code}")]])
                await m.bot.send_message(ch, text, reply_markup=kb)
                await m.answer(f"Опубликовал промокод в канал {ch}.")
            except Exception as e:
                await m.answer(f"Создал, но в канал не запостил: {html.escape(str(e))}\n"
                               f"Проверь, что бот — админ в {ch}.")

    @r.message(Command("promo_del"), _ADM)
    async def cmd_promo_del(m: Message, command: CommandObject) -> None:
        await subs.load()
        code = (command.args or "").strip().upper()
        async with subs._lock:
            p = (subs.state.get("promos") or {}).get(code)
            if p is not None:
                p["off"] = True
                await subs.save(backup=True)
        await m.answer(f"Промокод <code>{html.escape(code)}</code> выключен." if p is not None
                       else "Нет такого промокода. Список: /promos")

    @r.message(Command("promos"), _ADM)
    async def cmd_promos(m: Message) -> None:
        await subs.load()
        rows = [f"<code>{c}</code> −{p.get('pct')}% · ввели {len(p.get('activated_by') or [])}"
                + (f"/{p['max']}" if p.get("max") else "") + f" · оплат {p.get('uses', 0)}"
                + (" · выкл" if p.get("off") else "")
                for c, p in (subs.state.get("promos") or {}).items()]
        await m.answer("<b>Промокоды</b>\n" + ("\n".join(rows) or "нет") +
                       "\n\nКто вводил код: <code>/promo_users КОД</code>\n"
                       "Создать: <code>/promo_add КОД 30 [лимит]</code> · выключить: <code>/promo_del КОД</code>")

    @r.message(Command("promo_users"), _ADM)
    async def cmd_promo_users(m: Message, command: CommandObject) -> None:
        await subs.load()
        code = (command.args or "").strip().upper()
        p = (subs.state.get("promos") or {}).get(code)
        if not p:
            await m.answer("Нет такого промокода. Список: /promos")
            return
        ids = p.get("activated_by") or []
        if not ids:
            await m.answer(f"Промокод <code>{code}</code> пока никто не вводил.")
            return
        lines = [f"<b>Вводили промокод {code} ({len(ids)}):</b>", ""]
        for uid in ids[-100:]:
            u = subs.state["users"].get(str(uid)) or {}
            who = ("@" + u["username"]) if u.get("username") else (u.get("name") or "")
            paid = " ✅оплатил" if code in [x for x in (u.get("promos_used") or [])] else ""
            lines.append(f"<code>{uid}</code> {html.escape(str(who))}{paid}")
        await m.answer("\n".join(lines))

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
