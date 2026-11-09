"""Общие кирпичики окна: связь потоков с интерфейсом, карточки, поля, строки состояния."""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget)

from . import theme


class UiBus(QObject):
    """Позволяет любому потоку безопасно выполнить функцию в потоке окна."""
    _call = Signal(object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 — сбой в обработчике не должен убивать окно
            from ..core import log
            log.exception("Сбой в обработчике окна")

    def post(self, fn: Callable[[], None]) -> None:
        self._call.emit(fn)


def background(bus: UiBus, work: Callable[[], Any], done: Callable[[Any, Optional[BaseException]], None]) -> None:
    """Выполняет work в потоке, затем вызывает done(результат, ошибка) в потоке окна."""
    def runner() -> None:
        try:
            result, err = work(), None
        except BaseException as exc:  # noqa: BLE001
            result, err = None, exc
        bus.post(lambda: done(result, err))
    threading.Thread(target=runner, daemon=True, name="jarvis-bg").start()


class Card(QFrame):
    def __init__(self, title: str = "", hint: str = ""):
        super().__init__()
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(26, 22, 26, 22)
        self.lay.setSpacing(14)
        if title:
            t = QLabel(title)
            t.setObjectName("cardTitle")
            self.lay.addWidget(t)
        if hint:
            h = QLabel(hint)
            h.setObjectName("muted")
            h.setWordWrap(True)
            self.lay.addWidget(h)

    def add(self, w: QWidget) -> QWidget:
        self.lay.addWidget(w)
        return w

    def row(self, *widgets: QWidget, stretch_last: bool = False) -> QHBoxLayout:
        box = QHBoxLayout()
        box.setSpacing(10)
        for w in widgets:
            box.addWidget(w)
        if not stretch_last:
            box.addStretch(1)
        self.lay.addLayout(box)
        return box


def field(label: str, widget: QWidget, hint: str = "") -> QWidget:
    """Подпись над полем и, при необходимости, пояснение под ним."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    lab = QLabel(label)
    lab.setObjectName("label")
    lay.addWidget(lab)
    lay.addWidget(widget)
    if hint:
        h = QLabel(hint)
        h.setObjectName("muted")
        h.setWordWrap(True)
        lay.addWidget(h)
    box._label = lab  # type: ignore[attr-defined]
    return box


def note(text: str, wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(wrap)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


class Status(QLabel):
    """Строка результата рядом с кнопкой: серая, зелёная или красная."""

    def __init__(self) -> None:
        super().__init__("")
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hide()

    def _show(self, text: str, color: str) -> None:
        self.setStyleSheet(f"color: {color};")
        self.setText(text)
        self.setVisible(bool(text))

    def info(self, text: str) -> None:
        self._show(text, theme.MUTED)

    def ok(self, text: str) -> None:
        self._show(text, theme.OK)

    def warn(self, text: str) -> None:
        self._show(text, theme.WARN)

    def bad(self, text: str) -> None:
        self._show(text, theme.BAD)


class SecretEdit(QLineEdit):
    """Поле для ключа: скрыто, по значку глаза можно показать. Пустое поле = «не менять»."""

    def __init__(self, placeholder: str = ""):
        super().__init__()
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(False)
        self._eye = QAction("👁", self)
        self._eye.setCheckable(True)
        self._eye.setToolTip("Показать или скрыть")
        self._eye.toggled.connect(
            lambda on: self.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.addAction(self._eye, QLineEdit.ActionPosition.TrailingPosition)

    def value(self) -> Optional[str]:
        text = self.text().strip()
        return text or None

    def describe(self, mask: dict) -> str:
        if mask.get("set"):
            tail = mask.get("tail")
            return f"Ключ сохранён{' (…' + tail + ')' if tail else ''}. Оставьте поле пустым, чтобы не менять."
        return "Ключ ещё не задан."


class Page(QScrollArea):
    """Страница раздела: прокручиваемая колонка карточек."""
    title = ""
    lead = ""

    def __init__(self, ctx: Any):
        super().__init__()
        self.ctx = ctx
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("root")
        self.setWidget(body)
        # Колонка по центру с ограниченной шириной: так много воздуха, как в референсе, и строки не растягиваются
        outer = QHBoxLayout(body)
        outer.setContentsMargins(24, 0, 24, 0)
        column = QWidget()
        column.setObjectName("root")
        column.setMaximumWidth(820)
        outer.addStretch(1)
        outer.addWidget(column, 100)
        outer.addStretch(1)
        self.col = QVBoxLayout(column)
        self.col.setContentsMargins(0, 36, 0, 36)
        self.col.setSpacing(18)
        h = QLabel(self.title)
        h.setObjectName("h1")
        self.col.addWidget(h)
        if self.lead:
            lead = QLabel(self.lead)
            lead.setObjectName("lead")
            lead.setWordWrap(True)
            self.col.addWidget(lead)
        self.build()
        self.col.addStretch(1)

    def build(self) -> None:  # переопределяется
        pass

    def load(self) -> None:  # вызывается при показе страницы
        pass

    def add(self, w: QWidget) -> QWidget:
        self.col.addWidget(w)
        return w
