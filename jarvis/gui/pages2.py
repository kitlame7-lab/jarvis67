"""Страницы: Telegram (часть «Подключений»), Журнал, Система."""
from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QWidget)

from .. import REPO, __version__, backend, core, paths
from . import theme
from .common import Card, Page, SecretEdit, Status, background, field, note
from .pages import button, primary


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

class TelegramPage(Page):
    """Часть страницы «Подключения»: Telegram-бот, через которого можно писать Джарвису с телефона."""

    def build(self) -> None:
        card = Card("Telegram: писать Джарвису с телефона",
                    "Бот отвечает так же, как на голос. Работает через шлюз Hermes, который запускается отдельно.")
        self.token = SecretEdit("123456:ABC…")
        card.add(field("Токен бота", self.token, "Создайте бота у @BotFather в Telegram командой /newbot и скопируйте токен."))
        self.token_state = note("")
        card.add(self.token_state)
        self.users = QLineEdit()
        self.users.setPlaceholderText("123456789, 987654321")
        card.add(field("Кому можно писать боту (числовые ID через запятую)", self.users,
                       "Свой ID покажет бот @userinfobot. Без списка бот никому не отвечает, это защита от чужих."))
        self.col.addWidget(card)

        act = Card()
        self.tg_save = button("Сохранить")
        self.tg_apply = primary("Сохранить и перезапустить шлюз")
        act.row(self.tg_save, self.tg_apply)
        self.tg_status = Status()
        act.add(self.tg_status)
        self.tg_out = QPlainTextEdit()
        self.tg_out.setReadOnly(True)
        self.tg_out.setMaximumHeight(160)
        self.tg_out.hide()
        act.add(self.tg_out)
        self.col.addWidget(act)
        self.tg_save.clicked.connect(lambda: self.save_tg(False))
        self.tg_apply.clicked.connect(lambda: self.save_tg(True))

    def load(self) -> None:
        snap = backend.hermes_snapshot()
        self.token_state.setText(self.token.describe(snap["telegram"]["token"]))
        self.users.setText(snap["telegram"]["users"])

    def save_tg(self, restart: bool) -> None:
        try:
            backend.save_telegram(self.token.value(), self.users.text())
        except ValueError as exc:
            self.tg_status.bad(str(exc))
            return
        self.token.clear()
        self.load()
        if not restart:
            self.tg_status.ok("Сохранено. Чтобы бот заработал, перезапустите шлюз.")
            return
        self.tg_apply.setEnabled(False)
        self.tg_out.clear()
        self.tg_out.show()
        self.tg_status.info("Перезапускаю шлюз…")

        def work():
            return backend.restart_gateway(lambda line: self.ctx.bus.post(lambda l=line: self.tg_out.appendPlainText(l)))

        def done(msg, err):
            self.tg_apply.setEnabled(True)
            self.tg_status.bad(str(err)) if err else self.tg_status.ok(str(msg))
        background(self.ctx.bus, work, done)


# --------------------------------------------------------------------------
# Журнал
# --------------------------------------------------------------------------

class LogPage(Page):
    title = "Журнал"
    lead = "Что происходило внутри. Если что-то не работает, отправьте этот текст разработчику."

    def build(self) -> None:
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(420)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.col.addWidget(self.text, 1)
        row = QHBoxLayout()
        b1, b2 = button("Копировать всё"), button("Открыть папку с данными")
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        w = QWidget()
        w.setLayout(row)
        self.col.addWidget(w)
        b1.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.read()))
        b2.clicked.connect(lambda: open_folder(paths.DATA_DIR))
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)

    def read(self, limit: int = 400) -> str:
        try:
            lines = paths.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "Журнал пока пуст."
        return "\n".join(lines[-limit:])

    def refresh(self) -> None:
        text = self.read()
        if text != self.text.toPlainText():
            bar = self.text.verticalScrollBar()
            stick = bar.value() >= bar.maximum() - 4
            self.text.setPlainText(text)
            if stick:
                bar.setValue(bar.maximum())

    def load(self) -> None:
        self.refresh()
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def showEvent(self, e) -> None:  # noqa: N802
        self.timer.start()
        super().showEvent(e)

    def hideEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(e)


# --------------------------------------------------------------------------
# Система
# --------------------------------------------------------------------------

