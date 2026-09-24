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


@dataclass
class SessionRecord:
    name: str
    path: Path
    api_id: int
    api_hash: str


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

    def list_sessions(self) -> list[SessionRecord]:
        out = []
        for p in sorted(self.dir.glob("*.session")):
            name = p.stem
            api_id, api_hash = self.default_api_id, self.default_api_hash
            meta = p.with_suffix(".json")
            if meta.exists():
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                    api_id = int(data.get("api_id") or api_id)
                    api_hash = str(data.get("api_hash") or api_hash)
                except (ValueError, json.JSONDecodeError):
                    pass
            out.append(SessionRecord(name, p, api_id, api_hash))
        return out

    def next_name(self) -> str:
        used = [int(m.group(1)) for p in self.dir.glob("session_*")
                if (m := _NAME_RE.match(p.stem))]
        used += [int(m.group(1)) for p in (self.dir / "removed").glob("session_*")
                 if (m := _NAME_RE.match(p.stem))] if (self.dir / "removed").exists() else []
        return f"session_{(max(used) + 1 if used else 1):03d}"

    def session_path(self, name: str) -> Path:
        return self.dir / f"{name}.session"

    def save_meta(self, name: str, api_id: int, api_hash: str, phone: str | None = None) -> None:
        meta = self.dir / f"{name}.json"
        data = {"api_id": api_id, "api_hash": api_hash}
        if phone:
            data["phone_hint"] = phone[:4] + "****" + phone[-2:] if len(phone) > 6 else "****"
        meta.write_text(json.dumps(data), encoding="utf-8")
        _secure(meta)
        sp = self.session_path(name)
        if sp.exists():
            _secure(sp)

    def remove(self, name: str) -> bool:
        target = self.dir / "removed"
        target.mkdir(exist_ok=True)
        moved = False
        for suffix in (".session", ".json", ".session-journal"):
            p = self.dir / f"{name}{suffix}"
            if p.exists():
                shutil.move(str(p), str(target / p.name))
                moved = True
        return moved

    def discard(self, name: str) -> None:
        """Удалить недоделанную (неавторизованную) session после неудачного /add_session."""
        for suffix in (".session", ".json", ".session-journal"):
            p = self.dir / f"{name}{suffix}"
            try:
                p.unlink()
            except FileNotFoundError:
                pass
