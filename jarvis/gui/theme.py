"""Оформление: светлая тема в духе референса (почти белый фон, чёрные кнопки-«таблетки», мягкие скруглённые поля,
стеклянные шарики) и тёмный вариант. Все цвета лежат в `PALETTES`: чтобы поменять вид, достаточно поменять значения.

Значки и шарик рисуются кодом, файлов-картинок в программе нет."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap,
                           QRadialGradient)

# Внимание: Qt молча отбрасывает border-radius, если он больше половины высоты элемента. Поэтому у кнопок и полей
# радиус 20px при минимальной высоте не меньше 42px.
PALETTES: Dict[str, Dict[str, str]] = {
    "light": {
        "BG": "#f4f4f5", "SIDEBAR": "#f4f4f5", "PANEL": "#ececef", "CARD": "#ffffff", "FIELD": "#ffffff",
        "BORDER": "#e6e6ea", "TEXT": "#111113", "MUTED": "#7d7d88", "ACCENT": "#4a7dff", "ACCENT_HI": "#6b94ff",
        "PRIMARY": "#111113", "PRIMARY_HI": "#2b2b30", "ON_PRIMARY": "#ffffff",
        "DISABLED": "#dcdce0", "DISABLED_TEXT": "#a2a2ab", "FOCUS": "#111113", "HOVER": "#f0f0f3",
        "OK": "#1f9d6b", "WARN": "#b7791f", "BAD": "#c4455d", "REC": "#ff3b30",
        "BANNER_BG": "#eef3ff", "BANNER_BORDER": "#cfdcff", "WARN_BG": "#fff8e8", "WARN_BORDER": "#f0dcaa",
        "BAD_BG": "#fff1f2", "BAD_BORDER": "#f3c9cf", "REC_BG": "#fff0ef", "REC_BORDER": "#f5b5b0",
        "SCROLL": "#cfcfd6",
    },
    "dark": {
        "BG": "#050505", "SIDEBAR": "#050505", "PANEL": "#131315", "CARD": "#0e0e10", "FIELD": "#08080a",
        "BORDER": "#1f1f24", "TEXT": "#f2f2f4", "MUTED": "#8b8b95", "ACCENT": "#6ea8ff", "ACCENT_HI": "#93bdff",
        "PRIMARY": "#f2f2f4", "PRIMARY_HI": "#ffffff", "ON_PRIMARY": "#050505",
        "DISABLED": "#2a2a30", "DISABLED_TEXT": "#6d6d76", "FOCUS": "#f2f2f4", "HOVER": "#18181b",
        "OK": "#3ecf8e", "WARN": "#f5a524", "BAD": "#ff5c5c", "REC": "#ff4040",
        "BANNER_BG": "#101a2b", "BANNER_BORDER": "#25406b", "WARN_BG": "#2a2113", "WARN_BORDER": "#6e5320",
        "BAD_BG": "#1c0f0f", "BAD_BORDER": "#5a2222", "REC_BG": "#2a1010", "REC_BORDER": "#6e2020",
        "SCROLL": "#2a2a31",
    },
}

DEFAULT_MODE = "light"
MODE = DEFAULT_MODE
STATE_COLOR: Dict[str, str] = {}
QSS = ""

# Цвета шарика по состояниям: у «слушаю» радужный, у остальных преобладает свой оттенок
ORB_COLORS: Dict[str, List[str]] = {
    "listening": ["#7aa7ff", "#ff9ed0", "#ffc27a", "#8fe3c2"],
    "loading": ["#ffd37a", "#ffb86b", "#ff9ed0", "#ffe3a3"],
    "paused": ["#c9c9d1", "#e6e6ea", "#b8b8c2", "#dcdce2"],
    "stopped": ["#c9c9d1", "#e6e6ea", "#b8b8c2", "#dcdce2"],
    "error": ["#ff7a7a", "#ff4d6d", "#ffa07a", "#ff8fa3"],
    "recording": ["#ff5050", "#ff2d2d", "#ff8a8a", "#ff6a4d"],
}


def set_mode(mode: str) -> str:
    """Переключает палитру: обновляет цвета-константы модуля и таблицу стилей `QSS`."""
    global MODE, QSS
    MODE = mode if mode in PALETTES else DEFAULT_MODE
    globals().update(PALETTES[MODE])
    STATE_COLOR.clear()
    pal = PALETTES[MODE]
    STATE_COLOR.update({"listening": pal["OK"], "loading": pal["WARN"], "paused": pal["MUTED"], "stopped": pal["MUTED"],
                        "error": pal["BAD"], "recording": pal["REC"]})
    QSS = build_qss()
    return MODE


def build_qss() -> str:
    c = PALETTES[MODE]
    return f"""
