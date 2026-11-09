"""Загрузка модели распознавания: прогресс, кэш, отмена, зависание, запасной адрес — против поддельного сервера."""
import threading
import time
import unittest
from pathlib import Path

import tempfile

from fake_hf import FakeHub, tiny_model
from jarvis import models

REPO = "Systran/faster-whisper-small"


def run_fetch(endpoints, root, cancel=None, **kw):
    seen = []
    cancel = cancel or threading.Event()
    path = models.fetch("small", seen.append, cancel, endpoints=endpoints, root=Path(root), **kw)
    return path, seen


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_downloads_reports_progress_and_caches(self):
        with FakeHub(REPO, tiny_model(), chunk=20_000, pause=0.01) as hub:
            path, seen = run_fetch([hub.url], self.cache)
            self.assertTrue((Path(path) / "model.bin").exists())
            phases = [p.phase for p in seen]
            self.assertEqual(phases[0], "check")
            self.assertEqual(phases[-1], "done")
            self.assertIn("download", phases)
            done_values = [p.done for p in seen if p.phase == "download"]
            self.assertGreater(max(done_values), 0)
            self.assertEqual(done_values, sorted(done_values), "прогресс не должен идти назад")
            n = len(hub.requests)
            # Второй раз сеть не нужна: модель уже в кэше
            path2, seen2 = run_fetch([hub.url], self.cache)
            self.assertEqual(path2, path)
            self.assertEqual(len(hub.requests), n)
            self.assertEqual([p.phase for p in seen2], ["check", "done"])

    def test_text_shows_megabytes_and_host(self):
        p = models.Progress("download", 100 * 1024 * 1024, 484 * 1024 * 1024, 2 * 1024 * 1024, "https://hf-mirror.com", 2)
        text = p.text()
        self.assertIn("100 из 484 МБ", text)
        self.assertIn("2.0 МБ/с", text)
        self.assertIn("hf-mirror.com", text)
        self.assertIn("осталось", text)

    def test_cancel_is_immediate(self):
        cancel = threading.Event()
        with FakeHub(REPO, tiny_model(2_000_000), chunk=2_000, pause=0.05) as hub:
            threading.Timer(0.6, cancel.set).start()
            t0 = time.monotonic()
            with self.assertRaises(models.Cancelled):
                run_fetch([hub.url], self.cache, cancel=cancel)
            self.assertLess(time.monotonic() - t0, 3.0, "остановка должна срабатывать сразу, а не после конца загрузки")

    def test_stalled_endpoint_falls_back_to_next(self):
        with FakeHub(REPO, tiny_model(), mode="hang") as dead, FakeHub(REPO, tiny_model()) as good:
            t0 = time.monotonic()
            path, seen = run_fetch([dead.url, good.url], self.cache, stall_s=1.5)
            self.assertTrue((Path(path) / "model.bin").exists())
            self.assertEqual(max(p.attempt for p in seen), 2, "должен был перейти на второй адрес")
            self.assertLess(time.monotonic() - t0, 20)

    def test_broken_first_endpoint_falls_back(self):
        with FakeHub(REPO, tiny_model(), mode="broken") as bad, FakeHub(REPO, tiny_model()) as good:
            path, _ = run_fetch([bad.url, good.url], self.cache, stall_s=5)
            self.assertTrue((Path(path) / "model.bin").exists())

    def test_all_endpoints_fail_gives_readable_error(self):
        with FakeHub(REPO, tiny_model(), mode="broken") as a, FakeHub(REPO, tiny_model(), mode="broken") as b:
            with self.assertRaises(models.DownloadFailed) as ctx:
                run_fetch([a.url, b.url], self.cache, stall_s=5)
            msg = str(ctx.exception)
            self.assertIn(a.url, msg)
            self.assertIn(b.url, msg)
            self.assertIn("base", msg)

    def test_unknown_model_name(self):
        with self.assertRaises(ValueError):
            models.repo_for("не-модель")

    def test_local_dir_is_used_as_is(self):
        self.assertEqual(models.local_path(self.cache), self.cache)

    def test_manually_placed_folder_is_found_without_network(self):
        folder = models.model_dir("small", Path(self.cache))
        folder.mkdir(parents=True)
        for name, data in tiny_model().items():
            (folder / name).write_bytes(data)
        self.assertEqual(models.local_path("small", Path(self.cache), snapshot=lambda *a, **k: 1 / 0), str(folder))

    def test_incomplete_folder_is_not_trusted(self):
        folder = models.model_dir("small", Path(self.cache))
        folder.mkdir(parents=True)
        (folder / "config.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(models.local_path("small", Path(self.cache), snapshot=lambda *a, **k: 1 / 0))

    def test_failed_attempts_leave_no_final_folder(self):
        with FakeHub(REPO, tiny_model(), mode="broken") as bad:
            with self.assertRaises(models.DownloadFailed):
                run_fetch([bad.url], self.cache, stall_s=5)
        self.assertFalse(models.model_dir("small", Path(self.cache)).exists())


if __name__ == "__main__":
    unittest.main()
