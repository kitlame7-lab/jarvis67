"""Окно целиком, без экрана: нажимаем кнопки и смотрим, что записалось и что показано."""
import json
import time
import unittest
from pathlib import Path

import pytest

from helpers import STUB
from jarvis import backend, paths, updater

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jarvis.gui import theme  # noqa: E402
from jarvis.gui.window import Context, MainWindow  # noqa: E402
from test_backend import FakeOpenAI  # noqa: E402

app = QApplication.instance() or QApplication([])
app.setStyleSheet(theme.QSS)


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        QTest.qWait(30)
        if cond():
            return True
    return False


def page(w, title):
    for i, p in enumerate(w.pages):
        if p.title == title:
            w.nav.setCurrentRow(i)
            QTest.qWait(20)
            return p
    raise KeyError(title)


class GuiBase(unittest.TestCase):
    def setUp(self):
        # Hermes подставной, голос и микрофон выключены
        backend.save_settings({"greeting": False})
        data = json.loads(paths.SETTINGS_PATH.read_text(encoding="utf-8"))
        data["hermes"] = dict(data.get("hermes", {}), exe=str(STUB))
        paths.SETTINGS_PATH.write_text(json.dumps(data), encoding="utf-8")
        self.ctx = Context()
        self.w = MainWindow(self.ctx)
        self.w.show()
        QTest.qWait(50)
        self.addCleanup(self.close)

    def close(self):
        self.ctx.service.stop()
        self.w.quitting = True
        self.w.close()
        QTest.qWait(30)


class WindowTests(GuiBase):
    def test_every_page_opens(self):
        titles = [p.title for p in self.w.pages]
        self.assertEqual(titles, ["Главное", "Задачи", "Голос", "Слух", "Возможности", "Подключения", "Диагностика", "Система"])
        for t in titles:
            page(self.w, t)

    def test_no_web_or_eleven_leftovers_in_texts(self):
        from PySide6.QtWidgets import QLabel
        text = " ".join(l.text() for p in self.w.pages for l in p.findChildren(QLabel)).lower()
        self.assertNotIn("elevenlabs", text)
        self.assertNotIn("localhost", text)
        self.assertNotIn(".bat", text)

    def test_close_without_tray_really_closes(self):
        self.assertIsNone(self.w.tray)          # в тестовой среде трея нет
        asked = []
        self.w.quit_requested.connect(lambda: asked.append(1))
        self.w.close()
        self.assertEqual(asked, [1])            # и приложение завершается, а не висит без окна
        QTest.qWait(20)
        self.assertFalse(self.w.isVisible())

    def test_state_reaches_home_page_and_sidebar(self):
        self.ctx.state_changed.emit("listening", "")
        home = page(self.w, "Главное")
        self.assertEqual(home.state.text(), "Слушаю")
        self.assertEqual(self.w.foot_text.text(), "Слушаю")
        self.assertTrue(home.btn_stop.isEnabled())
        self.assertFalse(home.btn_start.isEnabled())
        self.ctx.state_changed.emit("paused", "")
        self.assertEqual(home.btn_pause.text(), "Продолжить")

    def test_home_shows_last_conversation(self):
        home = page(self.w, "Главное")
        self.ctx.event.emit("heard", "какая погода")
        self.ctx.event.emit("reply", "Ясно, сэр")
        self.assertEqual(home.heard.text(), "какая погода")
        self.assertEqual(home.reply.text(), "Ясно, сэр")

    def test_greeting_checkbox_saves(self):
        home = page(self.w, "Главное")
        home.greeting.setChecked(True)
        self.assertTrue(backend.read_user_settings()["greeting"])


