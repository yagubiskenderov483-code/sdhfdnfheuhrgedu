# Telegram Gift Marketplace — Live Tracker новых листингов

Отслеживает встроенный маркетплейс Telegram Gifts (NFT-подарки за Stars / TON), находит **только новые
листинги**, фильтрует их (цена ≥ `MIN_STARS`, продавец) и сразу постит в канал.

---

## 1. Что реально есть в Telegram API (проверено)

Проверено по схеме Telethon **1.45.0 (MTProto Layer 229)** и core.telegram.org:

| Метод | Для чего |
|---|---|
| `payments.getStarGifts(hash)` | список коллекций. У коллекций с вторичным рынком есть `StarGift.availability_resale` (сколько лотов выставлено) |
| `payments.getResaleStarGifts(gift_id, offset, limit)` | лоты маркета одной коллекции. **Без** `sort_by_price`/`sort_by_num` Telegram сортирует *«by the unixtime (descending) when their resell price was last changed»* — новые листинги всегда наверху |
| `users.getFullUser(id)` | `UserFull.stars_rating.level` (Level), статус платных сообщений |

Каждый лот — объект `starGiftUnique`: `id`, `gift_id`, `title`, `slug` (→ `https://t.me/nft/<slug>`), `num`,
`owner_id` (продавец), `attributes` (модель / фон / узор), `resell_amount` (`StarsAmount` = Stars,
`StarsTonAmount` = TON в нано-единицах), `resale_ton_only`. В ответе приходят `users`/`chats` →
username, Premium, `send_paid_messages_stars`, `contact_require_premium`.

**Чего в API нет — и что с этим сделано:**

* **Realtime-событий о новых лотах маркета нет.** Поэтому — быстрый polling с состоянием (курсором) по каждой
  коллекции, распределённый между всеми sessions. Плюс раз в `CATALOG_INTERVAL` сек `getStarGifts`: если у коллекции
  поменялось `availability_resale`, её опрашивают вне очереди.
* **Отдельного «listing ID» нет.** Используется `listing_id = "<starGiftUnique.id>:<продавец>"`, например
  `5873429911:u7181202828`. Если тот же подарок выставит *другой* продавец — это новый листинг.
* **Даты выставления нет** → `listing_created_at` остаётся пустым, `first_seen` = время обнаружения.
* **Национальности/страны/языка чужого пользователя нет** (`lang_code` приходит только для ботов). См. раздел RUSSIAN_ONLY.
* Курс Stars↔TON не придумывается: показываются только цены, которые вернул Telegram.

## 2. Как определяются НОВЫЕ лоты (без повторов и без пропусков)

Лоты коллекции идут «новые сверху». Для каждой коллекции хранится state — все известные лоты и их цены.
При каждом опросе страница читается сверху вниз:

* лот уже известен и цена **не** менялась → это **якорь**: всё, что выше него, появилось после прошлого опроса;
* неизвестные лоты **выше** якоря → **НОВЫЕ** (в очередь идут *все*, от старых к новым);
* известный лот с изменённой ценой → просто поднялся наверх, **не** новый;
* неизвестные лоты **ниже** якоря → старые лоты, сдвинувшиеся из-за продаж → тихо запоминаются;
* якоря на странице нет (за один опрос появилось больше `PAGE_LIMIT` лотов) → читается следующая страница
  (до `LIVE_MAX_PAGES`, после рестарта до `RECOVERY_MAX_PAGES`);
* якорь не найден вообще → тихая ре-синхронизация + WARNING в лог (лучше пропустить, чем разослать тысячи старых лотов).

Глобальная защита от дублей: перед обработкой лот вставляется в `listings` (`UNIQUE(listing_id)`,
`INSERT OR IGNORE`). Кто вставил первым — тот и обрабатывает. Второй раз лот не пройдёт никогда,
с какой бы session его ни увидели.

У каждого листинга есть конечный статус: `FILTERED_PRICE`, `FILTERED_SELLER`, `SENT`, `TEST_ONLY`,
`SEND_FAILED`, `ERROR`. Если процесс упал посреди обработки, листинги со статусом `NEW` дообрабатываются при следующем запуске.

```
Sessions ─► MarketplaceCoordinator (планировщик, 1 коллекция = 1 запрос за раз)
                 │
                 ▼
        Global Deduplication (UNIQUE listing_id)  ──►  Database (NEW)
                 │
                 ▼
        NewListingsQueue ─► PriceFilter ─► getFullUser (Level/Premium) ─► SellerFilter
                 │
                 ▼
        Database (final status) ─► TelegramSender ─► канал   (sent=1)
```

## 3. Структура

```
gift_tracker/
├── main.py                 # запуск, App, --check
├── config.py               # .env + runtime overrides (/set)
├── .env / .env.example
├── requirements.txt
├── database/   db.py  models.py  migrations.py
├── tracker/    coordinator.py  marketplace.py  worker.py  state.py  queue.py  ratelimit.py
├── filters/    price_filter.py  seller_filter.py
├── sessions/   manager.py  storage.py
├── bot/        admin.py  sender.py  formatter.py
├── utils/      logger.py  links.py
├── templates/  listing.html          # шаблон сообщения (правится свободно)
├── tests/      test_tracker.py       # офлайн-тесты логики
├── data/       tracker.db, seller_whitelist.txt, seller_blacklist.txt, admins.json, runtime_settings.json
├── logs/       app.log  errors.log  listings.log
└── telegram_sessions/  session_001.session (+ .json) ...
```

