"""Авто-решатель капчи funstat / fun_xsearchx_bot и аналогов.

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
    """Нормализуем OCR-результат: только буквы, нижний регистр, замена похожих."""
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(ch for ch in s if ch.isalpha())
    # tesseract часто путает: 0↔o, 1↔l, 5↔s — но мы уже отфильтровали цифры.
    # Иногда S → 5, O → 0, I → 1 — остались как есть, отфильтровались. OK.
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
def _try_import_ocr():
    try:
        import pytesseract
        from PIL import Image, ImageOps
        return pytesseract, Image, ImageOps
    except ImportError:
        return None


_OCR_MIN_CONFIDENCE = float(_env("CAPTCHA_OCR_MIN_CONF", "70"))   # 0..100, ниже — не доверяем


def ocr_word(photo: bytes) -> Optional[tuple[str, float]]:
    """Распознаём слово + уверенность (0..100). None если ничего разборчивого."""
    mods = _try_import_ocr()
    if mods is None or not photo:
        return None
    pytesseract, Image, ImageOps = mods
    try:
        img = Image.open(io.BytesIO(photo))
        img = img.convert("L")
        img = ImageOps.autocontrast(img, cutoff=5)
        # OCR любит крупный шрифт — всегда увеличиваем
        if img.width < 600:
            scale = 600 / img.width
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        best: Optional[tuple[str, float]] = None
        # psm 7/8 — одиночные слова; 6 — абзац; 13 — raw line
        for psm in (8, 7, 6, 13):
            cfg = (f"--oem 3 --psm {psm} -c tessedit_char_whitelist="
                   "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                   "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")
            try:
                data = pytesseract.image_to_data(img, config=cfg, output_type=pytesseract.Output.DICT)
            except Exception:
                continue
            # собираем все слова с confidence и берём лучшее
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
                if best is None or conf > best[1]:
                    best = (word, conf)
        if best:
            log.info("OCR → %r (conf %.0f%%)", best[0], best[1])
            return best
    except Exception as e:                                             # noqa: BLE001
        log.warning("OCR failed: %s", e)
    return None


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
        except (HTTPError, URLError, KeyError, json.JSONDecodeError, ValueError) as e:
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
        if not self.enabled or not photo:
            return None
        btns = list(buttons)
        if not btns:
            return None

        # 1) OCR локально — нужна уверенность не ниже порога, ИНАЧЕ нельзя гадать
        #    (неверный клик → funstat даёт новую капчу и зацикливается)
        if self._ocr_available:
            res = await asyncio.to_thread(ocr_word, photo)
            if res:
                word, conf = res
                idx = match_word_to_button(word, btns)
                if idx is None:
                    log.info("Captcha solver: OCR %r (conf %.0f%%) — ни одна кнопка не подошла", word, conf)
                elif conf < _OCR_MIN_CONFIDENCE:
                    log.info("Captcha solver: OCR %r (conf %.0f%% < %.0f%%) — не рискуем, передаю vision/админу",
                             word, conf, _OCR_MIN_CONFIDENCE)
                else:
                    log.info("Captcha solver: OCR → %r (conf %.0f%%) → кнопка %d (%s)",
                             word, conf, idx, btns[idx])
                    return idx

        # 2) vision API
        if self._vision.enabled:
            word = await asyncio.wait_for(
                asyncio.to_thread(self._vision.ask_word, photo),
                timeout=self._vision.timeout + 5,
            )
            if word:
                idx = match_word_to_button(word, btns)
                if idx is not None:
                    log.info("Captcha solver: vision → %r → кнопка %d (%s)", word, idx, btns[idx])
                    return idx
                log.info("Captcha solver: vision вернул %r, но ни одна кнопка не подошла", word)

        return None