class ModelPageTests(GuiBase):
    def setUp(self):
        super().setUp()
        self.srv = FakeOpenAI()
        self.addCleanup(self.srv.close)
        self.p = page(self.w, "Подключения")

    def choose(self, pid):
        self.p.provider.setCurrentIndex(self.p.provider.findData(pid))

    def test_custom_fields_visible_only_for_custom(self):
        self.choose("custom")
        self.assertFalse(self.p.url_row.isHidden())
        self.assertFalse(self.p.key_row.isHidden())
        self.assertFalse(self.p.btn_list.isHidden())
        self.choose("deepseek")
        self.assertTrue(self.p.url_row.isHidden())
        self.assertTrue(self.p.btn_list.isHidden())
        self.assertFalse(self.p.key_row.isHidden())
        self.choose("")
        self.assertTrue(self.p.box.isHidden())
        self.choose("__other__")
        self.assertFalse(self.p.other_row.isHidden())

    def test_save_custom_server(self):
        self.choose("custom")
        self.p.url.setText(self.srv.url)
        self.p.key.setText("sk-good")
        self.p.model.setEditText("gpt-x")
        self.p.btn_save.click()
        self.assertIn("Сохранено", self.p.status.text())
        now = backend.read_hermes_model(backend.hermes_home())
        self.assertEqual((now["provider"], now["model"], now["base_url"]), ("custom", "gpt-x", self.srv.url))
        self.assertEqual(self.p.key.text(), "")                       # поле очищено
        self.assertIn("Ключ сохранён", self.p.key_state.text())

    def test_validation_message_in_window(self):
        self.choose("custom")
        self.p.url.setText("не адрес")
        self.p.key.setText("k")
        self.p.model.setEditText("m")
        self.p.btn_save.click()
        self.assertIn("http", self.p.status.text())

    def test_save_and_check_ok(self):
        self.choose("custom")
        self.p.url.setText(self.srv.url)
        self.p.key.setText("sk-good")
        self.p.model.setEditText("gpt-x")
        self.p.btn_check.click()
        self.assertTrue(wait_for(lambda: "Всё работает" in self.p.status.text()), self.p.status.text())
        self.assertIn("Сервер отвечает", self.p.status.text())
        self.assertTrue(self.p.btn_save.isEnabled())

    def test_check_reports_bad_key_from_server(self):
        self.choose("custom")
        self.p.url.setText(self.srv.url)
        self.p.key.setText("sk-wrong")
        self.p.model.setEditText("gpt-x")
        self.p.btn_check.click()
        self.assertTrue(wait_for(lambda: "не принял ключ" in self.p.status.text()), self.p.status.text())
        self.assertTrue(self.p.btn_check.isEnabled())

    def test_check_uses_stored_key_when_field_empty(self):
        backend.save_model_choice("custom", "gpt-x", api_key="sk-good", base_url=self.srv.url)
        self.p.load()
        self.assertEqual(self.p.provider.currentData(), "custom")
        self.assertEqual(self.p.url.text(), self.srv.url)
        self.assertEqual(self.p.model.currentText(), "gpt-x")
        self.p.btn_check.click()
        self.assertTrue(wait_for(lambda: "Всё работает" in self.p.status.text()), self.p.status.text())

    def test_list_models_button(self):
        self.choose("custom")
        self.p.url.setText(self.srv.url)
        self.p.key.setText("sk-good")
        self.p.btn_list.click()
        self.assertTrue(wait_for(lambda: "Моделей на сервере: 2" in self.p.status.text()), self.p.status.text())
        self.assertEqual([self.p.model.itemText(i) for i in range(self.p.model.count())], ["claude-y", "gpt-x"])

    def test_builtin_provider_saves_override(self):
        self.choose("deepseek")
        self.p.key.setText("sk-ds")
        self.p.model.setEditText("deepseek-chat")
        self.p.btn_save.click()
        self.assertEqual(backend.read_user_settings()["hermes"]["provider"], "deepseek")
        self.assertEqual(backend.read_env_file(backend.hermes_home() / ".env")["DEEPSEEK_API_KEY"], "sk-ds")

    def test_switching_back_to_hermes_clears_override(self):
        backend.save_model_choice("deepseek", "deepseek-chat", api_key="k")
        self.p.load()
        self.assertEqual(self.p.provider.currentData(), "deepseek")
        self.choose("")
        self.p.btn_save.click()
        self.assertEqual(backend.read_user_settings()["hermes"]["provider"], "")

    def test_password_field_is_masked(self):
        from PySide6.QtWidgets import QLineEdit
        self.assertEqual(self.p.key.echoMode(), QLineEdit.EchoMode.Password)