* {{ font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", sans-serif; font-size: 14px; color: {c['TEXT']}; }}
QMainWindow, QWidget#root {{ background: {c['BG']}; }}
QWidget#sidebar {{ background: {c['SIDEBAR']}; }}
QLabel#brand {{ font-size: 20px; font-weight: 700; }}
QLabel#brandSub {{ color: {c['MUTED']}; font-size: 12px; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; }}
QListWidget#nav::item {{ padding: 11px 18px; margin: 3px 14px; border-radius: 20px; min-height: 18px; color: {c['MUTED']}; }}
QListWidget#nav::item:hover {{ background: {c['PANEL']}; color: {c['TEXT']}; }}
QListWidget#nav::item:selected {{ background: {c['PRIMARY']}; color: {c['ON_PRIMARY']}; }}
QLabel#h1 {{ font-size: 30px; font-weight: 700; }}
QLabel#hero {{ font-size: 30px; font-weight: 700; }}
QLabel#lead {{ color: {c['MUTED']}; font-size: 15px; }}
QLabel#muted {{ color: {c['MUTED']}; font-size: 12.5px; }}
QLabel#label {{ color: {c['MUTED']}; font-size: 12.5px; }}
QFrame#card {{ background: {c['CARD']}; border: 1px solid {c['BORDER']}; border-radius: 24px; }}
QLabel#cardTitle {{ font-size: 16px; font-weight: 700; }}
QFrame#banner {{ background: {c['BANNER_BG']}; border: 1px solid {c['BANNER_BORDER']}; border-radius: 20px; }}
QFrame#bannerWarn {{ background: {c['WARN_BG']}; border: 1px solid {c['WARN_BORDER']}; border-radius: 20px; }}
QFrame#rec {{ background: {c['REC_BG']}; border: 1px solid {c['REC_BORDER']}; border-radius: 20px; }}
QFrame#finding {{ background: {c['CARD']}; border: 1px solid {c['BORDER']}; border-radius: 18px; }}
QFrame#findingBad {{ background: {c['BAD_BG']}; border: 1px solid {c['BAD_BORDER']}; border-radius: 18px; }}
QFrame#findingWarn {{ background: {c['WARN_BG']}; border: 1px solid {c['WARN_BORDER']}; border-radius: 18px; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {c['FIELD']}; border: 1px solid {c['BORDER']}; border-radius: 20px; padding: 10px 18px; min-height: 22px;
    selection-background-color: {c['PRIMARY']}; selection-color: {c['ON_PRIMARY']}; }}
