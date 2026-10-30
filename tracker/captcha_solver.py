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
    # животные
    "fox": ("🦊",), "лиса": ("🦊",),
    "dog": ("🐶", "🐕", "🐩"), "puppy": ("🐶",), "собака": ("🐶", "🐕"), "пёс": ("🐶",), "пес": ("🐶",),
    "cat": ("🐱", "🐈"), "kitten": ("🐱",), "кот": ("🐱", "🐈"), "кошка": ("🐱",),
    "rabbit": ("🐰", "🐇"), "bunny": ("🐰",), "кролик": ("🐰", "🐇"), "заяц": ("🐰",),
    "bear": ("🐻",), "медведь": ("🐻",),
    "panda": ("🐼",), "панда": ("🐼",),
    "lion": ("🦁",), "лев": ("🦁",),
    "tiger": ("🐯", "🐅"), "тигр": ("🐯",),
    "pig": ("🐷", "🐖"), "свинья": ("🐷",),
    "cow": ("🐮", "🐄"), "корова": ("🐮",),
    "horse": ("🐴", "🐎"), "лошадь": ("🐴",), "конь": ("🐴",),
    "monkey": ("🐵", "🐒"), "обезьяна": ("🐵",),
    "mouse": ("🐭", "🐁"), "мышь": ("🐭",), "мышка": ("🐭",),
    "frog": ("🐸",), "лягушка": ("🐸",),
    "koala": ("🐨",), "коала": ("🐨",),
    "wolf": ("🐺",), "волк": ("🐺",),
    "chicken": ("🐔",), "курица": ("🐔",),
    "bird": ("🐦",), "птица": ("🐦",),
    "penguin": ("🐧",), "пингвин": ("🐧",),
    "unicorn": ("🦄",), "единорог": ("🦄",),
    # предметы / символы
    "star": ("⭐", "🌟", "✨"), "звезда": ("⭐", "🌟"),
    "heart": ("❤", "❤️", "💖", "💕"), "сердце": ("❤", "❤️"),
    "fire": ("🔥",), "огонь": ("🔥",),
    "sun": ("☀", "☀️", "🌞"), "солнце": ("☀", "☀️"),
    "moon": ("🌙", "🌚", "🌝"), "луна": ("🌙",),
    "cloud": ("☁", "☁️"), "облако": ("☁", "☁️"),
    "rain": ("🌧", "🌧️"), "дождь": ("🌧",),
    "snow": ("❄", "❄️", "☃"), "снег": ("❄",),
    "water": ("💧",), "вода": ("💧",),
    "lightning": ("⚡",), "молния": ("⚡",),
    "flower": ("🌸", "🌼", "🌺", "🌻"), "цветок": ("🌸", "🌼"),
    "tree": ("🌳", "🌲", "🌴"), "дерево": ("🌳",),
    "apple": ("🍎", "🍏"), "яблоко": ("🍎",),
    "banana": ("🍌",), "банан": ("🍌",),
    "cherry": ("🍒",), "вишня": ("🍒",),
    "pizza": ("🍕",), "пицца": ("🍕",),
    "cake": ("🎂", "🍰"), "торт": ("🎂", "🍰"),
    "coffee": ("☕",), "кофе": ("☕",),
    "car": ("🚗", "🏎"), "машина": ("🚗",),
    "plane": ("✈", "✈️", "🛩"), "самолёт": ("✈", "✈️"), "самолет": ("✈",),
    "rocket": ("🚀",), "ракета": ("🚀",),
    "ball": ("⚽", "🏀", "⚾"), "мяч": ("⚽",),
    "money": ("💰", "💵"), "деньги": ("💰",),
    "gift": ("🎁",), "подарок": ("🎁",),
    "key": ("🔑", "🗝"), "ключ": ("🔑",),
    "lock": ("🔒", "🔐"), "замок": ("🔒",),
    "bomb": ("💣",), "бомба": ("💣",),
    "crown": ("👑",), "корона": ("👑",),
    "diamond": ("💎",), "алмаз": ("💎",), "бриллиант": ("💎",),
    "ghost": ("👻",), "призрак": ("👻",),
    "alien": ("👽",), "пришелец": ("👽",),
    "robot": ("🤖",), "робот": ("🤖",),
    "skull": ("💀", "☠", "☠️"), "череп": ("💀",),
    "eye": ("👁", "👁️", "👀"), "глаз": ("👁",),
    "hand": ("✋", "🤚"), "рука": ("✋",),
    "thumb": ("👍", "👎"),
    "clock": ("⏰", "🕐", "⌚"), "часы": ("⏰", "⌚"),
    "book": ("📖", "📕", "📗", "📘"), "книга": ("📖",),
    "music": ("🎵", "🎶"), "музыка": ("🎵",),
    "camera": ("📷", "📸"), "камера": ("📷",),
    "phone": ("📱", "☎", "☎️"), "телефон": ("📱",),
    "mail": ("📧", "✉", "✉️"), "почта": ("📧",),
    "bell": ("🔔",), "колокол": ("🔔",),
    "umbrella": ("☂", "☂️", "☔"), "зонт": ("☂", "☔"),
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


def match_word_to_button(word: str, buttons: Iterable[str]) -> Optional[int]:
    """Главная логика: слово (из OCR) → индекс кнопки с подходящим эмодзи."""
    nw = _norm_word(word)
    if not nw:
        return None
    btns = list(buttons)

    # 1) словарь: слово → эмодзи, ищем кнопку с таким эмодзи
    dict_hits = set(_WORD_TO_EMOJI.get(nw, ()))
    # дополнительные формы: убрать окончания на -s, -es
    if not dict_hits and nw.endswith("s"):
        dict_hits = set(_WORD_TO_EMOJI.get(nw[:-1], ()))

    for i, btn in enumerate(btns):
        for em in _emojis_in(btn):
            if em in dict_hits:
                return i

    # 2) unicode name кнопки содержит слово (star → "WHITE MEDIUM STAR")
    for i, btn in enumerate(btns):
        for em in _emojis_in(btn):
            words = _emoji_name_words(em)
            if nw in words:
                return i
            # усечённые формы: dog ⊂ dogface, star ⊂ starfish
            for w in words:
                if nw in w or w in nw:
                    return i
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