class SystemPage(Page):
    title = "Система"
    lead = "Проверка всей цепочки, обновления и служебные действия."

    def build(self) -> None:
        h = Card("Hermes")
        self.hermes_line = QLabel("")
        self.hermes_line.setWordWrap(True)
        h.add(self.hermes_line)
        self.skill_line = note("")
        h.add(self.skill_line)
        self.btn_skill = button("Переустановить навык jarvis-pc")
        h.row(self.btn_skill)
        self.col.addWidget(h)

        t = Card("Проверка", "Проходит по всей цепочке: библиотеки, микрофон, голос, Hermes, модель и навык. Занимает до пары минут.")
        self.btn_test = primary("Проверить всё")
        self.btn_prepare = button("Скачать модель распознавания")
        t.row(self.btn_test, self.btn_prepare)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMinimumHeight(200)
        t.add(self.out)
        self.col.addWidget(t)

        u = Card("Обновления")
        self.upd_line = QLabel("")
        self.upd_line.setWordWrap(True)
        u.add(self.upd_line)
        self.notes = note("")
        u.add(self.notes)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.hide()
        u.add(self.bar)
        self.btn_check = button("Проверить обновления")
        self.btn_install = primary("Установить обновление")
        self.btn_install.hide()
        u.row(self.btn_check, self.btn_install)
        self.auto = QCheckBox("Проверять обновления автоматически")
        u.add(self.auto)
        self.col.addWidget(u)

        look = Card("Оформление")
        self.theme_box = QComboBox()
        self.theme_box.addItem("Светлое", "light")
        self.theme_box.addItem("Тёмное", "dark")
        look.add(field("Тема", self.theme_box))
        self.col.addWidget(look)

        o = Card("Прочее")
        b1 = button("Открыть папку с данными")
        b2 = button("Страница проекта на GitHub")
        o.row(b1, b2)
        o.add(note(f"Джарвис {__version__} · данные: {paths.DATA_DIR}"))
        self.col.addWidget(o)

        self.btn_skill.clicked.connect(self.reinstall_skill)
        self.btn_test.clicked.connect(self.run_test)
        self.btn_prepare.clicked.connect(self.run_prepare)
        self.btn_check.clicked.connect(lambda: self.ctx.updates.check(manual=True))
        self.btn_install.clicked.connect(self.ctx.updates.install)
        self.auto.toggled.connect(self.set_auto)
        self.theme_box.currentIndexChanged.connect(self.set_theme)
        b1.clicked.connect(lambda: open_folder(paths.DATA_DIR))
        b2.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"https://github.com/{REPO}")))
        self.ctx.updates.changed.connect(self.refresh_updates)
        self._loading = False

    def load(self) -> None:
        snap = backend.hermes_snapshot()
        if snap["exe"]:
            self.hermes_line.setText(f'<span style="color:{theme.OK}">●</span> Hermes найден: {html.escape(snap["exe"])}')
        else:
            self.hermes_line.setText(f'<span style="color:{theme.BAD}">●</span> Hermes не найден. Установите Hermes Agent и перезапустите Джарвиса.')
        st = backend.skill_state()
        self.skill_line.setText("Навык jarvis-pc установлен и актуален." if st["installed"] and st["current"]
                                else "Навык jarvis-pc устарел или не установлен. Нажмите «Переустановить».")
        self._loading = True
        self.auto.setChecked(bool(core.load_settings()["updates"]["auto_check"]))
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(core.load_settings()["ui"]["theme"])))
        self._loading = False
        self.refresh_updates()

    def set_theme(self) -> None:
        if self._loading:
            return
        mode = self.theme_box.currentData()
        backend.save_settings({"ui": {"theme": mode}})
        win = self.window()
        if hasattr(win, "apply_theme"):
            win.apply_theme(mode)

    def set_auto(self, on: bool) -> None:
        if not self._loading:
            backend.save_settings({"updates": {"auto_check": on}})

    def refresh_updates(self) -> None:
        u = self.ctx.updates
        color = theme.BAD if u.error else (theme.ACCENT if u.release else theme.TEXT)
        self.upd_line.setText(f'<span style="color:{color}">{html.escape(u.text)}</span>')
        self.notes.setText((u.release.notes[:600] if u.release else ""))
        self.btn_install.setVisible(bool(u.release) and bool(u.release.asset_url))
        self.btn_install.setEnabled(not u.busy)
        self.btn_check.setEnabled(not u.busy)
        done, total = u.progress
        self.bar.setVisible(u.busy and total > 0)
        if total:
            self.bar.setValue(done * 100 // total)

    def reinstall_skill(self) -> None:
        try:
            backend.install_skill()
        except Exception as exc:  # noqa: BLE001
            self.skill_line.setText(f"Не получилось: {exc}")
            return
        self.load()

    def _stream(self, fn, buttons) -> None:
        for b in buttons:
            b.setEnabled(False)
        self.out.clear()

        def emit(line: str) -> None:
            self.ctx.bus.post(lambda: self.out.appendPlainText(line))

        def done(_r, err):
            for b in buttons:
                b.setEnabled(True)
            if err:
                self.out.appendPlainText(f"Сбой: {type(err).__name__}: {err}")
        background(self.ctx.bus, lambda: fn(core.load_settings(), emit), done)

    def run_test(self) -> None:
        self._stream(core.run_selftest, (self.btn_test, self.btn_prepare))

    def run_prepare(self) -> None:
        self._stream(core.run_prepare, (self.btn_test, self.btn_prepare))
