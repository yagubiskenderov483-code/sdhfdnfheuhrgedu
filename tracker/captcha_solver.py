"""Авто-решатель капчи бота поиска (funstat и подобные).

Капча: картинка с эмодзи + 2–8 кнопок, на каждой — эмодзи. Надо нажать ту,
чьё эмодзи на картинке. Решаем через OpenAI-совместимый vision-эндпоинт.

Настройки читаем прямо из окружения, чтобы модуль был самодостаточным:
  CAPTCHA_AUTO_SOLVE   = "1" / "true" / "yes" — включить (по умолчанию: вкл, если задан ключ)
  CAPTCHA_API_KEY      — ключ (OpenAI / OpenRouter / любой совместимый)
  CAPTCHA_API_BASE     — базовый URL, по умолчанию "https://api.openai.com/v1"
  CAPTCHA_MODEL        — модель, по умолчанию "gpt-4o-mini"
  CAPTCHA_TIMEOUT      — секунды, по умолчанию 25
  CAPTCHA_MAX_RETRIES  — попыток, по умолчанию 2

API_BASE для популярных провайдеров:
  OpenAI       : https://api.openai.com/v1              (модель: gpt-4o-mini / gpt-4o)
  OpenRouter   : https://openrouter.ai/api/v1           (модель: openai/gpt-4o-mini, google/gemini-2.0-flash-exp:free, …)
  Groq         : https://api.groq.com/openai/v1         (модель: llama-3.2-11b-vision-preview)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
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


class CaptchaSolver:
    """Решает «выбери кнопку с таким же эмодзи, как на картинке».

    Использование:
        s = CaptchaSolver()
        if s.enabled:
            idx = await s.solve(photo_bytes, ["🦊", "🐰", "🐶"])
            if idx is not None:
                ...нажимаем кнопку idx...
    """

    def __init__(self) -> None:
        self.api_key = _env("CAPTCHA_API_KEY")
        self.api_base = _env("CAPTCHA_API_BASE", "https://api.openai.com/v1").rstrip("/")
        self.model = _env("CAPTCHA_MODEL", "gpt-4o-mini")
        self.timeout = max(5.0, float(_env("CAPTCHA_TIMEOUT", "25")))
        self.retries = max(1, int(_env("CAPTCHA_MAX_RETRIES", "2")))
        # по умолчанию включено, если задан ключ
        self.enabled = _bool("CAPTCHA_AUTO_SOLVE", bool(self.api_key)) and bool(self.api_key)

    # ------------------------------------------------------------------ public
    async def solve(self, photo: Optional[bytes], buttons: Iterable[str]) -> Optional[int]:
        """Вернуть индекс кнопки, которую надо нажать, или None если не вышло."""
        btns = list(buttons)
        if not self.enabled or not photo or not btns:
            return None
        # отфильтруем «пустые» кнопки (без эмодзи вообще — например «Обновить»):
        # на них мы точно не хотим попасть по ошибке
        emoji_idx = [i for i, t in enumerate(btns) if _looks_emoji(t)]
        if not emoji_idx:
            return None

        last_err: Optional[str] = None
        for attempt in range(1, self.retries + 1):
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(self._ask, photo, btns),
                    timeout=self.timeout,
                )
                idx = _parse_answer(raw, btns)
                if idx is not None and idx in emoji_idx:
                    log.info("Captcha solver: ответ %r → индекс %d (%s)", raw, idx, btns[idx])
                    return idx
                last_err = f"не распарсил ответ: {raw!r}"
            except asyncio.TimeoutError:
                last_err = f"timeout {self.timeout:.0f}s"
            except Exception as e:                                     # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
            log.warning("Captcha solver: попытка %d/%d — %s", attempt, self.retries, last_err)
        return None

    # ------------------------------------------------------------------ http
    def _ask(self, photo: bytes, buttons: list[str]) -> str:
        """Синхронный запрос к OpenAI-совместимому /chat/completions."""
        b64 = base64.b64encode(photo).decode()
        letters = [chr(ord("A") + i) for i in range(len(buttons))]
        choices = "\n".join(f"{letters[i]}) {buttons[i]!r}" for i in range(len(buttons)))
        prompt = (
            "You are solving a captcha. The image shows ONE emoji (sometimes with "
            "noise, lines, rotation, or distractor shapes). Below are the available "
            "buttons, each labelled with an emoji. Decide which button has the SAME "
            "emoji as the one in the image.\n\n"
            f"Buttons:\n{choices}\n\n"
            "Reply with ONLY the single letter of the correct button (e.g. \"B\"). "
            "No explanation, no punctuation."
        )
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 4,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
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
                # OpenRouter требует эти два заголовка для бесплатных моделей:
                "HTTP-Referer": "https://github.com/",
                "X-Title": "captcha-solver",
            },
            method="POST",
        )
        try:
            with _urlreq.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
        except HTTPError as e:
            body_txt = e.read().decode("utf-8", "replace")[:400] if hasattr(e, "read") else ""
            raise RuntimeError(f"HTTP {e.code}: {body_txt}") from None
        except URLError as e:
            raise RuntimeError(f"network: {e.reason}") from None

        try:
            return payload["choices"][0]["message"]["content"].strip()
        except Exception as e:                                         # noqa: BLE001
            raise RuntimeError(f"bad response: {payload!r:.300}") from e


# ---------------------------------------------------------------------- helpers
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F2FF"
    "]"
)


def _looks_emoji(t: str) -> bool:
    t = (t or "").strip()
    if not t:
        return False
    # либо есть эмодзи, либо чистые не-буквы длиной до 6 символов (как в is_captcha)
    if _EMOJI_RE.search(t):
        return True
    return 0 < len(t) <= 6 and not any(ch.isalnum() for ch in t)


def _parse_answer(raw: str, buttons: list[str]) -> Optional[int]:
    """Понимаем ответ модели: 'B', 'B)', '2', 'the second one', '🐶', …"""
    if not raw:
        return None
    s = raw.strip().strip("`'\" .,:;!?()[]{}")
    # 1) буква A/B/C/…
    m = re.match(r"^([A-Za-z])\b", s)
    if m:
        idx = ord(m.group(1).upper()) - ord("A")
        if 0 <= idx < len(buttons):
            return idx
    # 2) номер 1..N
    m = re.match(r"^(\d{1,2})\b", s)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(buttons):
            return idx
    # 3) само эмодзи — ищем в тексте какой-нибудь кнопки
    for i, b in enumerate(buttons):
        if b and (b in raw or raw in b):
            return i
    # 4) «first/second/third»
    words = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
             "sixth": 5, "seventh": 6, "eighth": 7,
             "первая": 0, "вторая": 1, "третья": 2, "четвёртая": 3, "четвертая": 3,
             "пятая": 4, "шестая": 5, "седьмая": 6, "восьмая": 7}
    low = s.lower()
    for w, idx in words.items():
        if w in low and idx < len(buttons):
            return idx
    return None
