"""Ники, написанные «шрифтами»: 𝗠𝗜𝗡𝗥𝗜, 𝑀𝒾𝓀𝓊, Ｍｉｋｕ, ⓜⓘⓚⓤ, ᴀʟᴇx, «размытые» (zalgo: значки над/под буквами),
с эмодзи и украшениями вокруг. Приводим к обычным буквам, чтобы
  • фильтры (короткое имя, русское/чужое) смотрели на реальные буквы;
  • бот поиска (funstat) искал по нормальному имени, если по «шрифтовому» ничего не нашлось.
Кириллица остаётся кириллицей (в латиницу не переводим — иначе сломается проверка на русский).
"""
from __future__ import annotations

import re
import unicodedata

# малые капители и «модифицированные» латинские буквы, которые NFKC не раскладывает
_SMALL_CAPS = str.maketrans({
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j",
    "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ǫ": "q", "ʀ": "r", "ꜱ": "s", "ᴛ": "t",
    "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z", "ɑ": "a", "ɡ": "g", "ɩ": "i", "ł": "l",
})
# невидимые символы и «склейки», которыми украшают ники
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁯﻿­͏ᅟᅠㅤﾠ]")
_SPACES = re.compile(r"\s+")


def plain_name(text: str | None) -> str:
    """Имя «как читается»: шрифтовые буквы → обычные, без zalgo-значков и невидимых символов.
    Эмодзи/украшения остаются (для фильтров они не мешают — считаются не-буквами)."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)          # 𝗠→M, Ｍ→M, ⓜ→m, ᵃ→a
    t = _INVISIBLE.sub("", t)
    # zalgo: после NFKC «нормальные» буквы с диакритикой (й, ё, ö) уже собраны в один символ,
    # поэтому оставшиеся отдельные значки сверху/снизу — мусор, убираем
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Mn", "Me"))
    t = t.translate(_SMALL_CAPS)
    return _SPACES.sub(" ", t).strip()


def _real_letter(ch: str) -> bool:
    """Буква латиницы / кириллицы / греческого (украшения вроде 𖥔 ꧁ 彡 — не буквы имени)."""
    if not ch.isalpha():
        return False
    try:
        n = unicodedata.name(ch)
    except ValueError:
        return False
    return n.startswith(("LATIN", "CYRILLIC", "GREEK"))


def letter_count(text: str | None) -> int:
    """Сколько в имени настоящих букв (после приведения шрифтов к обычным)."""
    return sum(1 for ch in plain_name(text) if _real_letter(ch))


def search_name(text: str | None) -> str:
    """Имя для поиска в funstat: только настоящие буквы, цифры и пробелы (без эмодзи, рамок, украшений)."""
    t = plain_name(text)
    t = "".join(ch if (_real_letter(ch) or ch.isdigit() or ch in " ._-'") else " " for ch in t)
    return _SPACES.sub(" ", t).strip(" ._-'")