QPlainTextEdit, QTextBrowser, QTableWidget {{
    background: {c['FIELD']}; border: 1px solid {c['BORDER']}; border-radius: 18px; padding: 10px 14px;
    selection-background-color: {c['PRIMARY']}; selection-color: {c['ON_PRIMARY']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {c['FOCUS']}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {c['MUTED']}; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{ background: {c['CARD']}; border: 1px solid {c['BORDER']}; selection-background-color: {c['PANEL']};
    selection-color: {c['TEXT']}; outline: none; padding: 6px; border-radius: 12px; }}
QPushButton {{ background: {c['CARD']}; border: 1px solid {c['BORDER']}; border-radius: 20px; padding: 11px 22px; min-height: 20px; }}
QPushButton:hover {{ background: {c['HOVER']}; }}
QPushButton:pressed {{ background: {c['PANEL']}; }}
QPushButton:disabled {{ color: {c['DISABLED_TEXT']}; background: {c['DISABLED']}; border-color: {c['DISABLED']}; }}
QPushButton#primary {{ background: {c['PRIMARY']}; border: 1px solid {c['PRIMARY']}; color: {c['ON_PRIMARY']}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {c['PRIMARY_HI']}; border-color: {c['PRIMARY_HI']}; }}
QPushButton#primary:disabled {{ background: {c['DISABLED']}; border-color: {c['DISABLED']}; color: {c['DISABLED_TEXT']}; }}
QPushButton#rec {{ background: {c['REC']}; border: 1px solid {c['REC']}; color: #ffffff; font-weight: 600; }}
QPushButton#rec:hover {{ background: #ff5a50; border-color: #ff5a50; }}
QPushButton#rec:disabled {{ background: {c['DISABLED']}; border-color: {c['DISABLED']}; color: {c['DISABLED_TEXT']}; }}
QPushButton#danger {{ color: {c['BAD']}; }}
QPushButton#link {{ border: none; background: transparent; color: {c['MUTED']}; padding: 4px 8px; text-align: left; }}
QPushButton#link:hover {{ color: {c['TEXT']}; }}
QCheckBox {{ spacing: 10px; }}
QCheckBox::indicator {{ width: 20px; height: 20px; border-radius: 7px; border: 1px solid {c['SCROLL']}; background: {c['FIELD']}; }}
QCheckBox::indicator:checked {{ background: {c['PRIMARY']}; border-color: {c['PRIMARY']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {c['BORDER']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {c['PRIMARY']}; width: 18px; height: 18px; margin: -7px 0; border-radius: 9px; }}
QSlider::sub-page:horizontal {{ background: {c['PRIMARY']}; border-radius: 2px; }}
QProgressBar {{ background: {c['PANEL']}; border: none; border-radius: 5px; height: 10px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {c['PRIMARY']}; border-radius: 5px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['SCROLL']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QHeaderView::section {{ background: {c['PANEL']}; border: none; border-bottom: 1px solid {c['BORDER']}; padding: 6px 10px; color: {c['MUTED']}; }}
QTableWidget {{ gridline-color: {c['BORDER']}; padding: 0; }}
QToolTip {{ background: {c['CARD']}; color: {c['TEXT']}; border: 1px solid {c['BORDER']}; padding: 6px; }}
QMenu {{ background: {c['CARD']}; border: 1px solid {c['BORDER']}; padding: 6px; border-radius: 12px; }}
QMenu::item {{ padding: 8px 22px; border-radius: 8px; }}
QMenu::item:selected {{ background: {c['PANEL']}; }}
"""


# --------------------------------------------------------------------------
# Шарик и значки
# --------------------------------------------------------------------------

def orb(state: str = "listening", size: int = 128) -> QPixmap:
    """Стеклянный шарик с радужным переливом, как в референсе. Цвет зависит от состояния."""
    scale = 2
    s = size * scale
    pm = QPixmap(s, s)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    colors = [QColor(x) for x in ORB_COLORS.get(state, ORB_COLORS["listening"])]
    pad = s * 0.08
    body = QRectF(pad, pad * 0.6, s - 2 * pad, s - 2 * pad)

    # мягкая тень под шариком
    shadow = QRadialGradient(QPointF(s / 2, s * 0.94), s * 0.32)
    shadow.setColorAt(0.0, QColor(0, 0, 0, 55))
    shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(shadow))
    p.drawEllipse(QRectF(s * 0.2, s * 0.86, s * 0.6, s * 0.12))

    # перелив: коническая заливка по кругу из цветов состояния
    conic = QConicalGradient(body.center(), 40)
    for i, col in enumerate(colors + [colors[0]]):
        conic.setColorAt(i / len(colors), col)
    p.setBrush(QBrush(conic))
    p.drawEllipse(body)

    # объём: тёмный край и светлая середина
    vol = QRadialGradient(QPointF(body.center().x() - s * 0.08, body.center().y() - s * 0.1), body.width() * 0.62)
    vol.setColorAt(0.0, QColor(255, 255, 255, 120))
    vol.setColorAt(0.55, QColor(255, 255, 255, 10))
    vol.setColorAt(1.0, QColor(20, 10, 40, 95))
    p.setBrush(QBrush(vol))
    p.drawEllipse(body)

    # блик
    hi = QLinearGradient(QPointF(body.left(), body.top()), QPointF(body.left() + body.width() * 0.5, body.top() + body.height() * 0.5))
    hi.setColorAt(0.0, QColor(255, 255, 255, 235))
    hi.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(hi))
    p.drawEllipse(QRectF(body.left() + body.width() * 0.14, body.top() + body.height() * 0.08,
                         body.width() * 0.42, body.height() * 0.3))
    p.setBrush(QColor(255, 255, 255, 190))
    p.drawEllipse(QRectF(body.right() - body.width() * 0.3, body.bottom() - body.height() * 0.22,
                         body.width() * 0.12, body.width() * 0.06))

    # тонкое светлое кольцо
    ring = QPen(QColor(255, 255, 255, 140), s * 0.012)
    p.setPen(ring)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(body)
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


def make_icon(state: str = "listening", size: int = 256) -> QIcon:
    return QIcon(orb(state, size // 2))


def dot(color: str, size: int = 12) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size)
    p.end()
    return pm


def font(size: int = 14, bold: bool = False) -> QFont:
    f = QFont()
    f.setPixelSize(size)
    f.setBold(bold)
    return f


def asset_qss(folder: Path) -> str:
    """Галочка для флажков: рисуется кодом и кладётся файлом, потому что в QSS нельзя нарисовать фигуру."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"check-{MODE}.png"
        pm = QPixmap(36, 36)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(PALETTES[MODE]["ON_PRIMARY"]), 4.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(9, 19), QPointF(15.5, 25.5), QPointF(27, 11)])
        p.end()
        pm.save(str(path))
        url = str(path).replace("\\", "/")
        return f'QCheckBox::indicator:checked {{ image: url("{url}"); }}'
    except Exception:  # noqa: BLE001 — без галочки приложение работает, просто флажок будет сплошным
        return ""


set_mode(DEFAULT_MODE)
