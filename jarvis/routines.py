"""Шаблоны рутин: обычный текст со списком шагов, который Джарвис выполняет по порядку.

Файл `%APPDATA%\\Jarvis\\routines\\<имя>.md`:

    # Утро
    - Скажи погоду
    - Открой почту в браузере
    - Создай заметку «План на {дата}»

Шаги пишутся человеческим языком; выполняет их встроенный движок теми же командами ПК, что и голосовые просьбы.
Подстановки: {дата}, {вчера}, {завтра}, {время}, {день недели}, а также {1}, {2}… — слова, сказанные после имени шаблона
(«выполни шаблон отчёт за март» → {1} = «за март»)."""
from __future__ import annotations

import datetime as dt
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import paths

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')

EXAMPLE = """# Утро
- Скажи, какое сегодня число и какая погода
- Открой почту в браузере
- Создай заметку «План на {дата}» с текстом «1. »
"""


def routines_dir() -> Path:
    return paths.DATA_DIR / "routines"


@dataclass
class Routine:
    name: str
    steps: List[str] = field(default_factory=list)
    text: str = ""
    path: Optional[Path] = None


def parse(name: str, text: str) -> Routine:
    title = name
    steps: List[str] = []
    plain: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            title = s.lstrip("#").strip() or title
            continue
        m = re.match(r"^(?:[-*•]|\d+[.)])\s+(.+)$", s)
        if m:
            steps.append(m.group(1).strip())
        else:
            plain.append(s)
    if not steps:
        steps = plain
    return Routine(title, steps, text)


def safe_name(name: str) -> str:
    out = BAD_NAME.sub(" ", name or "").strip(" .")
    return re.sub(r"\s+", " ", out)[:60]


def load_all() -> List[Routine]:
    d = routines_dir()
    out: List[Routine] = []
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            try:
                r = parse(f.stem, f.read_text(encoding="utf-8-sig"))
            except OSError:
                continue
            r.path = f
            out.append(r)
    return out


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е"))).strip()


def find(query: str, items: Optional[List[Routine]] = None) -> Optional[Routine]:
    items = load_all() if items is None else items
    q = _fold(query)
    if not q or not items:
        return None
    names = [(_fold(r.name), _fold(r.path.stem if r.path else r.name), r) for r in items]
    for a, b, r in names:
        if q in (a, b):
            return r
    for a, b, r in names:
        if (a and (q.startswith(a) or a in q)) or (b and (q.startswith(b) or b in q)):
            return r
    best = difflib.get_close_matches(q, [a for a, _, _ in names], n=1, cutoff=0.6)
    if best:
        return next(r for a, _, r in names if a == best[0])
    return None


def split_args(query: str, routine: Routine) -> List[str]:
    """Слова после названия шаблона: «отчёт за март» для шаблона «отчёт» → ["за", "март"] → {1}=«за март»."""
    q, n = _fold(query), _fold(routine.name)
    rest = q[len(n):].strip() if q.startswith(n) else ""
    return [rest] if rest else []


def save(name: str, text: str) -> Routine:
    clean = safe_name(name)
    if not clean:
        raise ValueError("Пустое название шаблона")
    if not text.lstrip().startswith("#"):
        text = f"# {clean}\n" + text
    d = routines_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{clean}.md"
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    r = parse(clean, text)
    r.path = path
    return r


def delete(name: str) -> bool:
    r = find(name)
    if r is None or r.path is None:
        return False
    r.path.unlink()
    return True


def expand(text: str, args: Optional[List[str]] = None, now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now()
    values = {
        "дата": now.strftime("%d.%m.%Y"), "вчера": (now - dt.timedelta(days=1)).strftime("%d.%m.%Y"),
        "завтра": (now + dt.timedelta(days=1)).strftime("%d.%m.%Y"), "время": now.strftime("%H:%M"),
        "день недели": WEEKDAYS[now.weekday()],
    }
    for i, a in enumerate(args or [], 1):
        values[str(i)] = a
    return re.sub(r"\{([^{}]+)\}", lambda m: values.get(m.group(1).strip().lower(), m.group(0)), text)


def instruction(routine: Routine, args: Optional[List[str]] = None) -> str:
    """Задание для встроенного движка."""
    steps = "\n".join(f"{i}. {expand(s, args)}" for i, s in enumerate(routine.steps, 1))
    return (f"Выполни шаблон «{routine.name}» строго по шагам, по порядку, командами ПК. Не пропускай шаги и не "
            f"добавляй своих. Если шаг не получается, переходи к следующему. В конце коротко отчитайся: что сделано, "
            f"что не получилось и почему.\nШаги:\n{steps}")


def step_budget(routine: Routine) -> int:
    return min(30, max(6, len(routine.steps) * 2 + 2))
