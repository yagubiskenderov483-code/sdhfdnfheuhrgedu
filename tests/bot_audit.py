"""Запуск:  python -m tests.bot_audit

Полная проверка бота на имитации Telegram: команды, кнопки, колбэки, вход аккаунта, прокси, пост."""
import re, asyncio, os, sys, tempfile, datetime
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, os.getcwd())
from config import settings
import main
from aiogram.types import Update, Message, Chat, User, CallbackQuery, InlineKeyboardMarkup
from aiogram import methods, Bot
import tests.test_tracker as t
import bot.admin as admin_mod

OK_TAGS = {"b","i","u","s","code","pre","a","blockquote","tg-spoiler"}
problems = []
sent = []
def check_html(text, where):
    for tag in re.findall(r"<([^>]*)>?", text or ""):
        name = tag.strip("/").split(" ")[0]
        if name not in OK_TAGS:
            problems.append(f"BAD HTML in {where}: <{tag[:30]}>")
    if len(text or "") > 4096:
        problems.append(f"TOO LONG ({len(text)}) in {where}")

async def fake_call(self, method, request_timeout=None):
    txt = getattr(method, "text", None) or getattr(method, "caption", None)
    sent.append((type(method).__name__, txt, method))
    if getattr(method, "parse_mode", "x") != None and txt:
        check_html(txt, type(method).__name__)
    now = datetime.datetime.now()
    if isinstance(method, methods.SendMessage):
        return Message(message_id=len(sent), date=now, chat=Chat(id=method.chat_id, type="private"), text=method.text,
                       reply_markup=method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None)
    if isinstance(method, methods.SendDocument):
        return Message(message_id=len(sent), date=now, chat=Chat(id=method.chat_id, type="private"),
                       document={"file_id": f"f{len(sent)}", "file_unique_id": "u", "file_name": "x"})
    if isinstance(method, methods.GetWebhookInfo):
        return NS(url="https://other.example/hook")
    if isinstance(method, methods.GetMyDescription):
        return NS(description="")
    if isinstance(method, methods.GetUpdates):
        await asyncio.sleep(3600)
    if isinstance(method, (methods.EditMessageText, methods.EditMessageReplyMarkup)):
        return True
    return True
Bot.__call__ = fake_call

# ---- имитация Telethon для /add_session
class FakeTC:
    def __init__(self, path, api_id, api_hash, proxy=None, **kw):
        self.path, self.proxy = path, proxy
    async def connect(self): pass
    async def disconnect(self): pass
    async def send_code_request(self, phone): return NS(phone_code_hash="h")
    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        from telethon import errors
        if code and code != "12345": raise errors.PhoneCodeInvalidError(request=None)
        if code: raise errors.SessionPasswordNeededError(request=None)
        if password != "pw": raise errors.PasswordHashInvalidError(request=None)
        Path(self.path + ".session").write_bytes(b"")
    async def get_me(self): return NS(id=424242)
admin_mod.TelegramClient = FakeTC

uid_seq = [100]
async def say(app, uid, text, chat_type="private"):
    sent.clear(); uid_seq[0] += 1
    upd = Update(update_id=uid_seq[0], message=Message(message_id=uid_seq[0], date=datetime.datetime.now(),
                 chat=Chat(id=uid, type=chat_type), from_user=User(id=uid, is_bot=False, first_name="u", username="me"), text=text))
    try:
        await app.dp.feed_update(app.bot, upd)
    except Exception as e:
        problems.append(f"EXCEPTION on {text!r}: {e!r}")
    return [(x or "")[:60].replace("\n", " | ") for m, x, _ in sent if m == "SendMessage"]

async def click(app, uid, data, msg_text="post", kb=None):
    sent.clear(); uid_seq[0] += 1
    msg = Message(message_id=5, date=datetime.datetime.now(), chat=Chat(id=-100, type="channel"), text=msg_text, reply_markup=kb)
    upd = Update(update_id=uid_seq[0], callback_query=CallbackQuery(id=str(uid_seq[0]), from_user=User(id=uid, is_bot=False, first_name="u", username="clicker"),
                 chat_instance="c", data=data, message=msg))
    try:
        await app.dp.feed_update(app.bot, upd)
    except Exception as e:
        problems.append(f"EXCEPTION on callback {data!r}: {e!r}")
    return [(type(m).__name__, (getattr(m, "text", None) or "")[:50]) for _, _, m in sent]

