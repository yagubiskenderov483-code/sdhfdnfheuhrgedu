"""Рефералы, промокоды, игра «испытать удачу», бан, обязательная подписка."""
import asyncio, sys, time, types
sys.path.insert(0, ".")
from config import settings
settings.admin_ids = {1}
settings.target_chat_id = -100
settings.sub_ton_address = "UQtest"
settings.required_channel = "@trackgiftsx"
import bot.subscription as S

class DB:
    def __init__(s): s.kv = {}
    async def kv_get(s, k, d=None): return s.kv.get(k, d)
    async def kv_set(s, k, v): s.kv[k] = v

class Bot:
    id = 1
    def __init__(s, dice=3, chan=True):
        s.sent = []; s.members = set(); s.dice = dice; s.chan = chan; s.banned = []
    async def send_message(s, uid, text, **kw):
        s.sent.append((uid, text)); return types.SimpleNamespace(message_id=len(s.sent))
    async def send_dice(s, uid, emoji):
        s.sent.append((uid, "DICE" + emoji)); return types.SimpleNamespace(dice=types.SimpleNamespace(value=s.dice))
    async def ban_chat_member(s, chat, uid, **k): s.banned.append(uid)
    async def unban_chat_member(s, *a, **k): pass
    async def get_chat_member(s, chat, uid):
        if chat == "@trackgiftsx":
            if s.chan is None:
                raise RuntimeError("bot is not admin")
            return types.SimpleNamespace(status="member" if s.chan else "left")
        return types.SimpleNamespace(status="member" if uid in s.members else "left")
    async def create_chat_invite_link(s, *a, **k): return types.SimpleNamespace(invite_link="https://t.me/+X")
    async def revoke_chat_invite_link(s, *a): pass
    async def set_message_reaction(s, **k): pass

S.asyncio.sleep = (lambda orig: (lambda t: orig(0)))(asyncio.sleep)

def mk(**kw):
    app = types.SimpleNamespace(db=DB(), bot=Bot(**kw))
    subs = S.Subscriptions(app); subs.ready.set()
    return app, subs

async def main():
    # --- реферал + промокод на оплате TON
    app, subs = mk()
    await subs.load()
    subs.user(100)                                         # пригласивший уже есть в боте
    assert await subs.register_ref(200, 100)
    assert not await subs.register_ref(200, 100), "повторно не засчитывается"
    assert not await subs.register_ref(300, 300), "сам себя — нет"
    assert subs.ref_info(100)["count"] == 1
    subs.state["promos"] = {"GIFT30": {"pct": 30, "max": 1, "uses": 0}}
    assert "активирован" in await subs.activate_promo(200, "gift30")
    code = await subs.new_invoice(200, "6h")
    inv = subs.state["invoices"][code]
    assert inv["nano"] == int(round(S.PLANS["6h"][1] * 0.7 * 1e9)), inv
    async def fake(): return [{"transaction_id": {"hash": "h1"},
                               "in_msg": {"source": "EQx", "value": str(inv["nano"]), "message": code}}]
    subs._fetch_txs = fake
    assert await subs.check_payments() == 1
    assert subs.state["promos"]["GIFT30"]["uses"] == 1 and not subs.user(200).get("promo")
    assert subs.ref_info(100)["bonus"] == settings.ref_bonus_minutes * 60 and subs.ref_info(100)["paid"] == 1
    assert "недействителен" in await subs.activate_promo(300, "GIFT30"), "лимит активаций"
    print("OK реферал: +30 мин за оплату; промокод −30% в сумме счёта и гасится после оплаты")
    # активация бонуса
    settings.ref_min_minutes = 30
    assert await subs.activate_ref_bonus(100)
    assert subs.ref_info(100)["bonus"] == 0
    print("OK реф-бонус активируется и обнуляется")

    # --- игра: гол / мимо, повтор доставки не перебрасывает мяч
    for dice, ball, win in ((3, "soccer", True), (2, "soccer", False), (3, "basket", False), (5, "basket", True)):
        app, subs = mk(dice=dice)
        await subs.load()
        code = await subs.new_invoice(7, S.GAME_PLAN, ball=ball)
        inv = subs.state["invoices"][code]
        assert inv["nano"] == int(round(settings.game_ton * 1e9))
        inv.update(paid=True, granted="inflight", paid_at=time.time())
        assert await subs._deliver(inv)
        assert inv["win"] is win and inv["granted"] is True, inv
        has_time = subs._has_access(subs.user(7)) or subs.user(7).get("pending")
        assert bool(has_time) is win
        app.bot.dice = 1                                   # повторная доставка — тот же результат
        inv["granted"] = False
        await subs._deliver(inv)
        assert inv["dice"] == dice and sum(1 for _, t in app.bot.sent if t.startswith("DICE")) == 1
    print("OK игра: ⚽ 3-5 гол, 🏀 4-5 попал; бросок один раз, выигрыш = время подписки")

    # --- бан и канал
    app, subs = mk(chan=False)
    await subs.load()
    assert not await subs.in_channel(app.bot, 55)
    app.bot.chan = True
    assert await subs.in_channel(app.bot, 55)
    app.bot.chan = None                                     # бот не админ канала — людей не блокируем
    subs._chan_ok.clear()
    assert await subs.in_channel(app.bot, 56)
    subs.user(57)["banned"] = True
    assert subs.is_banned(57)
    print("OK обязательная подписка и бан")

asyncio.run(main())
