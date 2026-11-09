"""Страницы: Задачи (запись действий), Подключения (модель и Telegram), Диагностика."""
from __future__ import annotations

import html
import json
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget

from .. import diag, tasks, trace
from . import theme
from .common import Card, Page, Status, background, field, note
from .pages import ModelPage, button, primary
from .pages2 import LogPage, TelegramPage


def _last_run(task: tasks.Task) -> str:
    try:
        data = json.loads((tasks.tasks_dir() / task.slug / "last_run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    when = str(data.get("at", ""))[:16].replace("T", " ")
    return f"последний запуск {when}: {'успешно' if data.get('ok') else 'остановлен'}"


# --------------------------------------------------------------------------
# Задачи
# --------------------------------------------------------------------------

class TasksPage(Page):
    title = "Задачи"
    lead = ("Покажите Джарвису один раз, что делать, и он повторит это сам. Скажите «Джарвис, запомни как я это делаю», "
            "выполните действия и скажите «стоп».")

    def build(self) -> None:
        rec = Card("Записать новую задачу",
                   "Джарвис запомнит щелчки, нажатия клавиш и смену окон, а название и фразы вызова придумает сам. "
                   "Текст из окон с паролями не сохраняется.")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Например: отчёт по продажам")
        rec.add(field("Название (можно не указывать)", self.name))
        self.btn_start = button("Начать запись", "rec")
        self.btn_stop = primary("Остановить и сохранить")
        self.btn_cancel = button("Отменить запись")
        rec.row(self.btn_start, self.btn_stop, self.btn_cancel)
        self.rec_banner = QFrame()
        self.rec_banner.setObjectName("rec")
        bl = QHBoxLayout(self.rec_banner)
        bl.setContentsMargins(16, 10, 16, 10)
        self.rec_text = QLabel("")
        bl.addWidget(self.rec_text)
        self.rec_banner.hide()
        rec.add(self.rec_banner)
        self.rec_status = Status()
        rec.add(self.rec_status)
        self.col.addWidget(rec)

        self.list_holder = QWidget()
        self.list_lay = QVBoxLayout(self.list_holder)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(12)
        self.col.addWidget(self.list_holder)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.btn_start.clicked.connect(self.start_recording)
        self.btn_stop.clicked.connect(self.stop_recording)
        self.btn_cancel.clicked.connect(self.cancel_recording)
        self._pending_delete: Optional[str] = None
        self.on_recording(False)

    # --- запись ------------------------------------------------------------------
    def start_recording(self) -> None:
        teacher = self.ctx.service.teacher
        if teacher is None:
            self.rec_status.warn("Запись действий выключена в настройках (teach.enabled).")
            return
        if not teacher.start(self.name.text().strip()):
            self.rec_status.bad(teacher.last_error or "Не получилось начать запись. Подробности в разделе «Диагностика».")
            return
        self.rec_status.info("")
        self.window().showMinimized()          # окно Джарвиса не должно мешать тому, что вы показываете

    def stop_recording(self) -> None:
        teacher = self.ctx.service.teacher
        if teacher and teacher.stop(self.name.text().strip()):
            self.rec_status.info("Собираю задачу… Это займёт несколько секунд.")
            self.name.clear()

    def cancel_recording(self) -> None:
        teacher = self.ctx.service.teacher
        if teacher and teacher.discard():
            self.rec_status.info("Запись отменена.")

    def on_recording(self, recording: bool) -> None:
        self.btn_start.setEnabled(not recording)
        self.name.setEnabled(not recording)
        self.btn_stop.setEnabled(recording)
        self.btn_cancel.setEnabled(recording)
        self.rec_banner.setVisible(recording)
        if recording:
            self.tick()
            self.timer.start()
        else:
            self.timer.stop()

    def tick(self) -> None:
        teacher = self.ctx.service._teacher
        stats = teacher.stats() if teacher else {"seconds": 0, "events": 0}
        sec = int(stats["seconds"])
        self.rec_text.setText(f'<span style="color:{theme.REC}">●</span> Идёт запись: {sec // 60}:{sec % 60:02d}. '
                              "Делайте нужные действия и скажите «стоп».")

    def on_event(self, kind: str, text: str) -> None:
        if kind == "tasks":
            self.reload()
            if text:
                self.rec_status.ok(text)

    # --- список ----------------------------------------------------------------------
    def load(self) -> None:
        self.reload()

    def reload(self) -> None:
        while self.list_lay.count():
            item = self.list_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        items = tasks.list_tasks()
        if not items:
            self.list_lay.addWidget(Card("Пока нет ни одной задачи",
                                         "Когда запишете первую, она появится здесь. Запускать можно голосом: "
                                         "«Джарвис, выполни задачу …»."))
            return
        for task in items:
            self.list_lay.addWidget(self._card(task))

    def _card(self, task: tasks.Task) -> Card:
        card = Card(task.name)
        card.setProperty("slug", task.slug)
        meta = [f"{len(task.steps)} шагов", f"создана {task.created[:16].replace('T', ' ')}"]
        if _last_run(task):
            meta.append(_last_run(task))
        card.add(note(" · ".join(meta)))
        if task.description:
            card.add(note(task.description))
        card.add(note(f"Голосом: «Джарвис, выполни задачу {task.name}»"))
        run, show, delete = primary("Запустить"), button("Шаги"), button("Удалить", "danger")
        card.row(run, show, delete)
        status = Status()
        card.add(status)
        steps = QPlainTextEdit()
        steps.setReadOnly(True)
        steps.setMaximumHeight(220)
        steps.setPlainText("\n".join(f"{s.get('i', n)}. {trace.describe_step(s)}"
                                     + (f" — {s['note']}" if s.get("note") else "")
                                     for n, s in enumerate(task.steps, 1)))
        steps.hide()
        card.add(steps)
        run.clicked.connect(lambda: self.run_task(task, status))
        show.clicked.connect(lambda: (steps.setVisible(steps.isHidden()), show.setText("Скрыть" if not steps.isHidden() else "Шаги")))
        delete.clicked.connect(lambda: self.delete_task(task, delete, status))
        return card

    def run_task(self, task: tasks.Task, status: Status) -> None:
        status.info("Запускаю…")
        background(self.ctx.bus, lambda: self.ctx.service.run_task(task),
                   lambda msg, err: status.bad(str(err)) if err else status.ok(str(msg)))

    def delete_task(self, task: tasks.Task, btn: Any, status: Status) -> None:
        # Два нажатия вместо окна подтверждения: так ничего не удалится случайно и не зависнет диалог
        if self._pending_delete != task.slug:
            self._pending_delete = task.slug
            btn.setText("Точно удалить?")
            QTimer.singleShot(4000, lambda: (setattr(self, "_pending_delete", None), btn.setText("Удалить"))
                              if self._pending_delete == task.slug and btn.parent() else None)
            return
        self._pending_delete = None
        if tasks.delete_task(task.slug):
            self.reload()
        else:
            status.bad("Не удалось удалить папку задачи.")


# --------------------------------------------------------------------------
# Подключения: модель и Telegram на одной странице
# --------------------------------------------------------------------------

class ConnectionsPage(ModelPage, TelegramPage):
    title = "Подключения"
    lead = ("Откуда Джарвис берёт ум (модель Hermes) и как с ним связаться с телефона (Telegram). "
            "Ключи сохраняются в самом Hermes: вставлять их нужно только здесь, в окне программы.")

    def build(self) -> None:
        ModelPage.build(self)
        TelegramPage.build(self)

    def load(self) -> None:
        ModelPage.load(self)
        TelegramPage.load(self)


# --------------------------------------------------------------------------
# Диагностика
# --------------------------------------------------------------------------

LEVEL_STYLE = {"error": ("findingBad", theme.BAD, "Ошибка"), "warn": ("findingWarn", theme.WARN, "Внимание"),
               "info": ("finding", theme.ACCENT, "К сведению"), "ok": ("finding", theme.OK, "В порядке")}


class DiagnosticsPage(LogPage):
    title = "Диагностика"
    lead = ("Находит, почему что-то не работает: сеть, модель речи, микрофон, Hermes, место на диске. "
            "Проверка запускается сама, а при сбое отчёт можно отправить разработчику одним нажатием.")

    def build(self) -> None:
        card = Card("Проверка")
        self.btn_run = primary("Проверить сейчас")
        self.btn_copy = button("Скопировать отчёт")
        self.btn_report = button("Сообщить разработчику")
        card.row(self.btn_run, self.btn_copy, self.btn_report)
        self.status = Status()
        card.add(self.status)
        self.col.addWidget(card)
        self.holder = QWidget()
        self.holder_lay = QVBoxLayout(self.holder)
        self.holder_lay.setContentsMargins(0, 0, 0, 0)
        self.holder_lay.setSpacing(10)
        self.col.addWidget(self.holder)
        self.data: Dict[str, Any] = {}
        self.findings: List[diag.Finding] = []
        self.report_text = ""
        self.running = False
        self._ran_once = False
        self._last_auto = 0.0
        self.btn_run.clicked.connect(lambda: self.run_check(manual=True))
        self.btn_copy.clicked.connect(self.copy_report)
        self.btn_report.clicked.connect(self.send_report)
        self.btn_copy.setEnabled(False)
        self.btn_report.setEnabled(False)
        LogPage.build(self)

    def load(self) -> None:
        LogPage.load(self)
        if not self._ran_once:
            self.run_check()

    def on_event(self, kind: str, text: str) -> None:
        # Сбой во время работы: сразу ищем причину, но не чаще раза в минуту
        if kind == "error" and time.monotonic() - self._last_auto > 60 and not self.running:
            self._last_auto = time.monotonic()
            self.run_check()

    def run_check(self, manual: bool = False) -> None:
        if self.running:
            return
        self.running = True
        self._ran_once = True
        self.btn_run.setEnabled(False)
        self.status.info("Проверяю сеть, модель, микрофон и Hermes…")
        state = (self.ctx.service.state, self.ctx.service.detail)

        def work() -> Dict[str, Any]:
            return diag.gather(service_state=state)

        def done(data: Any, err: Optional[BaseException]) -> None:
            self.running = False
            self.btn_run.setEnabled(True)
            if err:
                self.status.bad(f"Сама проверка не удалась: {type(err).__name__}: {err}")
                return
            self.show_result(data)
        background(self.ctx.bus, work, done)

    def show_result(self, data: Dict[str, Any]) -> None:
        self.data = data
        self.findings = diag.analyze(data)
        self.report_text = diag.render_text(data, self.findings)
        while self.holder_lay.count():
            item = self.holder_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        problems = [f for f in self.findings if f.level in ("error", "warn")]
        for f in self.findings:
            self.holder_lay.addWidget(self._finding(f))
        if problems:
            self.status.warn(f"Найдено проблем: {len(problems)}. Ниже подробности и что делать.")
        else:
            self.status.ok("Проблем не найдено.")
        self.btn_copy.setEnabled(True)
        self.btn_report.setEnabled(True)

    def _finding(self, f: diag.Finding) -> QFrame:
        name, color, label = LEVEL_STYLE.get(f.level, LEVEL_STYLE["info"])
        box = QFrame()
        box.setObjectName(name)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(4)
        head = QLabel(f'<span style="color:{color}">●</span> <b>{html.escape(f.title)}</b>')
        head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(head)
        if f.detail:
            lay.addWidget(note(f.detail))
        if f.fix:
            fix = QLabel(f"<b>Что делать:</b> {html.escape(f.fix)}")
            fix.setTextFormat(Qt.TextFormat.RichText)
            fix.setWordWrap(True)
            lay.addWidget(fix)
        box.setProperty("level", f.level)
        return box

    def copy_report(self) -> None:
        QGuiApplication.clipboard().setText(self.report_text)
        self.status.ok("Отчёт скопирован. Секреты (ключи, пути с именем пользователя) в нём скрыты.")

    def send_report(self) -> None:
        QGuiApplication.clipboard().setText(self.report_text)
        QDesktopServices.openUrl(QUrl(diag.issue_url(self.findings, self.report_text)))
        self.status.ok("Открыл страницу обращения в браузере. Полный отчёт уже в буфере: вставьте его в поле описания.")