## 4. Установка

Python 3.10+.

```bash
cd gift_tracker
python -m venv venv
# Windows:  venv\Scripts\activate
# Linux/mac: source venv/bin/activate
pip install -r requirements.txt
```

## 5. Настройка `.env`

`.env` уже заполнен вашими `API_ID`, `API_HASH`, `BOT_TOKEN`, `TARGET_CHAT_ID=-1004297759767`.
Проверьте, что @usndowjdbot — админ канала с правом публикации.

`ADMIN_IDS` — впишите свой Telegram ID (узнать: напишите боту `/start`, он покажет ID).
Если оставить пустым, при запуске в консоли появится строка `/claim a1b2c3d4` — отправьте её боту,
и вы станете админом (сохранится в `data/admins.json`).

⚠️ `.env` и `telegram_sessions/` нельзя коммитить в git (уже есть в `.gitignore`).

## 6. Запуск

```bash
python main.py --check     # проверка: БД, бот, канал, sessions, getStarGifts, getResaleStarGifts, фильтры, формат
python main.py             # боевой режим
```

Рекомендуется первый запуск с `TEST_MODE=true`: всё работает, но в канал ничего не уходит —
первые `TEST_MAX_MESSAGES` сообщений приходят вам в личку, остальное только в `logs/listings.log`.

## 7. Первая session

1. Запустите `python main.py`.
2. В личке с ботом: `/add_session`.
3. API ID → `-` (взять из .env) или число.
4. API HASH → `-` или hash (сообщение сразу удаляется).
5. Номер телефона `+79991234567`.
6. Код входа — **с пробелами**: `1 2 3 4 5` (иначе Telegram заблокирует вход как «пересланный код»).
7. Если есть 2FA — пароль (сообщение удаляется).
8. Бот ответит `✅ Session session_001 добавлена`, worker сразу подключается к координатору.

Файл сохраняется в `telegram_sessions/session_001.session`, после рестарта подхватывается автоматически.
Используйте отдельные аккаунты, а не основной.

## 8. Много sessions

* Через бота: `/add_session` сколько нужно раз (имена session_002, session_003… выдаются автоматически).
* Или положите готовые Telethon-файлы `*.session` в `telegram_sessions/` и отправьте `/reload`
  (или перезапустите). Если у session свой api_id/api_hash — рядом положите `session_XXX.json`:
  `{"api_id": 123, "api_hash": "..."}`, иначе берутся из `.env`.
* `/sessions` — сводка и причины ошибок; `/remove_session session_023` — убрать (файл переносится в `removed/`).
* Сломанная session (отозвана авторизация, бан, обрыв) не останавливает остальные: обрыв → reconnect с
  экспоненциальной задержкой, отзыв → `AUTH_ERROR` и исключение из ротации.

**Сколько нужно sessions.** Одна session делает `SESSION_RPS` запросов/сек (по умолчанию 1).
Коллекций с маркетом сейчас порядка сотни+, каждая = 1 запрос за опрос. Реальный цикл
≈ `коллекций / (sessions × SESSION_RPS)`; его показывает `/status` («Poll cycle»). Например, 150 коллекций и 20 sessions
при 1 RPS → каждая коллекция проверяется примерно раз в 7–8 сек; 150 sessions → ~1 сек.
Сократить объём можно через `COLLECTIONS=` (только нужные коллекции).
Точный безопасный лимит Telegram не публикует — при FloodWait session честно ждёт указанное время,
а её запросы уходят другим sessions. Не поднимайте `SESSION_RPS`, если в логах часто FloodWait.

## 9. MIN_STARS / MIN_TON

```
MIN_STARS=5000        # 4999 ❌, 5000 ✅
MIN_TON=0             # 0 = не проверять TON
ALLOW_TON_ONLY=false  # лоты, продающиеся только за TON, по умолчанию не отправляются
```
Или на лету из бота: `/set MIN_STARS 10000` (сохраняется в `data/runtime_settings.json`, перекрывает .env).

## 10. RUSSIAN_ONLY (только русские продавцы)

Telegram не отдаёт страну/национальность. Имя, фото и username не используются.
Продавец считается русским, если есть объективный признак (`filters/seller_filter.py`):

1. `data/seller_blacklist.txt` → отсеять; `data/seller_whitelist.txt` → пропустить;
2. видимый телефон начинается с `SELLER_PHONE_PREFIXES` (7);
3. **описание профиля (bio) написано по-русски** — кириллица, без украинских/казахских/белорусских букв.

Остальные при `UNKNOWN_SELLER_POLICY=block` (по умолчанию) отсеиваются — в канал идут только русские.
`allow` — пропускать и тех, о ком данных нет.

## 10.1 Повторы

