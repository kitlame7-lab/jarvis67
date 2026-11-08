"""Тёмная тема и значки, нарисованные кодом (файлов-картинок в программе нет)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QRadialGradient

BG = "#0e1116"
SIDEBAR = "#12161d"
PANEL = "#171c25"
CARD = "#1b212c"
FIELD = "#10141b"
BORDER = "#283040"
TEXT = "#e8ecf4"
MUTED = "#8c96ab"
ACCENT = "#4aa3ff"
ACCENT_HI = "#74b9ff"
OK = "#3ecf8e"
WARN = "#f5a524"
BAD = "#ff6464"

STATE_COLOR = {"listening": OK, "loading": WARN, "paused": MUTED, "stopped": MUTED, "error": BAD}

QSS = f"""
* {{ font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif; font-size: 14px; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QWidget#sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 20px; font-weight: 600; letter-spacing: 1px; }}
QLabel#brandSub {{ color: {MUTED}; font-size: 12px; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; }}
QListWidget#nav::item {{ padding: 10px 14px; margin: 2px 8px; border-radius: 8px; color: {MUTED}; }}
QListWidget#nav::item:hover {{ background: {PANEL}; color: {TEXT}; }}
QListWidget#nav::item:selected {{ background: {CARD}; color: {TEXT}; border-left: 3px solid {ACCENT}; }}
QLabel#h1 {{ font-size: 22px; font-weight: 600; }}
QLabel#lead {{ color: {MUTED}; }}
QLabel#muted {{ color: {MUTED}; font-size: 12.5px; }}
QLabel#label {{ color: {MUTED}; font-size: 12.5px; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLabel#cardTitle {{ font-size: 15px; font-weight: 600; }}
QFrame#banner {{ background: #1c2a3d; border: 1px solid #2f4a6e; border-radius: 10px; }}
QFrame#bannerWarn {{ background: #33281a; border: 1px solid #6e5320; border-radius: 10px; }}
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget {{
    background: {FIELD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 10px;
    selection-background-color: {ACCENT}; selection-color: #06101c; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {MUTED}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {CARD};
    outline: none; padding: 4px; }}
QPushButton {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 16px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {CARD}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {BORDER}; background: {SIDEBAR}; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: #06101c; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton#primary:disabled {{ background: #2a4a6a; border-color: #2a4a6a; color: #7f93a8; }}
QPushButton#danger {{ color: {BAD}; }}
QPushButton#danger:hover {{ border-color: {BAD}; }}
QPushButton#link {{ border: none; background: transparent; color: {ACCENT}; padding: 4px 6px; text-align: left; }}
QPushButton#link:hover {{ color: {ACCENT_HI}; }}
QCheckBox {{ spacing: 10px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {BORDER}; background: {FIELD}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QProgressBar {{ background: {FIELD}; border: 1px solid {BORDER}; border-radius: 6px; height: 10px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QHeaderView::section {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER}; padding: 6px 10px; color: {MUTED}; }}
QTableWidget {{ gridline-color: {BORDER}; padding: 0; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 6px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {CARD}; }}
"""


def make_icon(state: str = "listening", size: int = 256) -> QIcon:
    """Круглая «арка» в духе реактора: цвет показывает состояние."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = size / 2
    color = QColor(STATE_COLOR.get(state, ACCENT) if state != "listening" else ACCENT)
    grad = QRadialGradient(QPointF(c, c), c)
    grad.setColorAt(0.0, QColor("#1c2a3d"))
    grad.setColorAt(1.0, QColor("#0b0f15"))
    p.setBrush(QBrush(grad))
    p.setPen(QPen(color, size * 0.055))
    p.drawEllipse(QRectF(size * 0.07, size * 0.07, size * 0.86, size * 0.86))
    p.setPen(QPen(color, size * 0.035))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRectF(size * 0.27, size * 0.27, size * 0.46, size * 0.46))
    glow = QColor(color)
    glow.setAlpha(230)
    p.setBrush(QBrush(glow))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(size * 0.42, size * 0.42, size * 0.16, size * 0.16))
    p.end()
    return QIcon(pm)


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
        path = folder / "check.png"
        pm = QPixmap(36, 36)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#06101c"), 4.2)
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
