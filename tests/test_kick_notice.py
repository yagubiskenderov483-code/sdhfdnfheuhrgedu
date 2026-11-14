"""Кик по окончании срока → ЛС «срок окончен» с кнопкой «Продлить»; Stars — повторная/чужая оплата."""
import asyncio, sys, time, types
sys.path.insert(0, ".")
from config import settings
settings.admin_ids = {1}
settings.target_chat_id = -100
settings.sub_ton_address = "UQtest"
import bot.subscription as S

class DB:
    def __init__(s): s.kv = {}
    async def kv_get(s, k, d=None): return s.kv.get(k, d)
    async def kv_set(s, k, v): s.kv[k] = v

class Bot:
    def __init__(s): s.sent = []; s.members = set()
    async def send_message(s, uid, text, **kw): s.sent.append((uid, text, kw.get("reply_markup")))
    async def ban_chat_member(s, *a, **k): pass
    async def unban_chat_member(s, *a, **k): pass
    async def get_chat_member(s, chat, uid):
        return types.SimpleNamespace(status="member" if uid in s.members else "left")
    async def create_chat_invite_link(s, *a, **k): return types.SimpleNamespace(invite_link="https://t.me/+X")
    async def revoke_chat_invite_link(s, *a): pass
    async def set_message_reaction(s, **k): pass

S.asyncio.sleep = (lambda orig: (lambda t: orig(0)))(asyncio.sleep)

async def main():
    app = types.SimpleNamespace(db=DB(), bot=Bot())
    subs = S.Subscriptions(app); subs.ready.set(); await subs.load()
    u = subs.user(42); u.update(until=time.time() - 5, in_chat=True, started=time.time() - 7200)
    await subs._expire()
    msgs = [m for m in app.bot.sent if m[0] == 42]
    assert len(msgs) == 1 and "Срок вашей подписки окончен" in msgs[0][1], msgs
    kb = msgs[0][2]
    assert kb.inline_keyboard[0][0].callback_data == "sub:menu" and "Продлить" in kb.inline_keyboard[0][0].text
    print("OK кик → ЛС «срок окончен» + кнопка «Продлить подписку»")
    # reconcile после кика (человек всё ещё в канале по данным Telegram) — повторно не пишем
    app.bot.members.add(42); u["checked"] = 0
    await subs._reconcile()
    assert len([m for m in app.bot.sent if m[0] == 42]) == 1
    print("OK повторного «срок окончен» нет")
    # пробный
    t = subs.user(43); t.update(trial=True, started=time.time() - S.TRIAL_SECONDS, until=time.time() - 1, in_chat=True)
    await subs._expire()
    assert any(m[0] == 43 and "Пробный период закончился" in m[1] for m in app.bot.sent)
    print("OK пробный → отдельный текст")
    # Stars: первая оплата, повтор того же charge_id, вторая оплата по тому же счёту
    subs.state["invoices"]["STR1"] = {"code": "STR1", "uid": 50, "plan": "6h", "kind": "stars",
                                      "stars": 150, "ts": time.time(), "paid": False}
    await subs.on_stars_paid("STR1", 50, 150, "c1")
    assert subs.state["invoices"]["STR1"]["granted"] is True
    n = len(app.bot.sent)
    await subs.on_stars_paid("STR1", 50, 150, "c1")
    assert len(app.bot.sent) == n, "дубль апдейта не должен ничего слать"
    await subs.on_stars_paid("STR1", 50, 150, "c2")
    assert any(m[0] == 1 and "не найден или уже оплачен" in m[1] for m in app.bot.sent)
    assert subs.state["revenue_stars"] == 150
    print("OK Stars: дубль игнорируется, вторая оплата → админу, выручка не задвоена")
    # TON-проверка не падает на Stars/Crypto-счетах без nano
    subs.state["invoices"]["CRB1"] = {"code": "CRB1", "uid": 60, "plan": "6h", "kind": "crypto",
                                      "ts": time.time(), "paid": False}
    async def fake(): return []
    subs._fetch_txs = fake
    assert await subs.check_payments() == 0
    print("OK check_payments не падает на не-TON счетах")

asyncio.run(main())


def test_soft_zone():
    """Мягкие фильтры — все темы, кроме 300-1000 (по тому же значению, что и маршрут)."""
    import dataclasses, tempfile
    from pathlib import Path
    from database.models import Listing
    from filters.seller_filter import SellerFilter
    from tracker.queue import ListingPipeline
    settings.soft_filter_stars = 1000
    settings.route_by = "floor"
    settings.backdrop_routes = [("Black", -1, 6)]
    settings.russian_only, settings.unknown_seller_policy = True, "block"
    settings.require_free_messages, settings.max_seller_nfts, settings.max_seller_level = True, 12, 4
    base = {x.name: None for x in dataclasses.fields(Listing)
            if x.default is dataclasses.MISSING and x.default_factory is dataclasses.MISSING}
    def lot(**kw):
        l = Listing(**base)
        for k, v in kw.items():
            setattr(l, k, v)
        return l
    soft = ListingPipeline._soft
    assert not soft(lot(stars_price=1500, floor_stars=500)), "флор 500 → тема 300-1000 → строго"
    assert soft(lot(stars_price=900, floor_stars=1500)), "флор 1500 → тема 1000-3000 → мягко"
    assert soft(lot(stars_price=500, floor_stars=400, backdrop="Black")), "тема Black → мягко"
    f = SellerFilter(Path(tempfile.mkdtemp()))
    foreign = lot(seller_type="user", seller_id=5, messages="бесплатно", seller_about="nft seller",
                  seller_nfts=50, level=9)
    assert not f.check(foreign).passed and f.check(foreign, soft=True).passed
    paid = lot(seller_type="user", seller_id=5, messages="платные (100 ⭐)")
    assert f.check(paid, soft=True).code == "PAID_MESSAGES"
    arab = lot(seller_type="user", seller_id=5, messages="бесплатно", seller_name="محمد")
    assert f.check(arab, soft=True).code == "ARABIC_SCRIPT"
    short = lot(seller_name="ab")
    settings.allow_hidden_owner = True
    settings.hidden_name_strict = False
    # мягкий дефолт (dj): короткое латинское имя больше НЕ режем
    assert f.check(short).code == "OWNER_HIDDEN" and f.check(short, soft=True).passed
    # строгий режим — старое SHORT_NAME_HIDDEN
    settings.hidden_name_strict = True
    assert f.check(short).code == "SHORT_NAME_HIDDEN"
    settings.hidden_name_strict = False
    print("OK мягкая зона: всё кроме 300-1000; платные ЛС, арабы и ЧС режутся всегда")


test_soft_zone()
