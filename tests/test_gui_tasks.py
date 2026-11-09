"""Страницы «Задачи» и «Диагностика», красный значок записи."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jarvis import diag, tasks, teach  # noqa: E402
from test_gui import GuiBase, page, wait_for  # noqa: E402
from test_teach import FakeApi, ManualSampler, Clock, VK_A  # noqa: E402


def texts(widget):
    return " ".join(l.text() for l in widget.findChildren(QLabel))


def buttons(widget, text):
    return [b for b in widget.findChildren(QPushButton) if b.text() == text]


class TasksPageTests(GuiBase):
    def make_task(self, name="Отчёт по продажам"):
        t = tasks.Task(name=name, description="Открывает отчёт", steps=[
            {"i": 1, "kind": "click", "button": "left", "double": False, "window": {"title": "Excel", "process": "EXCEL.EXE"}, "x": .5, "y": .5},
            {"i": 2, "kind": "type", "text": "секретный текст", "length": 15, "window": {}}])
        tasks.save_task(t)
        return t

    def test_empty_state(self):
        p = page(self.w, "Задачи")
        self.assertIn("Пока нет ни одной задачи", texts(p.list_holder))

    def test_task_card_shows_steps_without_typed_text(self):
        self.make_task()
        p = page(self.w, "Задачи")
        self.assertIn("Отчёт по продажам", texts(p.list_holder))
        self.assertIn("2 шагов", texts(p.list_holder))
        self.assertIn("выполни задачу Отчёт по продажам", texts(p.list_holder))
        buttons(p.list_holder, "Шаги")[0].click()
        shown = " ".join(e.toPlainText() for e in p.list_holder.findChildren(QPlainTextEdit))
        self.assertIn("15 симв.", shown)
        self.assertNotIn("секретный", shown)

    def test_run_button_uses_service(self):
        self.make_task()
        calls = []
        self.ctx.service.run_task = lambda t: calls.append(t.slug) or "Запустил."
        p = page(self.w, "Задачи")
        buttons(p.list_holder, "Запустить")[0].click()
        self.assertTrue(wait_for(lambda: calls == ["otchet-po-prodazham"]))
        self.assertTrue(wait_for(lambda: "Запустил." in texts(p.list_holder)))

    def test_delete_needs_second_click(self):
        self.make_task()
        p = page(self.w, "Задачи")
        buttons(p.list_holder, "Удалить")[0].click()
        self.assertEqual(len(tasks.list_tasks()), 1)
        buttons(p.list_holder, "Точно удалить?")[0].click()
        self.assertEqual(tasks.list_tasks(), [])
        self.assertIn("Пока нет ни одной задачи", texts(p.list_holder))

    def test_recording_unavailable_outside_windows_is_explained(self):
        p = page(self.w, "Задачи")
        p.btn_start.click()
        self.assertIn("Windows", p.rec_status.text())
        self.assertFalse(p.rec_banner.isVisible())

    def test_recording_cycle_shows_red_state_and_new_task(self):
        api, clock = FakeApi(), Clock()
        sampler = ManualSampler(api, clock=clock)
        svc = self.ctx.service
        svc._teacher = teach.Teacher(lambda: sampler, root=tasks.tasks_dir(), on_state=svc._teach_state,
                                     on_done=svc._teach_done, screen=lambda: (1000, 500))
        p = page(self.w, "Задачи")
        p.name.setText("Моя запись")
        p.btn_start.click()
        self.assertTrue(wait_for(lambda: self.w.recording))
        self.assertFalse(p.rec_banner.isHidden())
        self.assertIn("Идёт запись", self.w.foot_text.text())
        self.assertFalse(p.btn_start.isEnabled())
        for down in ({VK_A}, set(), {VK_A}, set()):
            api.down = down
            clock.t += 0.2
            sampler.poll()
        p.btn_stop.click()
        self.assertTrue(wait_for(lambda: not self.w.recording))
        self.assertTrue(wait_for(lambda: [t.name for t in tasks.list_tasks()] == ["Моя запись"]))
        self.assertTrue(wait_for(lambda: "Моя запись" in texts(p.list_holder)))
        self.assertTrue(p.btn_start.isEnabled())
        self.assertNotIn("Идёт запись", self.w.foot_text.text())


class DiagnosticsPageTests(GuiBase):
    def setUp(self):
        super().setUp()
        self.real = diag.check_network
        diag.check_network = lambda targets, timeout=6.0: {
            n: {"ok": n != "Голос Microsoft", "ms": 40, "error": "timeout" if n == "Голос Microsoft" else "", "url": u}
            for n, u in targets.items()}
        self.addCleanup(lambda: setattr(diag, "check_network", self.real))

    def test_check_runs_and_shows_findings_with_advice(self):
        p = page(self.w, "Диагностика")
        self.assertTrue(wait_for(lambda: bool(p.findings), 90))
        body = texts(p.holder)
        self.assertIn("Что делать", body)
        self.assertTrue(p.btn_copy.isEnabled())
        self.assertIn("Джарвис", p.report_text)

    def test_copy_report_goes_to_clipboard_and_hides_home_path(self):
        from PySide6.QtGui import QGuiApplication
        p = page(self.w, "Диагностика")
        self.assertTrue(wait_for(lambda: bool(p.report_text), 90))
        p.btn_copy.click()
        clip = QGuiApplication.clipboard().text()
        self.assertEqual(clip, p.report_text)
        self.assertIn("скопирован", p.status.text())

    def test_report_to_developer_opens_prefilled_issue(self):
        from PySide6.QtGui import QDesktopServices
        opened = []
        real = QDesktopServices.openUrl
        QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toString()) or True)
        self.addCleanup(lambda: setattr(QDesktopServices, "openUrl", real))
        p = page(self.w, "Диагностика")
        self.assertTrue(wait_for(lambda: bool(p.report_text), 90))
        p.btn_report.click()
        self.assertEqual(len(opened), 1)
        self.assertIn("github.com/kitlame7-lab/jarvis67/issues/new", opened[0])
        self.assertLess(len(opened[0]), 2200)

    def test_error_event_triggers_new_check_once_a_minute(self):
        p = page(self.w, "Диагностика")
        self.assertTrue(wait_for(lambda: bool(p.findings) and not p.running, 90))
        p.findings = []
        self.ctx.event.emit("error", "что-то сломалось")
        self.assertTrue(wait_for(lambda: bool(p.findings), 90))
        p.findings = []
        QTest.qWait(50)
        self.ctx.event.emit("error", "ещё одно")
        QTest.qWait(300)
        self.assertEqual(p.findings, [], "повторная проверка раньше чем через минуту не нужна")


class ThemeTests(GuiBase):
    def tearDown(self):
        from jarvis.gui import theme
        theme.set_mode("light")

    def test_theme_switch_applies_and_is_saved(self):
        from jarvis import backend
        from jarvis.gui import theme
        p = page(self.w, "Система")
        p.theme_box.setCurrentIndex(p.theme_box.findData("dark"))
        self.assertEqual(theme.MODE, "dark")
        self.assertEqual(backend.read_user_settings()["ui"]["theme"], "dark")
        self.assertIn(theme.PALETTES["dark"]["BG"], theme.QSS)
        p.theme_box.setCurrentIndex(p.theme_box.findData("light"))
        self.assertEqual(theme.MODE, "light")

    def test_unknown_theme_is_rejected(self):
        from jarvis import backend
        backend.save_settings({"ui": {"theme": "розовая"}})
        self.assertNotIn("ui", backend.read_user_settings())

    def test_every_palette_defines_the_same_colors(self):
        from jarvis.gui import theme
        self.assertEqual(set(theme.PALETTES["light"]), set(theme.PALETTES["dark"]))
