"""Офлайн-тест автопоиска скрытого владельца (tracker/lookup.py) — без Telegram.
Запуск:  PYTHONPATH=. python tests/test_lookup.py"""
import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.getcwd())

from telethon import types
from telethon.tl.functions.payments import GetSavedStarGiftsRequest

from config import settings
from database.models import Listing
import tracker.lookup as L

ok_count = 0


def check(cond, what):
    global ok_count
    if not cond:
        print("FAIL:", what)
        sys.exit(1)
    ok_count += 1
    print("OK", what)


NOW = datetime.datetime.now(datetime.timezone.utc)


def bot_msg(mid, text, entities=None, markup=None, out=False):
    return types.Message(id=mid, peer_id=types.PeerUser(777), date=NOW, message=text,
                         entities=entities, reply_markup=markup, out=out)


def user(uid, first, username=None, last=None):
    return types.User(id=uid, first_name=first, last_name=last, username=username, access_hash=1)


def unique(slug, gid):
    return types.SavedStarGift(date=NOW, gift=types.StarGiftUnique(
        id=gid, gift_id=1, title="Plush Pepe", slug=slug, num=1, attributes=[], availability_issued=1,
        availability_total=1))


class FakeClient:
    """Имитация бота поиска: на «/human Иван» отвечает списком из трёх аккаунтов."""

    def __init__(self, reply_text=None, gifts=None, users=None, answer=True):
        self.sent = []
        self.answer = answer
        self.reply_text = reply_text
        self.gifts = gifts or {}
        self.users = users or {}

    async def get_input_entity(self, x):
        return x

    async def get_messages(self, ent, limit=10, min_id=0):
        if min_id == 0:
            return []                                    # переписки с ботом ещё не было
        if not self.answer:
            return []
        text = self.reply_text
        # «@ivan_real» как упоминание, второй — ссылкой на профиль по ID, третий — кнопкой t.me
        i1 = text.index("Иван Иванов")
        ents = [types.MessageEntityMentionName(offset=i1, length=len("Иван Иванов"), user_id=222)]
        markup = types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[
            types.KeyboardInlineButton(text="3", type=types.InlineButtonTypeUrl(url="https://t.me/ivan_third"))])])
        return [bot_msg(min_id + 1, text, ents, markup)]

    async def send_message(self, ent, text):
        self.sent.append(text)
        return bot_msg(100 + len(self.sent), text, out=True)

    async def get_entity(self, x):
        key = x.user_id if isinstance(x, types.PeerUser) else str(x).lower()
        if key not in self.users:
            raise ValueError("no user")
        return self.users[key]

    async def __call__(self, req):
        assert isinstance(req, GetSavedStarGiftsRequest)
        return types.payments.SavedStarGifts(count=len(self.gifts.get(req.peer.id, [])),
                                             gifts=self.gifts.get(req.peer.id, []), chats=[], users=[])


class FakeWorker:
    def __init__(self, name):
        self.name = name

    def is_usable(self):
        return True


class FakeSessions:
    def __init__(self, client):
        self.client = client
        self.workers = {"session_001": FakeWorker("session_001")}

    def get(self, name):
        return self.workers.get(name)

    def usable(self, include_reserved=False):
        return list(self.workers.values())

    async def run(self, fn, only=None, attempts=1, timeout=None, **kw):
        return await fn(self.client), only


REPLY = "Найдено 3:\n1. @ivan_real — Иван\n2. Иван Иванов ID: 222\n3. ещё один\nКанал: t.me/funstat_news"