class VoiceHearingAbilitiesTests(GuiBase):
    def test_voice_saved(self):
        p = page(self.w, "Голос")
        wait_for(lambda: p.voice.count() > 0 and p.voice.itemText(0) != "Загружаю список голосов…", 15)
        i = p.voice.findData("ru-RU-SvetlanaNeural")
        self.assertGreaterEqual(i, 0)
        p.voice.setCurrentIndex(i)
        p.rate.setValue(10)
        p.pitch.setValue(-5)
        p.address.setText("босс")
        p.btn_save.click()
        s = backend.read_user_settings()
        self.assertEqual(s["tts"]["voice"], "ru-RU-SvetlanaNeural")
        self.assertEqual((s["tts"]["rate"], s["tts"]["pitch"]), ("+10%", "-5Hz"))
        self.assertEqual(s["address"], "босс")
        self.assertEqual(p.rate_lab.text(), "+10%")

    def test_voice_page_has_no_elevenlabs(self):
        p = page(self.w, "Голос")
        self.assertFalse(hasattr(p, "eleven_key"))

    def test_voice_preview_failure_shown(self):
        p = page(self.w, "Голос")
        real = backend.preview_voice
        backend.preview_voice = lambda cfg, text: (_ for _ in ()).throw(RuntimeError("голос Microsoft не отвечает: тест"))
        try:
            p.btn_preview.click()
            self.assertTrue(wait_for(lambda: "не отвечает" in p.status.text()))
        finally:
            backend.preview_voice = real
        self.assertTrue(p.btn_preview.isEnabled())

    def test_hearing_saved_and_validated(self):
        p = page(self.w, "Слух")
        p.words.setText("джарвис, jarvis , ")
        p.follow.setValue(20)
        p.stt.setCurrentIndex(p.stt.findData("base"))
        p.auto.setChecked(False)
        p.thr.setValue(30)
        p.btn_save.click()
        s = backend.read_user_settings()
        self.assertEqual(s["wake"]["words"], ["джарвис", "jarvis"])
        self.assertEqual(s["wake"]["followup_seconds"], 20)
        self.assertEqual(s["stt"]["model"], "base")
        self.assertFalse(s["audio"]["auto_threshold"])
        self.assertAlmostEqual(s["audio"]["silence_threshold"], 0.03)
        p.words.setText(" , ")
        p.btn_save.click()
        self.assertIn("хотя бы одно", p.status.text())

    def test_hearing_loads_saved_values(self):
        backend.save_settings({"stt": {"model": "medium"}, "wake": {"words": ["альфа", "бета"]}})
        p = page(self.w, "Слух")
        self.assertEqual(p.stt.currentData(), "medium")
        self.assertEqual(p.words.text(), "альфа, бета")

    def test_abilities_roundtrip(self):
        p = page(self.w, "Возможности")
        p.city.setText("Казань")
        p.kill.setPlainText("a.exe\n\n b.exe ")
        p.btn_add.click()
        from PySide6.QtWidgets import QTableWidgetItem
        p.table.setItem(0, 0, QTableWidgetItem("Блокнот"))
        p.table.setItem(0, 1, QTableWidgetItem("notepad"))
        p.btn_add.click()                         # пустая строка не сохраняется
        p.yolo.setChecked(True)
        p.btn_save.click()
        s = backend.read_user_settings()
        self.assertEqual(s["city"], "Казань")
        self.assertEqual(s["game_mode"]["kill"], ["a.exe", "b.exe"])
        self.assertEqual(s["apps"], {"блокнот": "notepad"})
        self.assertTrue(s["hermes"]["yolo"])
        p.load()
        self.assertEqual(p.table.rowCount(), 1)
        p.table.selectRow(0)
        p.btn_del.click()
        self.assertEqual(p.table.rowCount(), 0)


