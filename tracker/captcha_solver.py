"""Авто-решатель капчи funstat / fun_xsearchx_bot и аналогов.

Включает кеш решённых капч (image_hash → button_index): если bot прислал
точно такую же картинку, которую мы уже решали, мгновенно повторяем ответ
без OCR. Это ускоряет повторные одинаковые капчи и повышает точность.

Капча: картинка со СЛОВОМ (напр. "Star", "Fox", "Dog") + 2–8 кнопок с эмодзи.
Надо нажать ту кнопку, чьё эмодзи означает это слово.

Стратегия (по убыванию приоритета):
  1. OCR (tesseract) — локально, бесплатно, оффлайн. Слово → эмодзи по словарю + unicodedata.name.
  2. Vision API (OpenAI-совместимый) — если задан CAPTCHA_API_KEY. Fallback если OCR не распознал.
  3. Админ — если ничего не вышло.

Зависимости:
  pytesseract + Pillow (в requirements.txt)
  tesseract-ocr (apt install, в Dockerfile) — иначе OCR отключается, работает только vision/админ.

Env:
  CAPTCHA_AUTO_SOLVE   — вкл/выкл (по умолчанию: вкл)
  CAPTCHA_API_KEY      — опционально, ключ vision API как второй уровень
  CAPTCHA_API_BASE     — https://api.openai.com/v1 (по умолчанию)
  CAPTCHA_MODEL        — gpt-4o-mini
  CAPTCHA_TIMEOUT      — 25
  CAPTCHA_MAX_RETRIES  — 2
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import unicodedata
from typing import Iterable, Optional
from urllib import request as _urlreq
from urllib.error import HTTPError, URLError

log = logging.getLogger("captcha")

# Кеш решённых капч: hash(image_bytes + buttons_signature) → индекс кнопки.
# Персистится на диск (CAPTCHA_CACHE_PATH или /var/data/captcha_cache.json),
# чтобы Render-деплои / рестарты не теряли накопленные ответы.
_CAPTCHA_CACHE: dict[str, int] = {}
_CAPTCHA_CACHE_MAX = 5000      # LRU-ish очистка при превышении
_CAPTCHA_CACHE_PATH = (os.environ.get("CAPTCHA_CACHE_PATH") or "/var/data/captcha_cache.json").strip()
_cache_loaded = False
_cache_dirty = False


def _cache_key(photo: bytes, buttons: list[str]) -> str:
    h = hashlib.md5(photo).hexdigest()[:16]
    sig = "|".join(buttons)
    return f"{h}:{sig}"


def _load_cache() -> None:
    """Разово при первом обращении — читаем JSON с диска в _CAPTCHA_CACHE."""
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        if os.path.exists(_CAPTCHA_CACHE_PATH):
            with open(_CAPTCHA_CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, int):
                        _CAPTCHA_CACHE[k] = v
            log.info("Captcha cache загружен: %d записей из %s",
                     len(_CAPTCHA_CACHE), _CAPTCHA_CACHE_PATH)
    except Exception as e:                                             # noqa: BLE001
        log.warning("Captcha cache: не удалось прочитать %s: %s", _CAPTCHA_CACHE_PATH, e)


def _save_cache() -> None:
    """Записывает кеш на диск. Вызывать не чаще раза в минуту (при изменениях)."""
    global _cache_dirty
    if not _cache_dirty:
        return
    _cache_dirty = False
    try:
        os.makedirs(os.path.dirname(_CAPTCHA_CACHE_PATH) or ".", exist_ok=True)
        tmp = _CAPTCHA_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CAPTCHA_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _CAPTCHA_CACHE_PATH)
    except Exception as e:                                             # noqa: BLE001
        log.warning("Captcha cache: не удалось сохранить %s: %s", _CAPTCHA_CACHE_PATH, e)


def cache_put(key: str, idx: int) -> None:
    """Добавить в кеш (с LRU-очисткой и пометкой dirty для последующего сохранения)."""
    global _cache_dirty
    _CAPTCHA_CACHE[key] = idx
    _cache_dirty = True
    if len(_CAPTCHA_CACHE) > _CAPTCHA_CACHE_MAX:
        for k in list(_CAPTCHA_CACHE)[:_CAPTCHA_CACHE_MAX // 4]:
            _CAPTCHA_CACHE.pop(k, None)


def cache_pop(key: str) -> None:
    global _cache_dirty
    if _CAPTCHA_CACHE.pop(key, None) is not None:
        _cache_dirty = True


_LAYER_HITS: dict[str, int] = {}      # сколько раз каждый слой матчера сработал


def captcha_cache_stats() -> dict:
    """Для вывода в /panel."""
    _load_cache()
    return {
        "size": len(_CAPTCHA_CACHE),
        "max": _CAPTCHA_CACHE_MAX,
        "path": _CAPTCHA_CACHE_PATH,
        "layer_hits": dict(_LAYER_HITS),
    }


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v else default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


# ---------------------------------------------------------------------------
# Словарь: слово (нижний регистр, ASCII) → список эмодзи, которые его означают.
# Если слова нет в словаре — пробуем через unicodedata.name().
# Пополняем по мере появления новых капч.
# ---------------------------------------------------------------------------
_WORD_TO_EMOJI: dict[str, tuple[str, ...]] = {
    # ── животные (земля) ──
    "fox": ("🦊",), "лиса": ("🦊",),
    "dog": ("🐶", "🐕", "🐩"), "puppy": ("🐶",), "собака": ("🐶", "🐕"), "пёс": ("🐶",), "пес": ("🐶",),
    "cat": ("🐱", "🐈"), "kitten": ("🐱",), "кот": ("🐱", "🐈"), "кошка": ("🐱",),
    "rabbit": ("🐰", "🐇"), "bunny": ("🐰",), "кролик": ("🐰", "🐇"), "заяц": ("🐰",),
    "bear": ("🐻", "🧸"), "медведь": ("🐻",), "teddy": ("🧸",),
    "panda": ("🐼",), "панда": ("🐼",),
    "lion": ("🦁",), "лев": ("🦁",),
    "tiger": ("🐯", "🐅"), "тигр": ("🐯",),
    "pig": ("🐷", "🐖"), "свинья": ("🐷",), "хрюшка": ("🐷",),
    "cow": ("🐮", "🐄"), "корова": ("🐮",),
    "horse": ("🐴", "🐎"), "лошадь": ("🐴",), "конь": ("🐴",),
    "monkey": ("🐵", "🐒"), "обезьяна": ("🐵",),
    "mouse": ("🐭", "🐁"), "мышь": ("🐭",), "мышка": ("🐭",), "крыса": ("🐀",), "rat": ("🐀",),
    "frog": ("🐸",), "лягушка": ("🐸",),
    "koala": ("🐨",), "коала": ("🐨",),
    "wolf": ("🐺",), "волк": ("🐺",),
    "chicken": ("🐔", "🐓"), "курица": ("🐔",), "петух": ("🐓",), "rooster": ("🐓",),
    "bird": ("🐦", "🐤"), "птица": ("🐦",),
    "penguin": ("🐧",), "пингвин": ("🐧",),
    "unicorn": ("🦄",), "единорог": ("🦄",),
    "elephant": ("🐘",), "слон": ("🐘",),
    "deer": ("🦌",), "олень": ("🦌",),
    "giraffe": ("🦒",), "жираф": ("🦒",),
    "zebra": ("🦓",), "зебра": ("🦓",),
    "camel": ("🐫", "🐪"), "верблюд": ("🐫",),
    "sheep": ("🐑",), "овца": ("🐑",), "goat": ("🐐",), "коза": ("🐐",),
    "snake": ("🐍",), "змея": ("🐍",),
    "turtle": ("🐢",), "черепаха": ("🐢",),
    "crocodile": ("🐊",), "крокодил": ("🐊",),
    "lizard": ("🦎",), "ящерица": ("🦎",),
    "dragon": ("🐲", "🐉"), "дракон": ("🐲", "🐉"),
    "dinosaur": ("🦕", "🦖"), "динозавр": ("🦕",),
    "owl": ("🦉",), "сова": ("🦉",),
    "eagle": ("🦅",), "орёл": ("🦅",), "орел": ("🦅",),
    "duck": ("🦆",), "утка": ("🦆",),
    "swan": ("🦢",), "лебедь": ("🦢",),
    "peacock": ("🦚",), "павлин": ("🦚",),
    "parrot": ("🦜",), "попугай": ("🦜",),
    "flamingo": ("🦩",), "фламинго": ("🦩",),
    "hamster": ("🐹",), "хомяк": ("🐹",),
    "squirrel": ("🐿",), "белка": ("🐿",),
    "hedgehog": ("🦔",), "ёж": ("🦔",), "еж": ("🦔",),
    "bat": ("🦇",), "летучая": ("🦇",),
    # ── насекомые ──
    "bee": ("🐝",), "пчела": ("🐝",),
    "butterfly": ("🦋",), "бабочка": ("🦋",),
    "spider": ("🕷",), "паук": ("🕷",),
    "scorpion": ("🦂",), "скорпион": ("🦂",),
    "snail": ("🐌",), "улитка": ("🐌",),
    "ant": ("🐜",), "муравей": ("🐜",),
    "bug": ("🐛", "🪲"), "жук": ("🪲",),
    # ── вода ──
    "fish": ("🐟", "🐠", "🐡"), "рыба": ("🐟", "🐠"),
    "shark": ("🦈",), "акула": ("🦈",),
    "whale": ("🐳", "🐋"), "кит": ("🐳", "🐋"),
    "dolphin": ("🐬",), "дельфин": ("🐬",),
    "octopus": ("🐙",), "осьминог": ("🐙",),
    "crab": ("🦀",), "краб": ("🦀",),
    "shrimp": ("🦐",), "креветка": ("🦐",),
    "lobster": ("🦞",), "омар": ("🦞",),
    "squid": ("🦑",), "кальмар": ("🦑",),
    # ── природа / погода ──
    "star": ("⭐", "🌟", "✨"), "звезда": ("⭐", "🌟"),
    "sun": ("☀", "☀️", "🌞", "🌅"), "солнце": ("☀", "☀️"),
    "moon": ("🌙", "🌚", "🌝", "🌛", "🌜"), "луна": ("🌙",),
    "cloud": ("☁", "☁️", "⛅"), "облако": ("☁", "☁️"), "туча": ("☁",),
    "rain": ("🌧", "🌧️", "☔"), "дождь": ("🌧",),
    "snow": ("❄", "❄️", "☃", "⛄"), "снег": ("❄",), "снеговик": ("☃", "⛄"), "snowman": ("☃", "⛄"),
    "water": ("💧",), "вода": ("💧",), "капля": ("💧",), "drop": ("💧",),
    "lightning": ("⚡",), "молния": ("⚡",), "bolt": ("⚡",),
    "rainbow": ("🌈",), "радуга": ("🌈",),
    "tornado": ("🌪",), "торнадо": ("🌪",),
    "fire": ("🔥",), "огонь": ("🔥",),
    "wind": ("💨",), "ветер": ("💨",),
    "earth": ("🌍", "🌎", "🌏"), "земля": ("🌍",), "planet": ("🪐",), "планета": ("🪐",),
    "wave": ("🌊",), "волна": ("🌊",),
    "mountain": ("⛰", "🏔"), "гора": ("⛰",), "вулкан": ("🌋",), "volcano": ("🌋",),
    # ── растения ──
    "flower": ("🌸", "🌼", "🌺", "🌻"), "цветок": ("🌸", "🌼"), "роза": ("🌹",), "rose": ("🌹",),
    "sunflower": ("🌻",), "подсолнух": ("🌻",),
    "tulip": ("🌷",), "тюльпан": ("🌷",),
    "cactus": ("🌵",), "кактус": ("🌵",),
    "tree": ("🌳", "🌲", "🌴"), "дерево": ("🌳",), "palm": ("🌴",), "пальма": ("🌴",),
    "leaf": ("🍃", "🍂"), "лист": ("🍃",),
    "clover": ("🍀", "☘"), "клевер": ("🍀",),
    "mushroom": ("🍄",), "гриб": ("🍄",),
    "herb": ("🌿",), "трава": ("🌿",),
    # ── еда ──
    "apple": ("🍎", "🍏"), "яблоко": ("🍎",),
    "banana": ("🍌",), "банан": ("🍌",),
    "cherry": ("🍒",), "вишня": ("🍒",), "черешня": ("🍒",),
    "grape": ("🍇",), "виноград": ("🍇",),
    "orange": ("🍊",), "апельсин": ("🍊",), "мандарин": ("🍊",),
    "lemon": ("🍋",), "лимон": ("🍋",),
    "strawberry": ("🍓",), "клубника": ("🍓",),
    "watermelon": ("🍉",), "арбуз": ("🍉",),
    "melon": ("🍈",), "дыня": ("🍈",),
    "peach": ("🍑",), "персик": ("🍑",),
    "pear": ("🍐",), "груша": ("🍐",),
    "pineapple": ("🍍",), "ананас": ("🍍",),
    "kiwi": ("🥝",), "киви": ("🥝",),
    "coconut": ("🥥",), "кокос": ("🥥",),
    "avocado": ("🥑",), "авокадо": ("🥑",),
    "tomato": ("🍅",), "помидор": ("🍅",), "томат": ("🍅",),
    "carrot": ("🥕",), "морковь": ("🥕",),
    "corn": ("🌽",), "кукуруза": ("🌽",),
    "pepper": ("🌶", "🫑"), "перец": ("🌶",),
    "potato": ("🥔",), "картофель": ("🥔",), "картошка": ("🥔",),
    "eggplant": ("🍆",), "баклажан": ("🍆",),
    "cucumber": ("🥒",), "огурец": ("🥒",),
    "garlic": ("🧄",), "чеснок": ("🧄",),
    "onion": ("🧅",), "лук": ("🧅",),
    "bread": ("🍞", "🥖"), "хлеб": ("🍞",), "багет": ("🥖",),
    "cheese": ("🧀",), "сыр": ("🧀",),
    "egg": ("🥚", "🍳"), "яйцо": ("🥚",),
    "meat": ("🍖", "🥩"), "мясо": ("🍖",), "steak": ("🥩",),
    "bacon": ("🥓",), "бекон": ("🥓",),
    "chicken_leg": ("🍗",), "курица": ("🍗", "🐔"),
    "hamburger": ("🍔",), "бургер": ("🍔",),
    "fries": ("🍟",), "картошка_фри": ("🍟",),
    "pizza": ("🍕",), "пицца": ("🍕",),
    "hotdog": ("🌭",), "хотдог": ("🌭",),
    "sandwich": ("🥪",), "сэндвич": ("🥪",),
    "taco": ("🌮",), "burrito": ("🌯",),
    "sushi": ("🍣",), "суши": ("🍣",),
    "ramen": ("🍜",), "рамен": ("🍜",),
    "soup": ("🍲",), "суп": ("🍲",),
    "cake": ("🎂", "🍰"), "торт": ("🎂", "🍰"), "cupcake": ("🧁",), "кекс": ("🧁",),
    "cookie": ("🍪",), "печенье": ("🍪",),
    "chocolate": ("🍫",), "шоколад": ("🍫",),
    "candy": ("🍬", "🍭"), "конфета": ("🍬",), "lollipop": ("🍭",), "леденец": ("🍭",),
    "donut": ("🍩",), "пончик": ("🍩",),
    "icecream": ("🍦", "🍨"), "мороженое": ("🍦",),
    "coffee": ("☕",), "кофе": ("☕",),
    "tea": ("🍵",), "чай": ("🍵",),
    "beer": ("🍺", "🍻"), "пиво": ("🍺",),
    "wine": ("🍷",), "вино": ("🍷",),
    "cocktail": ("🍸", "🍹"), "коктейль": ("🍸",),
    "champagne": ("🍾", "🥂"), "шампанское": ("🍾",),
    "milk": ("🥛",), "молоко": ("🥛",),
    "juice": ("🧃",), "сок": ("🧃",),
    "popcorn": ("🍿",), "попкорн": ("🍿",),
    "salt": ("🧂",), "соль": ("🧂",),
    "honey": ("🍯",), "мёд": ("🍯",), "мед": ("🍯",),
    # ── транспорт ──
    "car": ("🚗", "🏎"), "машина": ("🚗",), "авто": ("🚗",),
    "taxi": ("🚕",), "такси": ("🚕",),
    "bus": ("🚌",), "автобус": ("🚌",),
    "truck": ("🚚",), "грузовик": ("🚚",),
    "bicycle": ("🚲",), "велосипед": ("🚲",), "bike": ("🚲",),
    "motorcycle": ("🏍",), "мотоцикл": ("🏍",),
    "train": ("🚂", "🚆", "🚄"), "поезд": ("🚂", "🚆"),
    "plane": ("✈", "✈️", "🛩"), "самолёт": ("✈", "✈️"), "самолет": ("✈",),
    "helicopter": ("🚁",), "вертолёт": ("🚁",),
    "rocket": ("🚀",), "ракета": ("🚀",),
    "boat": ("⛵", "🚤"), "лодка": ("⛵", "🚤"), "ship": ("🚢",), "корабль": ("🚢",),
    "ufo": ("🛸",), "нло": ("🛸",),
    "anchor": ("⚓",), "якорь": ("⚓",),
    # ── люди / жесты ──
    "ghost": ("👻",), "призрак": ("👻",),
    "alien": ("👽",), "пришелец": ("👽",),
    "robot": ("🤖",), "робот": ("🤖",),
    "devil": ("👿", "😈"), "дьявол": ("😈",),
    "skull": ("💀", "☠", "☠️"), "череп": ("💀",),
    "pumpkin": ("🎃",), "тыква": ("🎃",), "jack": ("🎃",),
    "angel": ("👼", "😇"), "ангел": ("👼",),
    "santa": ("🎅",), "дед_мороз": ("🎅",),
    "princess": ("👸",), "принцесса": ("👸",),
    "prince": ("🤴",), "принц": ("🤴",),
    "king": ("🤴", "👑"), "король": ("🤴",),
    "queen": ("👸", "👑"), "королева": ("👸",),
    "eye": ("👁", "👁️", "👀"), "глаз": ("👁",),
    "ear": ("👂",), "ухо": ("👂",),
    "nose": ("👃",), "нос": ("👃",),
    "mouth": ("👄",), "рот": ("👄",),
    "tongue": ("👅",), "язык": ("👅",),
    "tooth": ("🦷",), "зуб": ("🦷",),
    "hand": ("✋", "🤚", "🖐"), "рука": ("✋", "🖐"),
    "thumbsup": ("👍",), "like": ("👍",),
    "thumbsdown": ("👎",), "dislike": ("👎",),
    "fist": ("✊", "👊"), "кулак": ("✊", "👊"),
    "clap": ("👏",), "хлопок": ("👏",),
    "pray": ("🙏",), "молитва": ("🙏",),
    "muscle": ("💪",), "мышца": ("💪",),
    # ── символы / предметы ──
    "heart": ("❤", "❤️", "💖", "💕", "💗", "💓"), "сердце": ("❤", "❤️"),
    "broken_heart": ("💔",), "разбитое": ("💔",),
    "sparkle": ("✨",), "sparkles": ("✨",),
    "kiss": ("💋", "😘"), "поцелуй": ("💋",),
    "crown": ("👑",), "корона": ("👑",),
    "diamond": ("💎",), "алмаз": ("💎",), "бриллиант": ("💎",), "gem": ("💎",),
    "ring": ("💍",), "кольцо": ("💍",),
    "gift": ("🎁",), "подарок": ("🎁",), "present": ("🎁",),
    "money": ("💰", "💵", "💴", "💶", "💷"), "деньги": ("💰",), "cash": ("💵",),
    "coin": ("🪙",), "монета": ("🪙",),
    "key": ("🔑", "🗝"), "ключ": ("🔑",),
    "lock": ("🔒", "🔐", "🔓"), "замок": ("🔒",),
    "bomb": ("💣",), "бомба": ("💣",),
    "pistol": ("🔫",), "gun": ("🔫",), "пистолет": ("🔫",),
    "sword": ("⚔", "🗡"), "меч": ("⚔",),
    "shield": ("🛡",), "щит": ("🛡",),
    "trophy": ("🏆",), "кубок": ("🏆",),
    "medal": ("🏅", "🎖"), "медаль": ("🏅",),
    "balloon": ("🎈",), "шар": ("🎈",),
    "candle": ("🕯",), "свеча": ("🕯",),
    "bulb": ("💡",), "лампа": ("💡",), "лампочка": ("💡",),
    "lamp": ("💡",),
    "clock": ("⏰", "🕐", "⌚", "🕰"), "часы": ("⏰", "⌚"), "будильник": ("⏰",), "alarm": ("⏰",),
    "hourglass": ("⌛", "⏳"), "песочные": ("⌛",),
    "book": ("📖", "📕", "📗", "📘", "📙", "📚"), "книга": ("📖",),
    "pencil": ("✏", "✏️", "📝"), "карандаш": ("✏",),
    "pen": ("🖊", "🖋"), "ручка": ("🖊",),
    "paper": ("📄", "📃"), "бумага": ("📄",),
    "folder": ("📁", "📂"), "папка": ("📁",),
    "scissors": ("✂", "✂️"), "ножницы": ("✂",),
    "ruler": ("📏",), "линейка": ("📏",),
    "paperclip": ("📎",), "скрепка": ("📎",),
    "magnet": ("🧲",), "магнит": ("🧲",),
    "music": ("🎵", "🎶"), "музыка": ("🎵",), "note": ("🎵",),
    "guitar": ("🎸",), "гитара": ("🎸",),
    "piano": ("🎹",), "пианино": ("🎹",),
    "drum": ("🥁",), "барабан": ("🥁",),
    "trumpet": ("🎺",), "труба": ("🎺",),
    "violin": ("🎻",), "скрипка": ("🎻",),
    "microphone": ("🎤",), "микрофон": ("🎤",), "mic": ("🎤",),
    "headphones": ("🎧",), "наушники": ("🎧",),
    "camera": ("📷", "📸"), "камера": ("📷",), "фотоаппарат": ("📷",),
    "video": ("📹", "🎥"), "видео": ("📹", "🎥"),
    "tv": ("📺",), "телевизор": ("📺",),
    "radio": ("📻",), "радио": ("📻",),
    "phone": ("📱", "☎", "☎️"), "телефон": ("📱",), "смартфон": ("📱",),
    "computer": ("💻", "🖥"), "компьютер": ("💻",), "laptop": ("💻",), "ноутбук": ("💻",),
    "keyboard": ("⌨",), "клавиатура": ("⌨",),
    "mouse_computer": ("🖱",),
    "disk": ("💾", "💿", "📀"), "диск": ("💿", "📀"),
    "mail": ("📧", "✉", "✉️"), "почта": ("📧",), "letter": ("✉",), "письмо": ("✉",),
    "envelope": ("✉",), "конверт": ("✉",),
    "package": ("📦",), "посылка": ("📦",), "коробка": ("📦",), "box": ("📦",),
    "mailbox": ("📬", "📭"), "ящик": ("📬",),
    "bell": ("🔔", "🛎"), "колокол": ("🔔",), "звонок": ("🔔",),
    "speaker": ("🔊", "🔈"), "громкоговоритель": ("🔊",),
    "mute": ("🔇",), "mega": ("📣",),
    "umbrella": ("☂", "☂️", "☔"), "зонт": ("☂", "☔"),
    "briefcase": ("💼",), "портфель": ("💼",),
    "bag": ("👜", "🎒"), "сумка": ("👜",), "backpack": ("🎒",), "рюкзак": ("🎒",),
    "glasses": ("👓", "🕶"), "очки": ("👓",), "sunglasses": ("🕶",),
    "hat": ("🎩", "👒", "🧢"), "шляпа": ("🎩", "👒"), "cap": ("🧢",), "кепка": ("🧢",),
    "shoe": ("👟", "👠", "👢"), "обувь": ("👟",), "ботинок": ("👟",),
    "shirt": ("👕",), "футболка": ("👕",), "tshirt": ("👕",),
    "dress": ("👗",), "платье": ("👗",),
    "jeans": ("👖",), "джинсы": ("👖",),
    "coat": ("🧥",), "пальто": ("🧥",),
    "sock": ("🧦",), "носок": ("🧦",),
    "glove": ("🧤",), "перчатка": ("🧤",),
    "scarf": ("🧣",), "шарф": ("🧣",),
    # ── спорт / игры ──
    "ball": ("⚽", "🏀", "⚾", "🏈", "🎾", "🏐", "🎱"), "мяч": ("⚽",),
    "football": ("🏈", "⚽"), "soccer": ("⚽",), "футбол": ("⚽",),
    "basketball": ("🏀",), "баскетбол": ("🏀",),
    "baseball": ("⚾",), "бейсбол": ("⚾",),
    "tennis": ("🎾",), "теннис": ("🎾",),
    "bowling": ("🎳",), "боулинг": ("🎳",),
    "dart": ("🎯",), "дартс": ("🎯",), "target": ("🎯",), "цель": ("🎯",),
    "dice": ("🎲",), "кубик": ("🎲",),
    "chess": ("♟",), "шахматы": ("♟",),
    "cards": ("🃏", "🎴"),
    "controller": ("🎮",), "геймпад": ("🎮",), "game": ("🎮",), "игра": ("🎮",),
    "puzzle": ("🧩",), "пазл": ("🧩",),
    # ── символы / геометрия ──
    "check": ("✅", "✔"), "галочка": ("✅",),
    "cross": ("❌", "✖"), "крестик": ("❌",),
    "warning": ("⚠", "⚠️"), "внимание": ("⚠",),
    "stop": ("🛑", "✋"), "стоп": ("🛑",),
    "arrow": ("➡", "⬅", "⬆", "⬇"), "стрелка": ("➡",),
    "recycle": ("♻", "♻️"), "перерабтка": ("♻",),
    "infinity": ("♾",), "бесконечность": ("♾",),
    "question": ("❓", "❔"), "вопрос": ("❓",),
    "exclamation": ("❗", "❕"), "восклицание": ("❗",),
    "circle": ("⭕", "🔴", "🟢", "🔵", "🟡", "🟠", "🟣", "⚫", "⚪"),
    "square": ("🟥", "🟩", "🟦", "🟨", "🟧", "🟪", "⬛", "⬜"),
    "triangle": ("🔺", "🔻"),
    # ── знаки зодиака / магия ──
    "crystal": ("🔮",), "хрусталь": ("🔮",), "ball_crystal": ("🔮",),
    "magic": ("🪄",), "магия": ("🪄",),
    "wand": ("🪄",), "палочка": ("🪄",),
    "witch": ("🧙",), "ведьма": ("🧙",),
    "wizard": ("🧙",), "волшебник": ("🧙",),
    "cross_religious": ("✝",), "крест": ("✝",),
    "yin": ("☯",), "yang": ("☯",),
}


def _norm_word(s: str) -> str:
    """Нормализуем OCR-результат: только буквы, нижний регистр, замена похожих.
    Этапы:
    1. Цифры на похожие буквы (OCR часто путает: 0↔o, 1↔l, 5↔s, 2↔z, 8↔b, 6↔g, 4↔a).
    2. Кириллические «близнецы» на латиницу (а→a, о→o, е→e, р→p, с→c, ...) —
       OCR с смешанным whitelist иногда читает 'star' как 'ѕтар' вперемешку.
    3. Остаются только буквы, нижний регистр."""
    s = unicodedata.normalize("NFKD", s or "").lower()
    digit_map = str.maketrans({"0": "o", "1": "l", "2": "z", "3": "e", "4": "a",
                               "5": "s", "6": "g", "7": "t", "8": "b", "9": "g"})
    s = s.translate(digit_map)
    # Кириллица + греческие + small-caps → латиница (визуальные близнецы OCR)
    cyr_map = str.maketrans({
        "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h",
        "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i",
        "ј": "j", "ɡ": "g", "ѕ": "s", "ԛ": "q", "ѵ": "v",
        # греческие буквы, которые tesseract иногда выдаёт даже с латинским whitelist.
        # ВАЖНО: NFKD выше декомпозирует ϲ→ς, поэтому обе формы в карте
        "α": "a", "ο": "o", "ρ": "p", "τ": "t", "κ": "k", "ν": "v", "ɑ": "a",
        "ϲ": "c", "ς": "c", "σ": "s",
        # small-caps (fuzzy OCR на мелком тексте)
        "ʟ": "l", "ʜ": "h", "ᴛ": "t", "ᴄ": "c", "ᴋ": "k", "ɪ": "i", "ᴏ": "o",
        "ᴘ": "p", "ᴀ": "a", "ʙ": "b", "ᴇ": "e", "ɴ": "n",
    })
    s = s.translate(cyr_map)
    s = "".join(ch for ch in s if ch.isalpha())
    return s


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"      # разные символы и пиктограммы, эмодзи
    "\U00002600-\U000027BF"      # разные символы (☀, ✈, ❤…)
    "\U00002B00-\U00002BFF"      # стрелки/формы, ВКЛЮЧАЯ ⭐ U+2B50
    "\U00002700-\U000027BF"      # дингбаты
    "\U0001F000-\U0001F2FF"      # маджонг, домино, enclosed alphanumerics
    "\U0001F600-\U0001F64F"      # emoticons
    "\U0001F680-\U0001F6FF"      # транспорт/символы
    "\U0001F900-\U0001F9FF"      # дополнительные символы
    "]"
    "(?:️|‍[\U0001F300-\U0001FAFF])*"  # VS16 и ZWJ-последовательности
)


def _emojis_in(text: str) -> list[str]:
    """Все эмодзи в тексте кнопки (их может быть несколько)."""
    return _EMOJI_RE.findall(text or "")


def _emoji_name_words(emoji: str) -> set[str]:
    """Слова из unicodedata.name каждого code point эмодзи."""
    words: set[str] = set()
    for ch in emoji:
        try:
            name = unicodedata.name(ch).lower()
        except ValueError:
            continue
        for w in name.replace("-", " ").split():
            if len(w) > 2 and w not in ("with", "and", "face", "sign", "symbol", "mark"):
                words.add(w)
    return words


def _button_vocabulary(buttons: list[str]) -> list[set[str]]:
    """Для каждой кнопки — набор слов, которыми её эмодзи может быть названо
    (из обратного словаря + unicodedata.name)."""
    # обратный словарь: эмодзи → все слова, которые на него маппятся
    rev: dict[str, set[str]] = {}
    for w, emojis in _WORD_TO_EMOJI.items():
        for em in emojis:
            rev.setdefault(em, set()).add(w)
    out: list[set[str]] = []
    for btn in buttons:
        vocab: set[str] = set()
        for em in _emojis_in(btn):
            vocab.update(rev.get(em, set()))
            vocab.update(_emoji_name_words(em))
        out.append(vocab)
    return out


def _lev(a: str, b: str) -> int:
    """Расстояние Левенштейна (O(len(a)*len(b)) — здесь строки короткие, норм)."""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_word_to_button(word: str, buttons: Iterable[str]) -> Optional[int]:
    """Слово (из OCR) → индекс кнопки.

    Логика: собираем словарь слов для каждой кнопки из её эмодзи. OCR часто
    ошибается на букву-две ('star' → 'stan', 'fox' → 'tox'), поэтому берём
    кнопку, у которой есть слово с минимальным расстоянием Левенштейна.
    Если ближайший кандидат слишком далёк — None (отдаём админу).
    """
    nw = _norm_word(word)
    if not nw:
        return None
    btns = list(buttons)
    vocab = _button_vocabulary(btns)

    best_i, best_dist, best_word = None, 999, ""
    for i, words in enumerate(vocab):
        for w in words:
            d = _lev(nw, w)
            # нормализуем на длину слова — для коротких (fox=3) допускаем меньше, для длинных больше
            # близость: расстояние ≤ max(1, 25% длины)
            if d < best_dist:
                best_dist, best_i, best_word = d, i, w
    if best_i is None:
        return None

    # допустимая погрешность: до 1 ошибки на 4 буквы, но минимум 1
    allowed = max(1, len(best_word) // 4)
    # дополнительно: разница с ВТОРЫМ лучшим должна быть ≥2 (иначе неоднозначно → None)
    second = 999
    for i, words in enumerate(vocab):
        if i == best_i:
            continue
        for w in words:
            second = min(second, _lev(nw, w))

    if best_dist <= allowed and (second - best_dist) >= 1:
        log.info("Captcha match: OCR %r → кнопка %d (слово %r, расстояние %d, порог %d, второй=%d)",
                 nw, best_i, best_word, best_dist, allowed, second)
        return best_i
    log.info("Captcha match: OCR %r — лучший %r/%d (порог %d), второй=%d — неоднозначно, пропуск",
             nw, best_word, best_dist, allowed, second)
    return None


# ---------------------------------------------------------------------------
# OCR через tesseract (локально)
# ---------------------------------------------------------------------------
_OCR_MIN_CONFIDENCE = float(_env("CAPTCHA_OCR_MIN_CONF", "20"))   # 0..100
# fuzzy_match сам отсекает мусор (xyz → None), поэтому порог confidence можно держать низким


def _try_import_ocr():
    try:
        import pytesseract
        from PIL import Image, ImageOps, ImageFilter
        return pytesseract, Image, ImageOps, ImageFilter
    except ImportError:
        return None


def _base_image(photo: bytes):
    """Разово открывает и готовит grayscale+autocontrast изображение.
    Возвращает PIL.Image или None (если Pillow/pytesseract не установлены)."""
    mods = _try_import_ocr()
    if mods is None:
        return None
    _, Image, ImageOps, _ = mods
    base = Image.open(io.BytesIO(photo)).convert("L")
    base = ImageOps.autocontrast(base, cutoff=1)
    return base


def _preprocess_from_base(base, threshold: int, scale: int, invert: bool = False,
                          sharpen: bool = False, contrast: float = 1.0, erode: bool = False):
    """Применяет вариант обработки к уже готовому base-изображению."""
    mods = _try_import_ocr()
    if mods is None:
        return None
    from PIL import Image, ImageOps, ImageFilter
    img = base
    if contrast != 1.0:
        from PIL import ImageEnhance
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if sharpen:
        img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda p, t=threshold: 0 if p < t else 255, mode="1").convert("L")
    if invert:
        img = ImageOps.invert(img)
    w, h = img.size
    img = img.resize((w * scale, h * scale), Image.LANCZOS)
    img = img.filter(ImageFilter.MedianFilter(5))
    if erode:
        img = img.filter(ImageFilter.MinFilter(3))
    return img


# Обратная совместимость — используется в тестах/вызовах извне
def _preprocess(photo: bytes, threshold: int, scale: int, invert: bool = False,
                sharpen: bool = False, contrast: float = 1.0, erode: bool = False):
    base = _base_image(photo)
    if base is None:
        return None
    return _preprocess_from_base(base, threshold, scale, invert, sharpen, contrast, erode)


def _ocr_and_match(photo: bytes, buttons: list[str]) -> Optional[int]:
    """OCR + матч к кнопкам с ранним выходом.

    Прогоняем варианты пред-обработки блоками (быстрые сначала). После каждого блока
    пробуем матч. Если нашли уверенный ответ — выходим не прогоняя все 85 попыток.
    Экономит 2-3 секунды на капчу которая читается с первого раза."""
    mods = _try_import_ocr()
    if mods is None or not photo:
        return None
    pytesseract = mods[0]
    # Разрешаем цифры тоже — OCR часто читает 's' как '5', 'o' как '0' на размытых капчах.
    # _norm_word вернёт обратно в буквы через digit→letter mapping.
    whitelist = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                 "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
                 "0123456789")
    hyps: list[tuple[str, float]] = []

    # Блоки вариантов по приоритету (быстрые сначала)
    blocks = [
        # Блок 1: 3 базовых порога × основные psm — покрывает ~90% случаев
        [(115, 6, False, False, 1.0, False),
         (130, 6, False, False, 1.0, False),
         (145, 6, False, False, 1.0, False)],
        # Блок 2: остальные пороги + контраст + sharpen
        [(100, 6, False, False, 1.0, False),
         (160, 6, False, False, 1.0, False),
         (130, 6, False, False, 2.5, False),
         (130, 6, False, True, 1.0, False),
         (115, 6, False, True, 1.5, False)],
        # Блок 3: тяжёлая артиллерия — erode, invert, большой скейл, экстремальный контраст
        [(130, 6, True, False, 1.0, False),
         (130, 6, False, False, 1.0, True),
         (115, 6, False, False, 1.0, True),
         (130, 10, False, False, 1.0, False),
         (130, 6, False, False, 3.5, False),
         (115, 8, False, True, 2.0, True),
         (145, 6, False, True, 1.5, True)],
    ]
    psms = (7, 8, 6, 13, 10)

    try:
        base = _base_image(photo)
    except Exception as e:                                             # noqa: BLE001
        log.warning("OCR: cannot open image: %s", e)
        return None
    if base is None:
        return None
    try:
        for block in blocks:
            for threshold, scale, inv, shrp, ctr, ero in block:
                img = _preprocess_from_base(base, threshold, scale, invert=inv, sharpen=shrp,
                                            contrast=ctr, erode=ero)
                if img is None:
                    continue        # один вариант не отрендерился — пробуем остальные
                for psm in psms:
                    cfg = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={whitelist}"
                    try:
                        data = pytesseract.image_to_data(img, config=cfg,
                                                         output_type=pytesseract.Output.DICT)
                    except Exception:
                        continue
                    for i, raw in enumerate(data.get("text", [])):
                        word = _norm_word(raw or "")
                        if not word or not (2 <= len(word) <= 32):
                            continue
                        try:
                            conf = float(data["conf"][i])
                        except (ValueError, TypeError, IndexError):
                            continue
                        if conf < 0:
                            continue
                        hyps.append((word, conf))
            # пробуем матч после каждого блока
            idx = _best_button_from_hypotheses(hyps, buttons)
            if idx is not None:
                return idx
    except Exception as e:                                             # noqa: BLE001
        log.warning("OCR failed: %s", e)
    log.info("Captcha solver: ни один из %d гипотез (%d вариантов) не подошёл",
             len(hyps), sum(len(b) for b in blocks))
    return None


def ocr_words(photo: bytes) -> list[tuple[str, float]]:
    """Собираем ВСЕ гипотезы OCR в несколько проходов с разной предобработкой.
    Нужно по максимуму — чтобы хоть одна гипотеза совпала с кнопкой."""
    mods = _try_import_ocr()
    if mods is None or not photo:
        return []
    pytesseract = mods[0]
    whitelist = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                 "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
                 "0123456789")
    hyps: list[tuple[str, float]] = []

    # Варианты предобработки: (threshold, scale, invert, sharpen, contrast, erode)
    variants = [
        # базовые 5 порогов
        (100, 6, False, False, 1.0, False),
        (115, 6, False, False, 1.0, False),
        (130, 6, False, False, 1.0, False),
        (145, 6, False, False, 1.0, False),
        (160, 6, False, False, 1.0, False),
        # разные контрасты
        (130, 6, False, False, 1.8, False),
        (130, 6, False, False, 2.5, False),
        (130, 6, False, False, 3.5, False),
        # sharpen — для размытых
        (130, 6, False, True, 1.0, False),
        (115, 6, False, True, 1.5, False),
        # инверсия — на случай светлый шрифт на тёмном фоне
        (130, 6, True, False, 1.0, False),
        # эрозия — утончает буквы, помогает на очень размытых где они сливаются
        (130, 6, False, False, 1.0, True),
        (115, 6, False, False, 1.0, True),
        (130, 6, False, False, 2.0, True),
        # большой скейл — иногда помогает
        (130, 8, False, False, 1.0, False),
        (130, 10, False, False, 1.0, False),
        (115, 8, False, False, 2.0, False),
    ]
    psms = (7, 8, 6, 13, 10)       # +10 — single char (на случай 1 буквы)

    # base_image открываем ОДИН раз на все 17 вариантов — не 17 раз.
    # _preprocess раньше дёргал _base_image внутри каждого цикла — экономим ~0.5-1с/капчу.
    try:
        base = _base_image(photo)
    except Exception as e:                                             # noqa: BLE001
        log.warning("OCR: cannot open image: %s", e)
        return []
    if base is None:
        return []
    try:
        for threshold, scale, inv, shrp, ctr, ero in variants:
            img = _preprocess_from_base(base, threshold, scale, invert=inv, sharpen=shrp,
                                        contrast=ctr, erode=ero)
            if img is None:
                continue        # один вариант не удался — пробуем остальные
            for psm in psms:
                cfg = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={whitelist}"
                try:
                    data = pytesseract.image_to_data(img, config=cfg,
                                                     output_type=pytesseract.Output.DICT)
                except Exception:
                    continue
                for i, raw in enumerate(data.get("text", [])):
                    word = _norm_word(raw or "")
                    if not word or not (2 <= len(word) <= 32):
                        continue
                    try:
                        conf = float(data["conf"][i])
                    except (ValueError, TypeError, IndexError):
                        continue
                    if conf < 0:
                        continue
                    hyps.append((word, conf))
    except Exception as e:                                             # noqa: BLE001
        log.warning("OCR failed: %s", e)
    return hyps


def ocr_word(photo: bytes) -> Optional[tuple[str, float]]:
    """Обратная совместимость: ТОП-гипотеза (слово с макс. confidence)."""
    hyps = ocr_words(photo)
    if not hyps:
        return None
    best = max(hyps, key=lambda x: x[1])
    log.info("OCR → %r (conf %.0f%%, hypotheses=%d)", best[0], best[1], len(hyps))
    return best


def _best_button_from_hypotheses(hyps: list[tuple[str, float]],
                                 buttons: list[str]) -> Optional[int]:
    """Многослойный матчер по 20 гипотезам OCR → индекс кнопки.

    Пробуем по убыванию строгости:
      1. Levenshtein: нашлась гипотеза с распределением ≤ порога к слову кнопки.
      2. Первые 2 буквы: гипотеза и слово кнопки совпадают по 2-символьному префиксу.
      3. Первая буква + голосование: кнопка побеждает, если больше всех гипотез
         начинается с её первой буквы (только когда её первая буква уникальна).
      4. Хоть одна гипотеза дословно содержится в слове кнопки или наоборот.

    Так ловим даже сильно размытые капчи: 'sf','she','sta' среди 7 кнопок
    [star,fox,dog,robot,bear,rabbit,ghost] голосуют за 'star' (единственное на 's')."""
    if not hyps or not buttons:
        return None
    vocab = _button_vocabulary(buttons)
    hyp_words = [w for w, _ in hyps]
    # отдельно для первых букв работаем только с латиницей
    hyp_words_ascii = [w for w in hyp_words if w and w[0].isascii() and w[0].isalpha()]

    # ── 1) Полный fuzzy Levenshtein ──
    best_i, best_dist, best_pair = None, 999, ("", "")
    for i, words in enumerate(vocab):
        for word in hyp_words:
            for w in words:
                d = _lev(word, w)
                if d < best_dist:
                    best_dist, best_i, best_pair = d, i, (word, w)
    if best_i is not None:
        allowed = max(1, len(best_pair[1]) // 3)
        second = 999
        for i, words in enumerate(vocab):
            if i == best_i:
                continue
            for word in hyp_words:
                for w in words:
                    second = min(second, _lev(word, w))
        if best_dist <= allowed and (second - best_dist) >= 1:
            log.info("Captcha vote[lev]: → кнопка %d (OCR %r → %r, dist %d/%d, second=%d)",
                     best_i, best_pair[0], best_pair[1], best_dist, allowed, second)
            _LAYER_HITS["lev"] = _LAYER_HITS.get("lev", 0) + 1
            return best_i

    # ── 2) Префикс в 2 символа ──
    prefix_votes: dict[int, int] = {}
    for i, words in enumerate(vocab):
        prefixes = {w[:2] for w in words if len(w) >= 2}
        for word in hyp_words_ascii:
            if len(word) >= 2 and word[:2] in prefixes:
                prefix_votes[i] = prefix_votes.get(i, 0) + 1
    if prefix_votes:
        best_i = max(prefix_votes, key=lambda k: prefix_votes[k])
        best_votes = prefix_votes[best_i]
        second_votes = max((v for k, v in prefix_votes.items() if k != best_i), default=0)
        if best_votes >= 2 and best_votes > second_votes:
            log.info("Captcha vote[prefix2]: → кнопка %d (голосов %d/%d)",
                     best_i, best_votes, second_votes)
            _LAYER_HITS["prefix2"] = _LAYER_HITS.get("prefix2", 0) + 1
            return best_i

    # ── 3) Первая буква + голосование ──
    first_letter_votes: dict[int, int] = {}
    button_first_letters: dict[int, set[str]] = {}
    for i, words in enumerate(vocab):
        button_first_letters[i] = {w[0] for w in words if w and w[0].isascii()}
    # собираем какие буквы вообще претендуют — чтобы выявить уникальные
    all_first_letters = [l for ls in button_first_letters.values() for l in ls]
    for i, letters in button_first_letters.items():
        for word in hyp_words_ascii:
            if word[0] in letters:
                first_letter_votes[i] = first_letter_votes.get(i, 0) + 1
    if first_letter_votes:
        best_i = max(first_letter_votes, key=lambda k: first_letter_votes[k])
        best_votes = first_letter_votes[best_i]
        second_votes = max((v for k, v in first_letter_votes.items() if k != best_i), default=0)
        # уникальность: буквы этой кнопки встречаются ТОЛЬКО у неё
        unique = all(all_first_letters.count(l) == 1 for l in button_first_letters[best_i])
        # Условия (мягко): 1+ голосов с уникальной буквой ИЛИ 2+ с отрывом ≥2
        if (best_votes >= 1 and unique and best_votes > second_votes) or \
           (best_votes >= 2 and best_votes >= second_votes + 2):
            log.info("Captcha vote[first-letter]: → кнопка %d (голосов %d, уникально=%s, второй=%d)",
                     best_i, best_votes, unique, second_votes)
            _LAYER_HITS["first-letter"] = _LAYER_HITS.get("first-letter", 0) + 1
            return best_i

    # ── 4) Подстрока: гипотеза ⊂ слово кнопки или наоборот ──
    sub_votes: dict[int, int] = {}
    for i, words in enumerate(vocab):
        for word in hyp_words_ascii:
            if len(word) < 2:
                continue
            for w in words:
                if word in w or (len(word) >= 3 and w in word):
                    sub_votes[i] = sub_votes.get(i, 0) + 1
                    break
    if sub_votes:
        best_i = max(sub_votes, key=lambda k: sub_votes[k])
        best_votes = sub_votes[best_i]
        second_votes = max((v for k, v in sub_votes.items() if k != best_i), default=0)
        if best_votes > second_votes:
            log.info("Captcha vote[substring]: → кнопка %d (голосов %d/%d)",
                     best_i, best_votes, second_votes)
            _LAYER_HITS["substring"] = _LAYER_HITS.get("substring", 0) + 1
            return best_i

    log.info("Captcha vote: %d гипотез — ни один из 4 слоёв не дал ответа, пропуск", len(hyps))
    return None


def estimate_letter_count(photo: bytes) -> int:
    """Оценить сколько букв на картинке по плотности тёмных пикселей.
    Эмпирика на капчах funstat 120×48: ~30 пикселей чернил на букву."""
    mods = _try_import_ocr()
    if mods is None or not photo:
        return 0
    _, Image, ImageOps, _ = mods
    try:
        img = Image.open(io.BytesIO(photo)).convert("L")
        img = ImageOps.autocontrast(img, cutoff=1)
        img = img.point(lambda p: 1 if p < 130 else 0, mode="1")
        try:
            ink = sum(img.get_flattened_data())  # Pillow 14+
        except AttributeError:
            ink = sum(1 for p in img.getdata() if p)
        # эмпирика: fox(3)=117, star(4)=111, robot(5)=161, bear(4)=132,
        #           rabbit(6)=225, dog(3)=131, ghost(5)=291. ~30px per letter.
        return max(2, min(10, round(ink / 30)))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Vision API (опциональный fallback)
# ---------------------------------------------------------------------------
class _VisionAPI:
    def __init__(self) -> None:
        self.api_key = _env("CAPTCHA_API_KEY")
        self.api_base = _env("CAPTCHA_API_BASE", "https://api.openai.com/v1").rstrip("/")
        self.model = _env("CAPTCHA_MODEL", "gpt-4o-mini")
        self.timeout = max(5.0, float(_env("CAPTCHA_TIMEOUT", "25")))
        self.retries = max(1, int(_env("CAPTCHA_MAX_RETRIES", "2")))
        self.enabled = bool(self.api_key)

    def ask_word(self, photo: bytes) -> Optional[str]:
        """Спрашиваем модель: какое слово на картинке. Возвращаем нормализованное."""
        if not self.enabled:
            return None
        b64 = base64.b64encode(photo).decode()
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 10,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": "What single word is written on this captcha image? "
                             "Reply with ONLY the word, no punctuation, no explanation."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        }
        data = json.dumps(body).encode()
        req = _urlreq.Request(
            f"{self.api_base}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/",
                "X-Title": "captcha-solver",
            },
            method="POST",
        )
        try:
            with _urlreq.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            raw = payload["choices"][0]["message"]["content"].strip()
            return _norm_word(raw) or None
        except Exception as e:
            # ЛЮБАЯ ошибка (сеть, таймаут сокета, JSON, индекс, что угодно) — просто откат на админа.
            # Иначе исключение летит из потока в solve(), и капча не решается НИКАК.
            log.warning("Vision API failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Главный класс
# ---------------------------------------------------------------------------
class CaptchaSolver:
    """Пробуем OCR → матч к кнопке. Если не вышло — vision API (если ключ есть)."""

    def __init__(self) -> None:
        self.enabled = _bool("CAPTCHA_AUTO_SOLVE", True)
        self._vision = _VisionAPI()
        # заранее сообщим, работает ли OCR
        self._ocr_available = _try_import_ocr() is not None
        if self.enabled and not self._ocr_available and not self._vision.enabled:
            log.warning("Captcha solver: ни OCR (pytesseract), ни vision API не доступны — "
                        "капчи будут уходить админу")

    async def solve(self, photo: Optional[bytes], buttons: Iterable[str]) -> Optional[int]:
        """Вернуть индекс кнопки или None."""
        idxs = await self.solve_ranked(photo, buttons)
        return idxs[0] if idxs else None

    async def solve_ranked(self, photo: Optional[bytes], buttons: Iterable[str]) -> list[int]:
        """Вернуть ранжированный список индексов кнопок (лучший первым).
        Используется для retry: если первый клик funstat назвал неверным,
        можно попробовать второй-третий без прохода через админа."""
        if not self.enabled or not photo:
            return []
        btns = list(buttons)
        if not btns:
            return []

        _load_cache()
        # 0) Кеш: та же картинка + те же кнопки → мгновенно отвечаем, без OCR (0 сек)
        ck = _cache_key(photo, btns)
        cached = _CAPTCHA_CACHE.get(ck)
        if cached is not None and 0 <= cached < len(btns):
            log.info("Captcha solver: кеш попал → кнопка %d (%s)", cached, btns[cached])
            # Истинный LRU: при попадании перемещаем ключ в конец — чтобы часто
            # используемые капчи не выпадали при FIFO-очистке cache_put.
            try:
                _CAPTCHA_CACHE[ck] = _CAPTCHA_CACHE.pop(ck)
            except KeyError:
                pass
            return [cached]

        results: list[int] = []

        # 1) OCR с early-exit: после первых базовых вариантов пробуем матч; если нашли — выходим.
        # ВАЖНО: оборачиваем в wait_for — tesseract на сломанной картинке
        # может зависнуть на минуты, блокируя всю очередь lookup.
        if self._ocr_available:
            try:
                idx = await asyncio.wait_for(
                    asyncio.to_thread(_ocr_and_match, photo, btns), timeout=30
                )
            except asyncio.TimeoutError:
                log.warning("Captcha solver: OCR timeout на 30s — пропускаем")
                idx = None
            except Exception as e:            # noqa: BLE001
                log.warning("Captcha solver: OCR упал: %s", e)
                idx = None
            if idx is not None:
                cache_put(ck, idx)
                results.append(idx)
                # дополнительно — лучшие OCR слова (по confidence), сматченные к кнопкам
                try:
                    hyps = await asyncio.wait_for(
                        asyncio.to_thread(ocr_words, photo), timeout=30
                    )
                    hyps.sort(key=lambda x: -x[1])          # сначала гипотезы с высокой confidence
                    for w, _ in hyps[:12]:
                        alt = match_word_to_button(w, btns)
                        if alt is not None and alt != idx and alt not in results:
                            results.append(alt)
                            if len(results) >= 3:
                                break
                except Exception:
                    pass
                return results

        # 1b) OCR не дал однозначного ответа, но собрал гипотезы — вытащим топ слабых матчей.
        # Это оправданная трата токенов retry: пусть funstat подтвердит или отвергнет,
        # а не отправляем сразу админу.
        if self._ocr_available:
            try:
                hyps = await asyncio.wait_for(
                    asyncio.to_thread(ocr_words, photo), timeout=30
                )
                hyps.sort(key=lambda x: -x[1])
                for w, _ in hyps[:12]:
                    alt = match_word_to_button(w, btns)
                    if alt is not None and alt not in results:
                        results.append(alt)
                        if len(results) >= 2:
                            break
            except Exception:
                pass

        # 2) vision API — защитимся от любых исключений, иначе капча не решается НИЧЕМ
        if self._vision.enabled:
            try:
                word = await asyncio.wait_for(
                    asyncio.to_thread(self._vision.ask_word, photo),
                    timeout=self._vision.timeout + 5,
                )
            except asyncio.TimeoutError:
                log.warning("Captcha solver: vision API timeout (%ds)", self._vision.timeout + 5)
                word = None
            except Exception as e:
                log.warning("Captcha solver: vision API упал: %s", e)
                word = None
            if word:
                idx = match_word_to_button(word, btns)
                if idx is not None:
                    log.info("Captcha solver: vision → %r → кнопка %d (%s)", word, idx, btns[idx])
                    # vision — первая, OCR-слабый — потом (на retry)
                    if idx not in results:
                        results.insert(0, idx)
                    else:
                        results.remove(idx)
                        results.insert(0, idx)
                    return results
                log.info("Captcha solver: vision вернул %r, но ни одна кнопка не подошла", word)

        return results