async def main():
    settings.lookup_bot_username = "funstat"
    settings.auto_lookup = True
    settings.lookup_sessions = []
    settings.lookup_timeout = 8
    settings.lookup_interval = 0

    # --- парсер кандидатов
    c = FakeClient(REPLY)
    msgs = await c.get_messages(None, min_id=5)
    keys = [x.key() for x in L.parse_candidates(msgs, "funstat")]
    check("id222" in keys, "упоминание по ID → кандидат")
    check("@ivan_real" in keys, "@username в тексте → кандидат")
    check("@ivan_third" in keys, "URL-кнопка t.me → кандидат")
    check("@funstat" not in keys, "сам бот поиска не кандидат")

    # реальный формат ответа бота поиска: «01. 💬 Имя (@username)», имя — ссылка
    real = ("Пользователи с указанным именем 'callmeluv'\n💬 - в базе есть сообщения/статистика\n\n"
            "01. 💬 całlmełuv (@callmeluv69)\n02. 💬 Петя (id7105095)")
    i = real.index("@callmeluv69")
    ents = [types.MessageEntityTextUrl(offset=real.index("całlmełuv"), length=9,
                                       url="https://t.me/infostat_robot?start=u123"),
            types.MessageEntityMention(offset=i, length=len("@callmeluv69"))]
    rk = [x.key() for x in L.parse_candidates([bot_msg(9, real, ents)], "infostat_robot")]
    check(rk[:1] == ["@callmeluv69"] and "id7105095" in rk, "формат «01. Имя (@username)» → @callmeluv69")
    check(not any("infostat" in k for k in rk), "ссылка на сам бот поиска не кандидат")

    # --- владелец: у @ivan_real NFT нет, у u222 (имя совпадает) NFT открыт
    users = {"ivan_real": user(111, "Иван", "ivan_real"), 222: user(222, "Иван", "ivan_222", "Иванов"),
             "ivan_third": user(333, "Иван", "ivan_third")}
    gifts = {111: [unique("OtherGift-1", 9)], 222: [unique("PlushPepe-77", 5555)], 333: []}
    c = FakeClient(REPLY, gifts, users)
    lk = L.OwnerLookup(FakeSessions(c))
    found = await lk.find_owner("Иван Иванов", "PlushPepe-77", 5555)
    check(c.sent == ["/start", "/human Иван Иванов"], "бот запущен и отправлена команда /human")
    check(found is not None and found.user_id == 222 and found.username == "ivan_222",
          "найден тот, у кого открыт ИМЕННО этот NFT")

    # повтор по тому же имени — из кэша, боту второй раз не пишем
    found2 = await lk.find_owner("Иван Иванов", "PlushPepe-77", 5555)
    check(len(c.sent) == 2 and found2 and found2.user_id == 222, "повтор имени — из кэша")

    # NFT ни у кого не открыт → владелец не найден
    c = FakeClient(REPLY, {111: [], 222: [], 333: []}, users)
    lk = L.OwnerLookup(FakeSessions(c))
    check(await lk.find_owner("Иван Иванов", "PlushPepe-77", 5555) is None, "NFT скрыт у всех → не найден")

    # бот молчит → None, без зависания
    c = FakeClient(REPLY, gifts, users, answer=False)
    lk = L.OwnerLookup(FakeSessions(c))
    settings.lookup_timeout = 5
    check(await lk.find_owner("Иван Иванов", "PlushPepe-77", 5555) is None, "бот не ответил → None")

    # капча: бот поиска сначала просит капчу → админ нажимает → запрос повторяется → владелец найден
    class CaptchaClient(FakeClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.solved = False
            self.clicked = None

        async def get_messages(self, ent, limit=10, min_id=0):
            if min_id and not self.solved:
                kb = types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[
                    types.KeyboardInlineButton(text=e, type=types.InlineButtonTypeCallback(data=d))
                    for e, d in (("🦊", b"a"), ("🐰", b"b"), ("🐶", b"c"))])])
                return [bot_msg(min_id + 1, "⚠️ Captcha.\nClick the button with the icon that looks like the picture.",
                                None, kb)]
            return await super().get_messages(ent, limit, min_id)

        async def download_media(self, msg, file=None):
            return b"img"

        async def __call__(self, req):
            from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
            if isinstance(req, GetBotCallbackAnswerRequest):
                self.clicked = req.data
                self.solved = True
                return None
            return await super().__call__(req)

    cc = CaptchaClient(REPLY, gifts, users)
    lk = L.OwnerLookup(FakeSessions(cc))
    got = {}

    async def on_cap(token, session, photo, buttons):
        got.update(photo=photo, buttons=buttons)
        asyncio.get_running_loop().call_later(0.1, lambda: asyncio.ensure_future(lk.solve_captcha(token, 0)))
    lk.on_captcha = on_cap
    f = await lk.find_owner("Иван Иванов", "PlushPepe-77", 5555)
    check(got.get("buttons") == ["🦊", "🐰", "🐶"] and got.get("photo") == b"img", "капча пришла админу с кнопками")
    check(cc.clicked == b"a", "нажатие админа передано боту поиска")
    check(f is not None and f.user_id == 222, "после капчи поиск продолжился и нашёл владельца")
    check(cc.sent.count("/human Иван Иванов") == 2, "команда повторена после капчи")

    # несколько выделенных аккаунтов: первый не смог написать боту → пишет второй
    class TwoSessions(FakeSessions):
        def __init__(self, client):
            super().__init__(client)
            self.workers = {"s1": FakeWorker("s1"), "s2": FakeWorker("s2")}
            self.used = []

        async def run(self, fn, only=None, **kw):
            self.used.append(only)
            if only == "s1":
                raise RuntimeError("limit")
            return await fn(self.client), only

    settings.lookup_sessions = ["s1", "s2"]
    settings.lookup_timeout = 8
    ts = TwoSessions(FakeClient(REPLY, gifts, users))
    f = await L.OwnerLookup(ts).find_owner("Иван Иванов", "PlushPepe-77", 5555)
    check(f is not None and f.user_id == 222 and "s2" in ts.used, "аккаунт s1 упал → поиск через s2")
    settings.lookup_sessions = []

    # отдельные аккаунты поиска не берут опросы маркета
    from sessions.manager import SessionManager
    sm = SessionManager.__new__(SessionManager)
    sm.workers = {n: FakeWorker(n) for n in ("session_001", "session_002", "session_003")}
    settings.lookup_sessions, settings.lookup_dedicated = ["session_003"], True
    names = [w.name for w in sm.usable()]
    check(names == ["session_001", "session_002"], "выделенный аккаунт не опрашивает маркет")
    check(len(sm.usable(include_reserved=True)) == 3, "но поиск его видит")
    settings.lookup_dedicated = False
    check(len(sm.usable()) == 3, "LOOKUP_DEDICATED=false — аккаунт работает и на маркет")
    settings.lookup_dedicated = True
    settings.lookup_sessions = ["session_001", "session_002", "session_003"]
    check(len(sm.usable()) == 3, "выделены все аккаунты — резерв не действует, трекер не встаёт")
    settings.lookup_sessions = []

    # выключено → ничего не делаем
    settings.auto_lookup = False
    c = FakeClient(REPLY, gifts, users)
    lk = L.OwnerLookup(FakeSessions(c))
    check(await lk.find_owner("Иван Иванов", "PlushPepe-77", 5555) is None and not c.sent,
          "AUTO_LOOKUP=false → боту не пишем")
    settings.auto_lookup = True

    # --- конвейер: скрытый владелец ищется ДО отправки, лот уходит уже с @username
    from tracker.queue import ListingPipeline
    from bot.formatter import MessageFormatter, listing_keyboard
    from database.models import ListingStatus

    class FakeDB:
        async def update_listing(self, l):
            self.l = l

    def pipeline(client):
        p = ListingPipeline.__new__(ListingPipeline)
        p.owner_lookup = L.OwnerLookup(FakeSessions(client))
        p.db = FakeDB()
        p.stats = {"filtered_seller": 0}
        p.paused_getter = p.muted_getter = None
        p.price_filter = type("PF", (), {"check": lambda self, l: type("R", (), {"passed": True})()})()
        from filters.seller_filter import SellerFilter
        import tempfile, pathlib
        p.seller_filter = SellerFilter(pathlib.Path(tempfile.mkdtemp()))
        p.sent_l = None
        p._lookup_tasks = set()

        async def resolve(l):
            return None
        p._resolve_hidden_owner = resolve

        async def sent_before(l):
            return None
        p._sent_before = sent_before

        async def check(l):
            from filters.seller_filter import SellerResult
            return SellerResult(True, "OK", "ok")
        p._check_seller = check
        p._seller_locks = {}

        async def send_stage(ev, l, base, t0):
            p.sent_l = l
        p._send_stage = send_stage
        return p

    def hidden():
        return Listing(listing_id="5555:n:Иван Иванов", gift_unique_id=5555, gift_id=1, gift_name="Plush Pepe",
                       gift_slug="PlushPepe-77", gift_link="https://t.me/nft/PlushPepe-77",
                       seller_key="n:Иван Иванов", seller_name="Иван Иванов", stars_price=10000)

    from tracker.queue import ListingEvent
    import time as _t
    settings.lookup_required = True
    p = pipeline(FakeClient(REPLY, gifts, users))
    l = hidden()
    check(p._needs_lookup(l), "лот со скрытым владельцем требует поиска")
    await p.process(ListingEvent(l, _t.monotonic(), "live"))
    check(p.sent_l is None and len(p._lookup_tasks) == 1, "поиск ушёл в фон — обработчик свободен сразу")
    await asyncio.gather(*list(p._lookup_tasks))
    check(p.sent_l is l, "лот отправлен после поиска")
    check(l.seller_username == "ivan_222" and l.seller_id == 222 and l.seller_key == "u222",
          "владелец подставлен как обычный продавец")
    text = MessageFormatter(settings.path(settings.template_path)).render(l)
    check("@ivan_222" in text, "в посте @username владельца")
    settings.trade_bot_username = "iostherobot"
    kb = listing_keyboard(l)
    btns = {b.text: b for row in kb.inline_keyboard for b in row}
    check(set(btns) == {"Занять", "Лот", "Написать"}, "кнопки: Занять / Лот / Написать (без «Клик»)")
    check(btns["Написать"].url.startswith("https://t.me/ivan_222?text=%40iostherobot"),
          "«Написать» → ЛС продавца с готовым оффером")
    settings.trade_bot_username = ""
    check(btns and listing_keyboard(l).inline_keyboard[1][1].url == "https://t.me/ivan_222",
          "без торгового бота «Написать» → просто ЛС продавца")

    # скидка оффера: до 1000 ⭐ включительно −30, дальше как было
    from bot.formatter import _trade_offer
    def off(stars):
        return _trade_offer(Listing(listing_id="x", gift_unique_id=1, gift_id=1, gift_name="x", stars_price=stars))
    check(off(1000) == (970, "") and off(500) == (470, ""), "до 1000 ⭐ → −30 (1000 → 970)")
    check(off(1001) == (951, "") and off(1800) == (1750, "") and off(3000) == (2900, ""),
          "от 1001 ⭐ — прежние скидки")

    # не нашли и LOOKUP_REQUIRED=true → лот не уходит
    p = pipeline(FakeClient(REPLY, {111: [], 222: [], 333: []}, users))
    l = hidden()
    await p.process(ListingEvent(l, _t.monotonic(), "live"))
    await asyncio.gather(*list(p._lookup_tasks))
    check(p.sent_l is None and l.status == ListingStatus.FILTERED_SELLER, "владелец не найден → лот не отправлен")

    l2 = Listing(listing_id="1:u1", gift_unique_id=1, gift_id=1, gift_name="x", seller_type="user",
                 seller_id=1, seller_name="A")
    check(not p._needs_lookup(l2), "видимый продавец — без поиска")


asyncio.run(main())
print(f"ALL OK ({ok_count})")
