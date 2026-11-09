"""Текст для озвучки по кусочкам: модель пишет потоком, а говорить нужно целыми фразами и как можно раньше.

`StreamSplitter` получает кусочки ответа модели и отдаёт готовые предложения. Строки `ДЕЙСТВИЕ: …` не озвучиваются:
это просьба выполнить команду, а не слова для человека."""
from __future__ import annotations

import re
from typing import List

from .core import normalize

ACTION_PREFIXES = ("действие", "action")
SENTENCE_END = re.compile(r"[.!?…]+[»\")]*(?=\s)|[.!?…]+[»\")]*$")
MIN_SENTENCE = 12          # «Т. е.», «1.» и подобное не режем отдельно
SOFT_LIMIT = 140           # длинную фразу без точки режем по запятой, чтобы не ждать конца


def _could_be_action(line: str) -> bool:
    head = line.lstrip().lower()
    if not head:
        return True        # пустое начало строки: ещё неизвестно, что будет
    for p in ACTION_PREFIXES:
        if head.startswith(p) or p.startswith(head):
            return True
    return False


def is_action_line(line: str) -> bool:
    head = line.lstrip().lower()
    return any(head.startswith(p) for p in ACTION_PREFIXES) and ":" in head[:12].replace("：", ":")


def split_sentences(text: str, final: bool) -> "tuple[List[str], str]":
    """Готовые предложения и остаток."""
    out: List[str] = []
    start = 0
    for m in SENTENCE_END.finditer(text):
        end = m.end()
        if end - start >= MIN_SENTENCE or (final and end == len(text)):
            piece = text[start:end].strip()
            if piece:
                out.append(piece)
            start = end
    rest = text[start:]
    if not final and len(rest) > SOFT_LIMIT:
        cut = rest.rfind(",", 0, SOFT_LIMIT)
        if cut > MIN_SENTENCE:
            out.append(rest[:cut + 1].strip())
            rest = rest[cut + 1:]
    if final:
        if rest.strip():
            out.append(rest.strip())
        rest = ""
    return out, rest


class StreamSplitter:
    def __init__(self) -> None:
        self.line = ""          # текущая строка целиком
        self.done = 0           # сколько символов этой строки уже отдано
        self.action = False     # строка оказалась командой

    def feed(self, chunk: str) -> List[str]:
        out: List[str] = []
        for part in re.split(r"(\n)", chunk):
            if part == "\n":
                out += self._end_line()
            elif part:
                self.line += part
                out += self._advance()
        return out

    def _advance(self) -> List[str]:
        if self.action:
            return []
        if is_action_line(self.line):
            self.action = True
            return []
        if self.done == 0 and _could_be_action(self.line) and len(self.line.strip()) < 12:
            return []                         # ещё неизвестно, команда это или текст
        ready, rest = split_sentences(self.line[self.done:], final=False)
        self.done = len(self.line) - len(rest)
        return ready

    def _end_line(self) -> List[str]:
        out: List[str] = []
        if not self.action and not is_action_line(self.line):
            out, _ = split_sentences(self.line[self.done:], final=True)
        self.line, self.done, self.action = "", 0, False
        return out

    def flush(self) -> List[str]:
        return self._end_line()


def echo_score(heard: str, spoken: str) -> float:
    """Насколько услышанное похоже на то, что Джарвис сам только что сказал (0…1).

    Колонки возвращают его голос в микрофон; такое «перебивание» нужно игнорировать."""
    hw = [w for w in normalize(heard).split() if len(w) > 2]
    sw = set(w for w in normalize(spoken).split() if len(w) > 2)
    if not hw or not sw:
        return 0.0
    hits = 0
    for w in hw:
        if w in sw or any(len(w) > 4 and (w[:5] == s[:5]) for s in sw):
            hits += 1
    return hits / len(hw)
