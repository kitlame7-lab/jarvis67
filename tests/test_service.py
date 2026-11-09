"""Жизненный цикл ассистента с подставными микрофоном и распознаванием."""
import queue
import threading
import time
import unittest


from helpers import STUB
from jarvis import backend, diag, models, service
from jarvis.service import AssistantService


class FakeStt:
    """Подставное распознавание. Режимы нужны, чтобы воспроизвести настоящие случаи зависания загрузки."""
    mode = "ok"          # ok | download (идёт, пока не отменят) | uncancellable (держит 1.2 с, отмену не видит) | fail
    loads = 0

    def __init__(self, cfg):
        self.cfg = cfg

    def load(self, on_progress=None, cancel=None):
        FakeStt.loads += 1
        notify = on_progress or (lambda p: None)
        if FakeStt.mode == "download":
            done = 0
            while not cancel.is_set():
                done += 1_000_000
                notify(models.Progress("download", done, 100_000_000, 1_000_000.0, "https://hf-mirror.com", 2))
                time.sleep(0.02)
            raise models.Cancelled()
        if FakeStt.mode == "uncancellable":
            notify(models.Progress("loading"))
            time.sleep(1.2)
        if FakeStt.mode == "fail":
            raise models.DownloadFailed("Не удалось скачать модель распознавания речи.\n  нет сети")

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


class ServiceCase(unittest.TestCase):
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
        FakeStt.mode, FakeStt.loads = "ok", 0
        self.progress = []
        self.svc = AssistantService(lambda s, d: self.states.append(s), lambda k, t: self.events.append((k, t)),
                                    lambda f, t: self.progress.append((f, t)))
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



class ServiceTests(ServiceCase):
    def test_start_listen_answer_stop(self):
        FakeRec.feed = ["Джарвис, расскажи анекдот"]
        self.svc.start()
        self.assertTrue(self.wait(lambda: any(k == "reply" for k, _ in self.events)), self.events)
        self.assertEqual(self.svc.state, service.LISTENING)
        kinds = [k for k, _ in self.events]
        self.assertEqual(kinds[:2], ["heard", "reply"])
        self.assertIn("расскажи анекдот", self.events[1][1])
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


class SlowStartTests(ServiceCase):
    """Случаи, из-за которых окно навсегда оставалось на «Загружаюсь…»."""

    def test_progress_is_reported_during_download(self):
        FakeStt.mode = "download"
        self.svc.start()
        self.assertTrue(self.wait(lambda: any(f > 0 for f, _ in self.progress)))
        fraction, text = [x for x in self.progress if x[0] > 0][-1]
        self.assertGreater(fraction, 0)
        self.assertIn("МБ", text)
        self.assertEqual(self.svc.state, service.LOADING)

    def test_stop_during_download_is_immediate(self):
        FakeStt.mode = "download"
        self.svc.start()
        self.assertTrue(self.wait(lambda: any(f > 0 for f, _ in self.progress)))
        t0 = time.monotonic()
        self.svc.stop()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertFalse(self.svc.running)
        self.assertEqual(self.svc.state, service.STOPPED)

    def test_restart_during_download_starts_again(self):
        FakeStt.mode = "download"
        self.svc.start()
        self.assertTrue(self.wait(lambda: FakeStt.loads == 1 and any(f > 0 for f, _ in self.progress)))
        FakeStt.mode = "ok"                      # на второй раз «сеть наладилась»
        self.svc.restart()
        self.assertTrue(self.wait(lambda: self.svc.state == service.LISTENING), self.states[-5:])
        self.assertEqual(FakeStt.loads, 2)

    def test_restart_while_load_cannot_be_cancelled_still_restarts(self):
        """Загрузку в память прервать нельзя: просьба «перезапустить» не должна теряться."""
        FakeStt.mode = "uncancellable"
        self.svc.start()
        self.assertTrue(self.wait(lambda: FakeStt.loads == 1))
        FakeStt.mode = "ok"
        self.svc.stop(wait=0.2)
        self.assertTrue(self.svc.running, "поток ещё занят загрузкой")
        self.svc.start()                          # раньше эта просьба молча терялась
        self.assertTrue(self.wait(lambda: self.svc.state == service.LISTENING, timeout=10), self.states[-5:])
        self.assertEqual(FakeStt.loads, 2)

    def test_stop_cancels_pending_restart(self):
        FakeStt.mode = "uncancellable"
        self.svc.start()
        self.assertTrue(self.wait(lambda: FakeStt.loads == 1))
        self.svc.stop(wait=0.1)
        self.svc.start()
        self.svc.stop(wait=0.1)
        time.sleep(1.8)
        self.assertFalse(self.svc.running)
        self.assertEqual(FakeStt.loads, 1)

    def test_download_failure_is_an_error_with_reason(self):
        FakeStt.mode = "fail"
        self.svc.start()
        self.assertTrue(self.wait(lambda: self.svc.state == service.ERROR))
        self.assertTrue(any(k == "error" and "нет сети" in t for k, t in self.events))
        self.assertTrue(any("загрузка модели" == e["source"] for e in diag.recent_errors()))

    def test_watchdog_reports_a_stuck_stage(self):
        old = dict(service.PATIENCE_S), service.WATCH_EVERY_S
        service.PATIENCE_S[service.STAGE_MEMORY] = 0.3
        service.WATCH_EVERY_S = 0.1
        self.addCleanup(lambda: (service.PATIENCE_S.update(old[0]), setattr(service, "WATCH_EVERY_S", old[1])))
        FakeStt.mode = "uncancellable"
        self.svc.start()
        self.assertTrue(self.wait(lambda: any(k == "error" and "не двигается" in t for k, t in self.events), timeout=6),
                        self.events)
        self.assertTrue(any(e["source"] == "запуск" for e in diag.recent_errors()))

    def test_timeline_records_stages(self):
        self.svc.start()
        self.assertTrue(self.wait(lambda: self.svc.state == service.LISTENING))
        stages = [t["stage"] for t in diag.TIMELINE.snapshot()]
        self.assertIn(service.STAGE_MIC, stages)
        self.assertIsNone(diag.TIMELINE.current(), "в состоянии «Слушаю» открытых этапов быть не должно")