`UNIQUE_GIFTS=true` — один и тот же NFT второй раз не отправляется (даже от другого продавца / по другой цене).
`UNIQUE_SELLERS=true` — один и тот же продавец второй раз не отправляется;
`SELLER_REPEAT_HOURS=24` — разрешить повтор продавца через 24 часа (0 = никогда).

## 10.2 Аккаунты не пропадают после деплоя

После каждого `/add_session` бот шифрует все sessions (ключ `SESSION_BACKUP_KEY`) и закрепляет файл
`sessions.backup` в личке первого админа из `ADMIN_IDS` (или в `SESSIONS_BACKUP_CHAT_ID`).
При запуске бот сам скачивает закреплённую копию и восстанавливает аккаунты. Аккаунтов — сколько угодно.
**Обязательно** впишите свой ID в `ADMIN_IDS` (бот показывает его в `/start`) и не откреплять сообщение.

## 11. INITIAL SYNC

При первом запуске (коллекция ещё не в БД):

```
[01:20:01] [INFO] Initial marketplace sync... (152 collections)
[01:20:08] [INFO] Existing listings saved: 18342
[01:20:08] [INFO] Initial sync completed
[01:20:08] [INFO] LIVE TRACKER ENABLED 🟢
```
Для каждой коллекции берётся верх рынка (`INITIAL_SYNC_PAGES` × 100 лотов) и сохраняется в `market_items`
**без отправки**. Скачивать все десятки тысяч лотов не нужно: старые лоты не поднимаются наверх,
а «съехавшие» снизу распознаются как старые (см. п.2). Новая коллекция, появившаяся позже, тоже
сначала синхронизируется молча. `INITIAL_SYNC=false` — существующие лоты тоже будут обработаны (не рекомендуется).

## 12. LIVE TRACKER

```
[01:20:10] [NEW] Listing #5873429911:u7181202828 | Ice Cream #92615 | seller u7181202828 | via session_004 (live)
[01:20:10] [PRICE] #5873429911:u7181202828 10000 Stars / 105.45 TON
[01:20:10] [FILTER] #5873429911:u7181202828 price passed
[01:20:10] [FILTER] #5873429911:u7181202828 seller passed (UNKNOWN_ALLOWED)
[01:20:10] [SEND] Listing #5873429911:u7181202828 (channel, 0.41s after detection)
```
Коллекции опрашиваются по кругу с целевым интервалом `TRACKER_INTERVAL`, параллельно не более
`MAX_CONCURRENT_SESSIONS` запросов, каждая коллекция — только одной session за раз.

## 13. Перезапуск / восстановление

Состояние хранится в SQLite: `market_items` (известные лоты и цены), `collection_state`
(`last_cursor`, `last_seen_listing_id`, `last_seen_timestamp`, `complete` по каждой коллекции),
`kv` (глобальные `last_seen_*`, счётчики), `listings` (все обработанные новые листинги).

При запуске: загрузить state → дообработать листинги со статусом `NEW` → режим **RECOVERY**: каждая коллекция
читается сверху до первого якоря (до `RECOVERY_MAX_PAGES` страниц) — всё, что появилось, пока программа
была выключена, обрабатывается и отправляется → **LIVE 🟢**. Весь маркет заново не скачивается,
лоты, известные до выключения, повторно не отправляются.

## 14. Шаблон сообщения

`templates/listing.html` (HTML-разметка Telegram). Переменные: `{gift_title_link} {gift_name} {gift_link}
{gift_num} {model} {backdrop} {symbol} {price_line} {stars_price} {ton_price} {currency} {seller_line}
{seller_username} {seller_id} {seller_name} {seller_profile} {seller_check} {level} {messages}
{premium_status} {listing_id} {first_seen}`. После правки — `/reload`.

Ссылка на продавца: при наличии username — `https://t.me/<username>`; без username — ссылка
`tg://user?id=<ID>` (откроется, только если приватность продавца это позволяет) и сам ID. Username никогда не выдумывается.
Продавец может быть и каналом (`owner_id` = канал) или TON-кошельком (`owner_address`) — это тоже учитывается.

## 15. Команды бота

`/start /status /sessions /add_session /remove_session /reload /stats /test /settings /set /cancel`
`/test` — полная проверка + пример сообщения вам в личку (в канал не уходит); `/test channel` — тестовый пост в канал.

## 16. Тесты логики (без Telegram)

```bash
python -m tests.test_tracker
```
Имитирует маркет: initial sync без отправки, всплеск лотов больше страницы, смена цены, перепродажа другим
продавцом, глобальные дубли, пропущенные за время выключения лоты.

## 17. PostgreSQL

Весь SQL сосредоточен в `database/db.py` и `database/migrations.py`. Для перехода: заменить aiosqlite на asyncpg
в классе `Database` (интерфейс методов тот же), `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL`,
`INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`, плейсхолдеры `?` → `$1..$n`.

## 18. Хостинг

Нужен постоянный диск (для `telegram_sessions/` и `data/`). На Render — Background Worker + Persistent Disk,
либо VPS. Запуск: `python main.py`.
