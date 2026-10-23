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

    def usable(self):
        return list(self.workers.values())

    async def run(self, fn, only=None, attempts=1, timeout=None, **kw):
        return await fn(self.client), only


REPLY = "Найдено 3:\n1. @ivan_real — Иван\n2. Иван Иванов ID: 222\n3. ещё один\nКанал: t.me/funstat_news"


async def main():
    settings.lookup_bot_username = "funstat"
    settings.auto_lookup = True
    settings.lookup_session = ""
    settings.lookup_timeout = 8

    # --- парсер кандидатов
    c = FakeClient(REPLY)
    msgs = await c.get_messages(None, min_id=5)
    keys = [x.key() for x in L.parse_candidates(msgs, "funstat")]
    check("id222" in keys, "упоминание по ID → кандидат")
    check("@ivan_real" in keys, "@username в тексте → кандидат")
    check("@ivan_third" in keys, "URL-кнопка t.me → кандидат")
    check("@funstat" not in keys, "сам бот поиска не кандидат")

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

    # выключено → ничего не делаем
    settings.auto_lookup = False
    c = FakeClient(REPLY, gifts, users)
    lk = L.OwnerLookup(FakeSessions(c))
    check(await lk.find_owner("Иван Иванов", "PlushPepe-77", 5555) is None and not c.sent,
          "AUTO_LOOKUP=false → боту не пишем")
    settings.auto_lookup = True

    # --- конвейер: пост редактируется, в нём @username
    from tracker.queue import ListingPipeline
    from bot.formatter import MessageFormatter

    class FakeSender:
        def __init__(self):
            self.edits = []

        async def edit_listing(self, chat_id, message_id, text, preview, markup=None, fallback=None):
            self.edits.append((chat_id, message_id, text, markup))
            return True

    class FakeDB:
        async def update_listing(self, l):
            self.l = l

    c = FakeClient(REPLY, gifts, users)
    p = ListingPipeline.__new__(ListingPipeline)
    p.owner_lookup = L.OwnerLookup(FakeSessions(c))
    p.sender, p.db = FakeSender(), FakeDB()
    p.formatter = MessageFormatter(settings.path(settings.template_path))
    p.stats = {}
    l = Listing(listing_id="5555:n:Иван Иванов", gift_unique_id=5555, gift_id=1, gift_name="Plush Pepe",
                gift_slug="PlushPepe-77", gift_link="https://t.me/nft/PlushPepe-77", seller_key="n:Иван Иванов",
                seller_name="Иван Иванов", stars_price=10000)
    check(p._needs_lookup(l), "лот со скрытым владельцем требует поиска")
    await p._lookup_and_edit(l, -100123, 42)
    check(len(p.sender.edits) == 1, "пост отредактирован")
    chat, mid, text, markup = p.sender.edits[0]
    check(chat == -100123 and mid == 42, "правится нужное сообщение")
    check("@ivan_222" in text and "funstat" in text, "в посте @username и пометка о поиске")
    btns = [b.text for row in markup.inline_keyboard for b in row]
    check("🔍 Клик" not in btns, "кнопка ручного поиска убрана после находки")
    check(p.db.l.seller_username == "ivan_222", "найденный владелец сохранён в БД")

    l2 = Listing(listing_id="1:u1", gift_unique_id=1, gift_id=1, gift_name="x", seller_type="user",
                 seller_id=1, seller_name="A")
    check(not p._needs_lookup(l2), "видимый продавец — без поиска")


asyncio.run(main())
print(f"ALL OK ({ok_count})")
