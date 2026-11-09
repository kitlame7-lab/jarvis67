"""Голосовые команды записи и запуска задач внутри Assistant (без микрофона и без Hermes)."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import tasks
from jarvis.intents import Intent
from tests.test_core import make


class FakeTeach:
    def __init__(self):
        self.recording = False
        self.calls = []
        self.ok = True

    def start(self, name=""):
        self.calls.append(("start", name))
        self.recording = self.ok
        return self.ok

    def stop(self, name=""):
        self.calls.append(("stop", name))
        self.recording = False
        return True

    def discard(self):
        self.calls.append(("discard",))
        was, self.recording = self.recording, False
        return was


class IntentFlowTests(unittest.TestCase):
    def setUp(self):
        self.a, self.sp = make()
        self.teach = FakeTeach()
        self.a.teach = self.teach
        self.asked = []
        self.a.answer = lambda cmd: self.asked.append(cmd) or "ответ"      # Hermes не нужен

    def test_start_needs_wake_word(self):
        self.assertIsNone(self.a.handle_transcript("запомни как я это делаю"))
        self.assertEqual(self.teach.calls, [])
        self.a.handle_transcript("Джарвис, запомни как я это делаю")
        self.assertEqual(self.teach.calls, [("start", "")])
        self.assertEqual(self.asked, [])

    def test_while_recording_only_stop_and_cancel_work_without_wake_word(self):
        self.teach.recording = True
        self.assertIsNone(self.a.handle_transcript("какая сегодня погода"))
        self.assertEqual(self.asked, [])
        self.a.handle_transcript("стоп, назови задачу отчёт")
        self.assertEqual(self.teach.calls, [("stop", "отчёт")])
        self.teach.recording = True
        self.a.handle_transcript("отмени запись")
        self.assertEqual(self.teach.calls[-1], ("discard",))

    def test_ordinary_request_still_goes_to_hermes(self):
        self.a.handle_transcript("Джарвис, какая погода")
        self.assertEqual(self.asked, ["какая погода"])
        self.assertEqual(self.teach.calls, [])

    def test_start_failure_and_double_start_are_explained(self):
        self.teach.ok = False
        self.assertIn("Не получилось", self.a.handle_transcript("Джарвис, запиши мои действия"))
        self.teach.ok, self.teach.recording = True, True
        self.assertIn("уже идёт", self.a.handle_intent(Intent("teach_start")))

    def test_without_recorder_commands_are_polite(self):
        self.a.teach = None
        self.assertIn("недоступна", self.a.handle_transcript("Джарвис, запомни как я это делаю"))

    def test_task_run_finds_task_and_uses_runner(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tasks.save_task(tasks.Task(name="Отчёт по продажам", steps=[{"kind": "key", "key": "enter"}]), root=root)
            runner_calls = []
            self.a.task_runner = lambda t: runner_calls.append(t.slug) or "Запустил."
            with mock.patch.object(tasks, "tasks_dir", lambda: root):
                reply = self.a.handle_transcript("Джарвис, выполни задачу отчёт по продажам")
                missing = self.a.handle_transcript("Джарвис, выполни задачу совсем другое")
        self.assertEqual(reply, "Запустил.")
        self.assertEqual(runner_calls, ["otchet-po-prodazham"])
        self.assertIn("Не нашёл такой задачи", missing)
        self.assertIn("Отчёт по продажам", missing)
        self.assertEqual(self.asked, [])

    def test_notify_speaks_from_background(self):
        self.a.notify("Задача готова")
        end = time.time() + 3
        while time.time() < end and "Задача готова" not in self.sp.said:
            time.sleep(0.02)
        self.assertIn("Задача готова", self.sp.said)


if __name__ == "__main__":
    unittest.main()
