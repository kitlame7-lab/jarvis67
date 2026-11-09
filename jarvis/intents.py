"""Голосовые команды самого Джарвиса, которые обрабатываются до обращения к Hermes: запись действий.

Всё остальное (в том числе «выполни задачу …») идёт в Hermes как обычная просьба, а он вызывает навык jarvis-pc.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .core import normalize

START = [
    r"\bзапомни(те)?\b.*\b(как я|что я|мои действия|эти действия|мои шаги)\b",
    r"\bзапиши(те)?\b.*\b(мои действия|действия|экран|что я делаю|как я|мои шаги)\b",
    r"\bзаписывай\b",
    r"\bзапишем\b.*\bзадач",
    r"\bнаучись\b",
    r"\bсмотри\b.*\b(как я|что я)\b",
    r"\bсоздай задачу (по|из|на основе)\b.*\b(действ|запис|экран)",
    r"\bначни(те)? запись\b",
    r"\bповторяй за мной\b",
]
STOP = [
    r"\bстоп\b", r"\bхватит\b", r"\bготово\b", r"\bзакончил(а|и)?\b", r"\bзаверши(ть)?\b",
    r"\bостанови(ть)? запись\b", r"\bэто (всё|все)\b", r"\bзавершить запись\b", r"\bсохрани(ть)?\b",
]
CANCEL = [r"\bотмен(а|и|ить) запис", r"\bне сохраняй\b", r"\bотбой\b", r"\bзабудь (это|запись)\b"]


def _fold(patterns):
    """normalize() заменяет й на и и ё на е, поэтому шаблоны приводятся к тому же виду."""
    return [re.compile(p.replace("й", "и").replace("ё", "е")) for p in patterns]


START, STOP, CANCEL = _fold(START), _fold(STOP), _fold(CANCEL)
RUN_TASK = re.compile(r"\b(?:выполни|запусти|повтори|проиграй|сделай|включи)(?:те)?\s+(?:мою\s+|эту\s+|записанную\s+)?"
                      r"(?:задачу|задание)\s+(.+)$")
RUN_ROUTINE = re.compile(r"\b(?:выполни|запусти|сделай|проиграй|включи)(?:те)?\s+(?:мой\s+|мою\s+|свой\s+)?"
                         r"(?:шаблон|рутину|сценарий|распорядок)\s+(.+)$")
NAME = re.compile(r"(?:назови(?:те)?(?: (?:её|ее|это|задачу|запись))?|называется|под названием|назван(?:ие)?)\s+(.+)$")


@dataclass
class Intent:
    kind: str                # teach_start | teach_stop | teach_cancel | task_run | routine_run
    arg: str = ""            # название задачи, если его назвали


def parse(text: str, recording: bool = False) -> Optional[Intent]:
    """Распознаёт просьбу про запись. «Стоп» и «отмена» учитываются только во время записи."""
    low = normalize(text)
    if not low:
        return None
    name = ""
    raw = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()   # название берём с исходными й и ё
    m = NAME.search(raw)
    if m:
        name = m.group(1).strip(" .!?,")
    if recording:
        if any(p.search(low) for p in CANCEL):
            return Intent("teach_cancel")
        if any(p.search(low) for p in STOP):
            return Intent("teach_stop", name)
        return None
    if any(p.search(low) for p in START):
        return Intent("teach_start", name)
    routine = RUN_ROUTINE.search(raw)
    if routine:
        return Intent("routine_run", re.sub(r"\s+пожалуйста$", "", routine.group(1).strip()))
    run = RUN_TASK.search(raw)
    if run:
        return Intent("task_run", re.sub(r"\s+пожалуйста$", "", run.group(1).strip()))
    return None