async def run():
    from utils.logger import setup_logging; setup_logging(Path(tempfile.mkdtemp()), "WARNING")
    tmp = Path(tempfile.mkdtemp())
    settings.database_path = str(tmp / "db.sqlite"); settings.sessions_dir = str(tmp / "s")
    settings.startup_delay = 0; settings.admin_ids = set(); settings.allow_all_users = True
    admin_mod.ADMINS_FILE = tmp / "admins.json"
    market = t.FakeMarket(); market.floors = {1: 6000}
    for _ in range(30): market.list_new(market.item())
    t.patch_market(market)
    app = main.App()
    app.storage.dir = tmp / "s"; app.storage.dir.mkdir(parents=True, exist_ok=True)
    # сессии: подменяем подключение на фейк
    from tracker.worker import WorkerStatus
    async def fake_connect(self):
        self.status = WorkerStatus.ACTIVE; return True
    import tracker.worker as wm
    wm.SessionWorker.connect = fake_connect
    async def fake_run(fn, **kw):
        return await fn(None), "session_001"
    app.sessions.run = fake_run
    fsend = t.FakeSender()
    async def capture(text, preview, markup=None, fallback_markup=None, target=None, thread=None):
        check_html(text, "LISTING POST"); return await fsend.send_listing(text, preview, markup, fallback_markup, target, thread)
    app.pipeline.sender.send_listing = capture
    await app.start()
    await asyncio.sleep(1.5)

    OWNER, STRANGER = 777, 555
    # 1) добавление аккаунта: неверный код, 2FA, неверный пароль, верный
    for step in ["/add_session", "+79991234567", "1 1 1 1 1", "1 2 3 4 5", "wrong", "pw"]:
        r = await say(app, OWNER, step); print(f"add_session {step!r:18} -> {r[:1]}")
    print("sessions after add:", list(app.sessions.workers), "owner admin:", admin_mod.is_manager(OWNER))
    # 2) все команды и кнопки владельцем и чужим
    cmds = ["/start","/status","/sessions","/stats","/settings","/last","/collections","/test","/cancel",
            "/pause","/resume","/proxies","/reload","/set MIN_STARS 5000","/set BAD 1","/set MIN_STARS abc",
            "/proxy session_001 socks5://u:p@1.2.3.4:1080","/proxy session_001 off","/remove_session",
            "📊 Статус","📈 Статистика","⏸ Пауза","▶️ Продолжить","👥 Аккаунты","🕘 Последние лоты","📚 Коллекции",
            "/claim abc","привет"]
    for c in cmds:
        r = await say(app, OWNER, c)
        if not r and c not in ("привет", "/claim abc"): problems.append(f"NO REPLY (owner) to {c!r}")
    for c in ["/status","/pause","⏸ Пауза","/proxies","/set MIN_STARS 1"]:
        r = await say(app, STRANGER, c)
        if not r: problems.append(f"NO REPLY (stranger) to {c!r}")
    r = await say(app, OWNER, "/proxies\nsocks5://u:p@1.1.1.1:1080\n2.2.2.2:1080:a:b")
    print("proxies set ->", r[:1])
    # 3) новый лот → пост в канал с кнопками
    for _ in range(40):
        if app.coordinator.sync_progress()[0] == 1 and await app.db.count_market_items() > 0: break
        await asyncio.sleep(0.5)
    settings.russian_only = False; settings.require_free_messages = False; settings.max_seller_nfts = 0; settings.max_seller_level = 0
    newit = market.item(9000); market.list_new(newit)
    await app.coordinator.refresh_catalog(); await asyncio.sleep(2)
    print("posts sent:", len(fsend.sent))
    if len(fsend.sent) != 1: problems.append("new lot was not posted to the channel")
    # 4) кнопки под постом
    from bot.formatter import listing_keyboard
    from database.models import Listing
    kb = listing_keyboard(Listing(listing_id="1:u1", gift_unique_id=1, gift_id=1, gift_name="X", gift_link="https://t.me/nft/X-1",
                                  seller_type="user", seller_id=1, seller_username="abc", seller_profile_link="https://t.me/abc"))
    print("take ->", await click(app, 999, "take:1", kb=kb))
    print("taken ->", await click(app, 998, "taken"))
    # 5) удаление аккаунта кнопками
    print("rmsess ->", await click(app, OWNER, "rmsess:session_001"))
    print("rmsess_no ->", await click(app, OWNER, "rmsess_no"))
    print("rmsess stranger ok ->", await click(app, STRANGER, "rmsess_ok:session_001"))
    print("rmsess_ok ->", await click(app, OWNER, "rmsess_ok:session_001"), "left:", list(app.sessions.workers))
    # 6) остановка
    try:
        await asyncio.wait_for(app.stop(), 30); print("stop: OK")
    except Exception as e:
        problems.append(f"stop failed: {e!r}")
    print("\nPROBLEMS:" if problems else "\nNO PROBLEMS FOUND", *problems, sep="\n  ")
    os._exit(1 if problems else 0)
asyncio.run(run())
