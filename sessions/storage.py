"""
Хранилище Telegram sessions.

telegram_sessions/
    session_001.session      ← файл Telethon (SQLite с auth key) — это СЕКРЕТ, никому не отдавать
    session_001.json         ← api_id / api_hash для этой session (если отличаются от .env), chmod 600
    removed/                 ← сюда переносятся удалённые через /remove_session

Можно просто положить готовые *.session (Telethon) в папку — они подхватятся при запуске или /reload.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^session_(\d+)$")


def _proxies_enabled() -> bool:
    """USE_PROXIES=1 в .env включает прокси; по умолчанию все аккаунты работают напрямую."""
    return os.getenv("USE_PROXIES", "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SessionRecord:
    name: str
    path: Path
    api_id: int
    api_hash: str
    owner_id: int | None = None      # кто добавил session через бота (может её удалить)
    proxy: str | None = None         # socks5://user:pass@host:port — свой IP для аккаунта


def _secure(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows — права задаются ACL папки


class SessionStorage:
    def __init__(self, directory: Path, default_api_id: int, default_api_hash: str):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.default_api_id = default_api_id
        self.default_api_hash = default_api_hash
        # Кеш list_sessions на 5с — админ-команды часто его дёргают (/sessions, /flood, /lookup_panel...),
        # а glob+json.loads на N файлов — лишний диск при каждом нажатии кнопки. 5с достаточно,
        # чтобы /reload, /add_session, /remove_session увидели изменения быстро.
        self._list_cache: list[SessionRecord] | None = None
        self._list_cache_at = 0.0

    def wipe_proxies(self) -> int:
        """Полностью убрать прокси: очистить пул и снять прокси со всех аккаунтов."""
        n = 0
        try:
            self.pool_path.unlink()
        except FileNotFoundError:
            pass
        for meta in self.dir.glob("*.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("proxy"):
                data["proxy"] = None
                meta.write_text(json.dumps(data), encoding="utf-8")
                _secure(meta)
                n += 1
        if n:
            self._invalidate_list_cache()
        return n

    def list_sessions(self) -> list[SessionRecord]:
        import time as _t
        now = _t.monotonic()
        # Кеш учитывает USE_PROXIES: при переключении (например, в тестах) содержимое SessionRecord.proxy
        # меняется, но файлы те же — поэтому в ключ кеша входит и флаг прокси.
        sig = _proxies_enabled()
        if (self._list_cache is not None and now - self._list_cache_at < 5.0
                and getattr(self, "_list_cache_sig", None) == sig):
            return self._list_cache
        out = []
        for p in sorted(self.dir.glob("*.session")):
            name = p.stem
            api_id, api_hash, owner, proxy = self.default_api_id, self.default_api_hash, None, None
            meta = p.with_suffix(".json")
            if meta.exists():
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {}
                    api_id = int(data.get("api_id") or api_id)
                    api_hash = str(data.get("api_hash") or api_hash)
                    owner = int(data["owner_id"]) if data.get("owner_id") else None
                    proxy = (data.get("proxy") or None) if _proxies_enabled() else None
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    pass
            out.append(SessionRecord(name, p, api_id, api_hash, owner, proxy))
        self._list_cache = out
        self._list_cache_at = now
        self._list_cache_sig = sig
        return out

    def _invalidate_list_cache(self) -> None:
        """Явно сбросить кеш после /reload, /add_session, /remove_session."""
        self._list_cache = None
        self._list_cache_at = 0.0

    def next_name(self) -> str:
        used = [int(m.group(1)) for p in self.dir.glob("session_*")
                if (m := _NAME_RE.match(p.stem))]
        used += [int(m.group(1)) for p in (self.dir / "removed").glob("session_*")
                 if (m := _NAME_RE.match(p.stem))] if (self.dir / "removed").exists() else []
        return f"session_{(max(used) + 1 if used else 1):03d}"

    def session_path(self, name: str) -> Path:
        return self.dir / f"{name}.session"

    def _read_meta(self, name: str) -> dict:
        try:
            data = json.loads((self.dir / f"{name}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _atomic_write_json(self, meta: Path, data: dict) -> None:
        """tmp + os.replace — иначе падение (SIGKILL, диск, power) оставит частично
        записанный .json, и _read_meta вернёт {} → потеря api_id/api_hash/owner_id."""
        tmp = meta.with_suffix(meta.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        _secure(tmp)
        os.replace(tmp, meta)

    def save_meta(self, name: str, api_id: int, api_hash: str, phone: str | None = None,
                  owner_id: int | None = None, proxy: str | None = None) -> None:
        meta = self.dir / f"{name}.json"
        data = self._read_meta(name)
        data.update({"api_id": api_id, "api_hash": api_hash})
        if owner_id:
            data["owner_id"] = owner_id
        if proxy is not None:
            data["proxy"] = proxy or None
        if phone:
            data["phone_hint"] = phone[:4] + "****" + phone[-2:] if len(phone) > 6 else "****"
        self._atomic_write_json(meta, data)
        sp = self.session_path(name)
        if sp.exists():
            _secure(sp)
        self._invalidate_list_cache()

    def set_proxy(self, name: str, proxy: str | None) -> None:
        data = self._read_meta(name)
        data["proxy"] = proxy or None
        meta = self.dir / f"{name}.json"
        self._atomic_write_json(meta, data)
        self._invalidate_list_cache()

    # ------------------------------------------------------------------ пул прокси
    @property
    def pool_path(self) -> Path:
        return self.dir / "proxies.txt"

    def load_pool(self) -> list[str]:
        from utils.proxy import split_lines
        try:
            return split_lines(self.pool_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []

    def save_pool(self, proxies: list[str]) -> None:
        # tmp + os.replace, чтобы при сбое не оставить proxies.txt с половиной строк
        # (split_lines прочитает их, но потеряет остальные прокси → пустые аккаунты).
        tmp = self.pool_path.with_suffix(self.pool_path.suffix + ".tmp")
        tmp.write_text("\n".join(proxies) + "\n", encoding="utf-8")
        _secure(tmp)
        os.replace(tmp, self.pool_path)

    def pick_proxy(self, per_proxy: int) -> str | None:
        """Наименее загруженный прокси из пула (не больше per_proxy аккаунтов на один)."""
        if not _proxies_enabled():
            return None
        pool = self.load_pool()
        if not pool:
            return None
        used: dict[str, int] = {p: 0 for p in pool}
        for rec in self.list_sessions():
            if rec.proxy in used:
                used[rec.proxy] += 1
        best = min(pool, key=lambda p: used[p])
        return best if used[best] < max(1, per_proxy) else None

    def remove(self, name: str) -> bool:
        target = self.dir / "removed"
        target.mkdir(exist_ok=True)
        moved = False
        for suffix in (".session", ".json", ".session-journal"):
            p = self.dir / f"{name}{suffix}"
            if p.exists():
                shutil.move(str(p), str(target / p.name))
                moved = True
        if moved:
            self._invalidate_list_cache()
        return moved

    def discard(self, name: str) -> None:
        """Удалить недоделанную (неавторизованную) session после неудачного /add_session."""
        self._invalidate_list_cache()
        for suffix in (".session", ".json", ".session-journal"):
            p = self.dir / f"{name}{suffix}"
            try:
                p.unlink()
            except FileNotFoundError:
                pass
