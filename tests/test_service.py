"""Жизненный цикл ассистента с подставными микрофоном и распознаванием."""
import queue
import threading
import time
import unittest


from helpers import STUB
from jarvis import backend, service
from jarvis.service import AssistantService


class FakeStt:
    def __init__(self, cfg):
        self.cfg = cfg

    def load(self):
        pass

    def transcribe(self, audio):
        return audio


class FakeRec:
    """Выдаёт заранее заданные «фразы» (здесь сразу текст) и ждёт остановки."""
    feed = []
    opened = 0

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.stop = threading.Event()
        self.paused = False
        self.q = queue.Queue()
        for item in list(FakeRec.feed):
            self.q.put(item)
        self.muted = 0

    def open(self):
        FakeRec.opened += 1

    def close(self):
        pass

    def calibrate(self):
        return 0.02

    def mute(self):
        self.muted += 1

    def unmute(self):
        self.muted -= 1

    def utterances(self):
        while not self.stop.is_set():
            try:
                yield self.q.get(timeout=0.05)
            except queue.Empty:
                continue


class FailingRec(FakeRec):
    def open(self):
        raise OSError("микрофон отключён")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.states, self.events = [], []
        backend.save_settings({"greeting": False})
        import json
        from jarvis import paths
        data = json.loads(paths.SETTINGS_PATH.read_text(encoding="utf-8"))
        data["hermes"] = dict(data.get("hermes", {}), exe=str(STUB))
        data["greeting"] = False
        paths.SETTINGS_PATH.write_text(json.dumps(data), encoding="utf-8")
        self.old = (service.Transcriber, service.Recorder, service.Speaker)
        service.Transcriber, service.Recorder = FakeStt, FakeRec
        FakeRec.feed, FakeRec.opened = [], 0
        self.svc = AssistantService(lambda s, d: self.states.append(s), lambda k, t: self.events.append((k, t)))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.svc.stop()
        service.Transcriber, service.Recorder, service.Speaker = self.old

    def wait(self, cond, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_start_listen_answer_stop(self):
        FakeRec.feed = ["Джарвис, какая погода"]
        self.svc.start()
        self.assertTrue(self.wait(lambda: any(k == "reply" for k, _ in self.events)), self.events)
        self.assertEqual(self.svc.state, service.LISTENING)
        kinds = [k for k, _ in self.events]
        self.assertEqual(kinds[:2], ["heard", "reply"])
        self.assertIn("какая погода", self.events[1][1])
        self.svc.stop()
        self.assertEqual(self.svc.state, service.STOPPED)
        self.assertFalse(self.svc.running)
        self.assertIn(service.LOADING, self.states)
        self.assertIn(service.LISTENING, self.states)

    def test_start_twice_is_harmless(self):
        self.svc.start()
        self.assertTrue(self.wait(lambda: self.svc.state == service.LISTENING))
        self.svc.start()
        time.sleep(0.2)
        self.assertEqual(FakeRec.opened, 1)

    def test_restart_reads_microphone_again(self):
        self.svc.start()
        self.assertTrue(self.wait(lambda: self.svc.state == service.LISTENING))
        self.svc.restart()
        self.assertTrue(self.wait(lambda: self.svc.state == service.LISTENING and FakeRec.opened == 2))

    def test_pause_and_resume(self):
        self.svc.start()
        self.assertTrue(self.wait(lambda: self.svc.state == service.LISTENING))
        self.svc.set_paused(True)
        self.assertEqual(self.svc.state, service.PAUSED)
        self.assertTrue(self.svc._recorder.paused)
        self.svc.set_paused(False)
        self.assertEqual(self.svc.state, service.LISTENING)

    def test_microphone_failure_is_reported_and_retried(self):
        service.Recorder = FailingRec
        self.svc.start()
        self.assertTrue(self.wait(lambda: self.svc.state == service.ERROR))
        self.assertTrue(any(k == "error" and "Микрофон" in t for k, t in self.events))
        self.svc.stop(wait=7)
        self.assertEqual(self.svc.state, service.STOPPED)

    def test_text_chat_uses_hermes_without_microphone(self):
        self.svc.ask_text("расскажи про арбуз")
        self.assertTrue(self.wait(lambda: any(k == "reply" for k, _ in self.events)))
        self.assertEqual(self.events[0], ("heard", "расскажи про арбуз"))
        self.assertIn("арбуз", self.events[1][1])
        self.assertEqual(FakeRec.opened, 0)

    def test_text_chat_error_is_an_event(self):
        from helpers import env
        with env(STUB_FAIL="1"):
            self.svc.ask_text("привет")
            self.assertTrue(self.wait(lambda: any(k == "error" for k, _ in self.events)))

    def test_missing_hermes_is_reported(self):
        import json
        from jarvis import paths
        data = json.loads(paths.SETTINGS_PATH.read_text(encoding="utf-8"))
        data["hermes"]["exe"] = "/нет/такого"
        paths.SETTINGS_PATH.write_text(json.dumps(data), encoding="utf-8")
        old_path = __import__("os").environ["PATH"]
        __import__("os").environ["PATH"] = ""
        try:
            self.svc.start()
            self.assertTrue(self.wait(lambda: any(k == "error" and "Hermes" in t for k, t in self.events)))
        finally:
            __import__("os").environ["PATH"] = old_path
