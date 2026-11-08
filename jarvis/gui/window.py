"""Главное окно, значок в трее и общий контекст приложения."""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QPushButton, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import __version__, service as svc
from ..core import log
from . import theme
from .common import Page, UiBus, background
from .pages import AbilitiesPage, HearingPage, HomePage, ModelPage, VoicePage
from .pages2 import ChatPage, LogPage, SystemPage, TelegramPage
from .updates import UpdateController, auto_check_enabled

CHECK_EVERY_MS = 6 * 3600 * 1000


class Context(QObject):
    """Всё, что нужно страницам: ассистент, шина потоков, обновления."""
    state_changed = Signal(str, str)
    event = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.bus = UiBus(self)
        self.service = svc.AssistantService(
            on_state=lambda s, d: self.bus.post(lambda: self.state_changed.emit(s, d)),
            on_event=lambda k, t: self.bus.post(lambda: self.event.emit(k, t)))
        self.updates = UpdateController(self.bus)
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self.restart_service)

    def restart_service(self) -> None:
        """Применить настройки: остановка может занять несколько секунд, поэтому не в потоке окна."""
        background(self.bus, self.service.restart, lambda _r, _e: None)

    def restart_service_soon(self) -> None:
        # Если ассистент не запущен (пользователь остановил его сам), не запускаем без просьбы
        if self.service.running:
            self._restart_timer.start(600)


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, ctx: Context):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Джарвис")
        self.setWindowIcon(theme.make_icon("listening"))
        self.resize(1040, 720)
        self.setMinimumSize(880, 580)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- боковая панель
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(220)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 22, 0, 16)
        sl.setSpacing(0)
        brand = QLabel("ДЖАРВИС")
        brand.setObjectName("brand")
        brand.setContentsMargins(22, 0, 0, 0)
        sub = QLabel("голосовой помощник")
        sub.setObjectName("brandSub")
        sub.setContentsMargins(22, 0, 0, 14)
        sl.addWidget(brand)
        sl.addWidget(sub)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        sl.addWidget(self.nav, 1)
        foot = QHBoxLayout()
        foot.setContentsMargins(22, 0, 12, 0)
        foot.setSpacing(8)
        self.foot_dot = QLabel()
        self.foot_dot.setPixmap(theme.dot(theme.MUTED, 10))
        self.foot_text = QLabel("Остановлен")
        self.foot_text.setObjectName("muted")
        foot.addWidget(self.foot_dot)
        foot.addWidget(self.foot_text)
        foot.addStretch(1)
        sl.addLayout(foot)
        ver = QLabel(f"версия {__version__}")
        ver.setObjectName("muted")
        ver.setContentsMargins(22, 4, 0, 0)
        sl.addWidget(ver)
        outer.addWidget(side)

        # --- правая часть: баннер обновления + страницы
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(16, 10, 16, 10)
        self.banner_text = QLabel("")
        self.btn_banner = QPushButton("Установить")
        self.btn_banner.setObjectName("primary")
        self.btn_banner.setCursor(Qt.CursorShape.PointingHandCursor)
        later = QPushButton("Позже")
        later.setObjectName("link")
        later.setCursor(Qt.CursorShape.PointingHandCursor)
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.btn_banner)
        bl.addWidget(later)
        self.banner.hide()
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(24, 16, 24, 0)
        wl.addWidget(self.banner)
        rl.addWidget(wrap)
        self.stack = QStackedWidget()
        rl.addWidget(self.stack, 1)
        outer.addWidget(right, 1)

        self.pages: List[Page] = []
        for cls in (HomePage, ModelPage, VoicePage, HearingPage, AbilitiesPage, TelegramPage, ChatPage, LogPage, SystemPage):
            page = cls(ctx)
            self.pages.append(page)
            self.stack.addWidget(page)
            self.nav.addItem(QListWidgetItem(page.title))
        self.nav.currentRowChanged.connect(self.show_page)
        self.nav.setCurrentRow(0)

        ctx.state_changed.connect(self.on_state)
        ctx.event.connect(self.on_event)
        ctx.updates.changed.connect(self.on_update_changed)
        later.clicked.connect(self.banner.hide)
        self.btn_banner.clicked.connect(ctx.updates.install)

        # --- трей
        self.quitting = False
        self.tray: Optional[QSystemTrayIcon] = None
        self.told_tray = False
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.build_tray()

    # --- страницы -----------------------------------------------------------
    def show_page(self, row: int) -> None:
        if 0 <= row < len(self.pages):
            self.stack.setCurrentIndex(row)
            self.pages[row].load()

    def goto(self, title: str) -> None:
        for i, p in enumerate(self.pages):
            if p.title == title:
                self.nav.setCurrentRow(i)

    # --- события ассистента ---------------------------------------------------
    def on_state(self, state: str, detail: str) -> None:
        self.foot_dot.setPixmap(theme.dot(theme.STATE_COLOR.get(state, theme.MUTED), 10))
        self.foot_text.setText(svc.STATE_TEXT.get(state, state))
        for p in self.pages:
            if hasattr(p, "on_state"):
                p.on_state(state, detail)
        if self.tray is not None:
            self.tray.setIcon(theme.make_icon(state))
            self.tray.setToolTip(f"Джарвис: {svc.STATE_TEXT.get(state, state)}")
            self.pause_action.setText("Продолжить" if state == svc.PAUSED else "Пауза")
            self.pause_action.setEnabled(state in (svc.LISTENING, svc.PAUSED))

    def on_event(self, kind: str, text: str) -> None:
        for p in self.pages:
            if hasattr(p, "on_event"):
                p.on_event(kind, text)

    # --- обновления -----------------------------------------------------------
    def on_update_changed(self) -> None:
        u = self.ctx.updates
        show = bool(u.release and u.release.asset_url)
        self.banner.setVisible(show)
        if show:
            busy = u.busy
            self.banner_text.setText(f"Доступна новая версия {u.release.version}. {u.text if busy else ''}".strip())
            self.btn_banner.setEnabled(not busy)

    def start_update_checks(self) -> None:
        if not auto_check_enabled():
            return
        QTimer.singleShot(8000, self.ctx.updates.check)
        self._upd_timer = QTimer(self)
        self._upd_timer.setInterval(CHECK_EVERY_MS)
        self._upd_timer.timeout.connect(lambda: auto_check_enabled() and self.ctx.updates.check())
        self._upd_timer.start()

    # --- трей -----------------------------------------------------------------
    def build_tray(self) -> None:
        self.tray = QSystemTrayIcon(theme.make_icon("stopped"), self)
        self.tray.setToolTip("Джарвис")
        menu = QMenu()
        open_a = QAction("Открыть Джарвиса", self)
        self.pause_action = QAction("Пауза", self)
        restart_a = QAction("Перезапустить", self)
        quit_a = QAction("Выход", self)
        for a in (open_a, self.pause_action, restart_a):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(quit_a)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu
        open_a.triggered.connect(self.bring_to_front)
        self.pause_action.triggered.connect(
            lambda: self.ctx.service.set_paused(self.ctx.service.state != svc.PAUSED))
        restart_a.triggered.connect(self.ctx.restart_service)
        quit_a.triggered.connect(self.quit_requested.emit)
        self.tray.activated.connect(
            lambda reason: self.bring_to_front() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.tray is not None and self.tray.isVisible() and not self.quitting:
            event.ignore()
            self.hide()
            if not self.told_tray:
                self.told_tray = True
                self.tray.showMessage("Джарвис работает в фоне",
                                      "Значок остался в трее. Полностью закрыть: правой кнопкой → «Выход».",
                                      QSystemTrayIcon.MessageIcon.Information, 5000)
            return
        if not self.quitting:
            # Без значка в трее окно — единственный вход в приложение: закрытие означает выход
            self.quit_requested.emit()
        super().closeEvent(event)


def shutdown(app: QApplication, ctx: Context, window: Optional[MainWindow]) -> None:
    """Корректный выход: остановить микрофон, убрать значок из трея, закрыть окно."""
    if window is not None:
        window.quitting = True
        if window.tray is not None:
            window.tray.hide()

    def finish(_r=None, _e=None) -> None:
        log.info("Джарвис завершает работу")
        app.quit()
    background(ctx.bus, ctx.service.stop, finish)