class TelegramChatLogSystemTests(GuiBase):
    def test_telegram_save(self):
        p = page(self.w, "Подключения")
        p.token.setText("123:ABC")
        p.users.setText("111, 222")
        p.tg_save.click()
        env = backend.read_env_file(backend.hermes_home() / ".env")
        self.assertEqual((env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_ALLOWED_USERS"]), ("123:ABC", "111,222"))
        self.assertEqual(p.token.text(), "")
        self.assertIn("Ключ сохранён", p.token_state.text())
        p.users.setText("не цифры")
        p.tg_save.click()
        self.assertIn("цифры", p.tg_status.text())

    def test_home_text_command_roundtrip_through_hermes(self):
        p = page(self.w, "Главное")
        p.cmd.setText("покажи погоду")
        p.btn_send.click()
        self.assertTrue(wait_for(lambda: "Выполнено" in p.reply.text()))
        self.assertEqual(p.heard.text(), "покажи погоду")
        self.assertTrue(p.btn_send.isEnabled())
        self.assertEqual(p.cmd.text(), "")

    def test_home_progress_bar_follows_download(self):
        p = page(self.w, "Главное")
        self.assertTrue(p.progress.isHidden())
        self.ctx.progress.emit(0.42, "Скачиваю")
        self.assertFalse(p.progress.isHidden())
        self.assertEqual(p.progress.value(), 420)
        self.ctx.progress.emit(-1.0, "")
        self.assertTrue(p.progress.isHidden())

    def test_log_page_shows_log(self):
        paths.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        paths.LOG_PATH.write_text("12:00:00 INFO Слушаю\n", encoding="utf-8")
        p = page(self.w, "Диагностика")
        self.assertIn("Слушаю", p.text.toPlainText())

    def test_system_page_status(self):
        p = page(self.w, "Система")
        self.assertIn("Hermes найден", p.hermes_line.text())
        self.assertIn("Навык", p.skill_line.text())
        p.btn_skill.click()
        self.assertIn("актуален", p.skill_line.text())

    def test_system_selftest_streams_output(self):
        p = page(self.w, "Система")
        p.btn_test.click()
        self.assertTrue(wait_for(lambda: "Проверка Джарвиса" in p.out.toPlainText()))
        self.assertTrue(wait_for(lambda: "Не прошло" in p.out.toPlainText() or "Всё в порядке" in p.out.toPlainText(), 120))
        self.assertTrue(wait_for(lambda: p.btn_test.isEnabled()))      # кнопка возвращается чуть позже вывода
        self.assertIn("Hermes найден", p.out.toPlainText())


class UpdateUiTests(GuiBase):
    REL = updater.Release("9.9.9", "Новое: окно", "https://x", "https://dl/Jarvis-Setup-9.9.9.exe",
                          "Jarvis-Setup-9.9.9.exe", 10, "")

    def setUp(self):
        super().setUp()
        self.real = (updater.check, updater.download, updater.launch_installer)
        self.addCleanup(lambda: (setattr(updater, "check", self.real[0]), setattr(updater, "download", self.real[1]),
                                 setattr(updater, "launch_installer", self.real[2])))

    def test_banner_hidden_without_update(self):
        updater.check = lambda: None
        self.ctx.updates.check(manual=True)
        self.assertTrue(wait_for(lambda: "последняя версия" in self.ctx.updates.text))
        self.assertTrue(self.w.banner.isHidden())

    def test_banner_appears_and_install_flow(self):
        updater.check = lambda: self.REL
        calls = []
        updater.download = lambda rel, progress=None, dest_dir=None: (progress(5, 10), calls.append("dl"), Path("x.exe"))[2]
        updater.launch_installer = lambda path: calls.append("run")
        quits = []
        self.ctx.updates.quit_requested.connect(lambda: quits.append(1))
        self.ctx.updates.check(manual=True)
        self.assertTrue(wait_for(lambda: not self.w.banner.isHidden()))
        self.assertIn("9.9.9", self.w.banner_text.text())
        self.w.btn_banner.click()
        self.assertTrue(wait_for(lambda: quits))
        self.assertEqual(calls, ["dl", "run"])

    def test_check_error_shown_only_for_manual(self):
        def boom():
            raise updater.UpdateError("нет связи с GitHub: тест")
        updater.check = boom
        before = self.ctx.updates.text
        self.ctx.updates.check(manual=False)
        self.assertTrue(wait_for(lambda: not self.ctx.updates.busy))
        self.assertEqual(self.ctx.updates.text, before)
        self.ctx.updates.check(manual=True)
        self.assertTrue(wait_for(lambda: "нет связи" in self.ctx.updates.text))
        self.assertTrue(self.ctx.updates.error)

    def test_manual_click_during_background_check_is_not_lost(self):
        import threading
        gate = threading.Event()

        def slow():
            gate.wait(5)
            raise updater.UpdateError("нет связи с GitHub: тест")
        updater.check = slow
        self.ctx.updates.check(manual=False)         # фоновая проверка зависла в ожидании
        self.assertTrue(self.ctx.updates.busy)
        self.ctx.updates.check(manual=True)          # пользователь нажал кнопку
        self.assertIn("Проверяю", self.ctx.updates.text)
        gate.set()
        self.assertTrue(wait_for(lambda: "нет связи" in self.ctx.updates.text))
        self.assertTrue(self.ctx.updates.error)

    def test_install_failure_keeps_app_running(self):
        updater.check = lambda: self.REL

        def bad(rel, progress=None, dest_dir=None):
            raise updater.UpdateError("контрольная сумма не совпала")
        updater.download = bad
        quits = []
        self.ctx.updates.quit_requested.connect(lambda: quits.append(1))
        self.ctx.updates.check(manual=True)
        wait_for(lambda: self.ctx.updates.release is not None)
        self.w.btn_banner.click()
        self.assertTrue(wait_for(lambda: "контрольная сумма" in self.ctx.updates.text))
        self.assertEqual(quits, [])

    def test_system_page_shows_install_button(self):
        updater.check = lambda: self.REL
        p = page(self.w, "Система")
        self.ctx.updates.check(manual=True)
        self.assertTrue(wait_for(lambda: not p.btn_install.isHidden()))
        self.assertIn("9.9.9", p.upd_line.text())
