"""Хранилище задач, созданных по записи действий: папка на задачу с task.json и снимками экрана.

`%APPDATA%\\Jarvis\\tasks\\<имя-латиницей>\\task.json`. Запускает задачи `pc.py task run` (jarvis-pc.exe): тот читает
тот же формат и свой поиск по названию, поэтому логика поиска здесь и там должна совпадать (это проверяет тест).
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths

FORMAT_VERSION = 1
_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                      "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def tasks_dir() -> Path:
    return paths.DATA_DIR / "tasks"


def slugify(name: str) -> str:
    """Имя папки: латиница, цифры и дефисы (русский текст в путях и командах приносит лишние проблемы)."""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in (name or "").lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return (out or "task")[:48]


@dataclass
class Task:
    name: str
    slug: str = ""
    description: str = ""
    phrases: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    created: str = ""
    screen: List[int] = field(default_factory=lambda: [1920, 1080])
    notes: Dict[str, str] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {"version": FORMAT_VERSION, "name": self.name, "slug": self.slug, "description": self.description,
                "phrases": self.phrases, "created": self.created, "screen": self.screen, "notes": self.notes,
                "steps": self.steps}

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Task":
        return cls(name=str(data.get("name", "")), slug=str(data.get("slug", "")), description=str(data.get("description", "")),
                   phrases=[str(p) for p in data.get("phrases", [])], steps=list(data.get("steps", [])),
                   created=str(data.get("created", "")), screen=list(data.get("screen", [1920, 1080])),
                   notes={str(k): str(v) for k, v in (data.get("notes") or {}).items()})


def unique_slug(name: str, root: Optional[Path] = None) -> str:
    root = root or tasks_dir()
    base = slugify(name)
    slug, n = base, 2
    while (root / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def save_task(task: Task, shots_from: Optional[Path] = None, root: Optional[Path] = None) -> Path:
    """Записывает задачу; снимки экрана переносит из временной папки записи."""
    root = root or tasks_dir()
    if not task.slug:
        task.slug = unique_slug(task.name, root)
    if not task.created:
        task.created = datetime.now().isoformat(timespec="seconds")
    folder = root / task.slug
    folder.mkdir(parents=True, exist_ok=True)
    if shots_from and shots_from.exists():
        target = folder / "shots"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(shots_from), str(target))
    (folder / "task.json").write_text(json.dumps(task.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


def load_task(slug: str, root: Optional[Path] = None) -> Optional[Task]:
    path = (root or tasks_dir()) / slug / "task.json"
    try:
        task = Task.from_json(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        return None
    task.slug = task.slug or slug
    return task


def list_tasks(root: Optional[Path] = None) -> List[Task]:
    root = root or tasks_dir()
    out = []
    if root.exists():
        for folder in sorted(root.iterdir()):
            if folder.is_dir() and not folder.name.startswith("_"):
                task = load_task(folder.name, root)
                if task:
                    out.append(task)
    return sorted(out, key=lambda t: t.created, reverse=True)


def delete_task(slug: str, root: Optional[Path] = None) -> bool:
    folder = (root or tasks_dir()) / slug
    if not slug or "/" in slug or "\\" in slug or ".." in slug or not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е"))).strip()


def find_task(query: str, tasks: Optional[List[Task]] = None, root: Optional[Path] = None) -> Optional[Task]:
    """Ищет задачу по названию, папке или фразе вызова: точно, по вхождению, затем нечётко.

    Тот же порядок реализован в pc.py (`task run`); менять нужно обе стороны сразу."""
    tasks = tasks if tasks is not None else list_tasks(root)
    q = _norm(query)
    if not q:
        return None
    labels = [(t, [_norm(t.name), _norm(t.slug)] + [_norm(p) for p in t.phrases]) for t in tasks]
    for t, names in labels:
        if q in names:
            return t
    for t, names in labels:
        if any(q in n or n in q for n in names if n):
            return t
    best, score = None, 0.0
    for t, names in labels:
        for n in names:
            r = difflib.SequenceMatcher(None, q, n).ratio() if n else 0.0
            if r > score:
                best, score = t, r
    return best if score >= 0.62 else None
