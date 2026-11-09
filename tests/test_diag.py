"""Диагностика: отчёт без секретов, журнал ошибок, выводы по сценариям, проверка связи."""
import http.server
import json
import threading
import time
import unittest

import pytest

from jarvis import diag, paths


def base(**over):
    """Набор данных «всё хорошо»; тесты портят нужные места."""
    data = {
        "app": {"version": "3.2.0", "frozen": True, "python": "3.12", "platform": "Windows-11", "encoding": "cp1251",
                "time": "2026-11-09T12:00:00"},
        "paths": {"data": "C:/x", "log": "C:/x/jarvis.log", "free_gb": 50.0},
        "settings": {"stt": {"model": "small"}, "tts": {}, "audio_device": "", "hermes": {"model": "m"}},
        "hermes": {"exe": "C:/hermes.exe", "version": "1.0", "provider": "custom", "model": "gpt", "base_url": "https://s.ru/v1"},
        "model_cache": {"name": "small", "found": True, "path": "C:/x/models/small"},
        "mic": {"count": 1, "error": ""},
        "service": {"state": "listening", "detail": ""},
        "timeline": [], "current_stage": None, "errors": [], "log_tail": [],
        "network": {n: {"ok": True, "ms": 50, "url": n} for n in
                    ("Hugging Face", "Зеркало Hugging Face", "Голос Microsoft", "GitHub", "Погода", "Сервер модели")},
    }
    data.update(over)
    return data


def titles(findings, level=None):
    return [f.title for f in findings if level is None or f.level == level]


class ScrubTests(unittest.TestCase):
    def test_keys_and_tokens_are_removed(self):
        text = ("ключ sk-abcdef1234567890 токен 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw "
                "Authorization: Bearer abc.def-1234567 api_key=hunter2 password: qwerty "
                "https://user:pass@host.ru/x " + "A" * 50)
        out = diag.scrub(text)
        for secret in ("abcdef1234567890", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", "abc.def-1234567", "hunter2", "qwerty",
                       "user:pass", "A" * 50):
            self.assertNotIn(secret, out)

    def test_home_directory_is_masked(self):
        from pathlib import Path
        out = diag.scrub(f"файл {Path.home()}/Desktop/x.txt")
        self.assertNotIn(str(Path.home()), out)

    def test_safe_url_drops_credentials_and_query(self):
        self.assertEqual(diag.safe_url("https://u:p@srv.ru:8443/v1/?key=abc#x"), "https://srv.ru:8443/v1")


class ErrorLogTests(unittest.TestCase):
    def test_duplicates_are_counted(self):
        for _ in range(3):
            diag.record_error("тест", "Ошибка один")
        diag.record_error("тест", "Ошибка два")
        items = {e["message"]: e["count"] for e in diag.recent_errors()}
        self.assertEqual(items, {"Ошибка один": 3, "Ошибка два": 1})

    def test_secrets_never_reach_the_file(self):
        diag.record_error("тест", "сбой с ключом sk-secretsecretsecret1")
        self.assertNotIn("secretsecretsecret", paths.DATA_DIR.joinpath("errors.jsonl").read_text(encoding="utf-8"))

    def test_log_is_bounded(self):
        for i in range(diag.MAX_ERRORS + 30):
            diag.record_error("тест", f"разная ошибка {i}")
        self.assertLessEqual(len(diag.recent_errors(10_000)), diag.MAX_ERRORS)

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_thread_exceptions_are_captured(self):
        diag.install_hooks()
        t = threading.Thread(target=lambda: 1 / 0, name="падающий", daemon=True)
        t.start()
        t.join(5)
        self.assertTrue(any("падающий" in e["source"] and "ZeroDivisionError" in e["message"] for e in diag.recent_errors()))

    def test_clear(self):
        diag.record_error("тест", "x")
        diag.clear_errors()
        self.assertEqual(diag.recent_errors(), [])


class TimelineTests(unittest.TestCase):
    def test_stages_follow_each_other(self):
        tl = diag.Timeline()
        tl.begin("а")
        time.sleep(0.05)
        tl.begin("б", "заметка")
        cur = tl.current()
        self.assertEqual(cur["stage"], "б")
        self.assertGreaterEqual(tl.snapshot()[0]["seconds"], 0.05)
        self.assertFalse(tl.snapshot()[0]["running"])
        tl.end()
        self.assertIsNone(tl.current())


class AnalyzeTests(unittest.TestCase):
    def test_healthy(self):
        f = diag.analyze(base())
        self.assertEqual([x.level for x in f], ["ok"])

    def test_stuck_loading_names_the_stage_and_fix(self):
        data = base(service={"state": "loading", "detail": ""},
                    current_stage={"stage": "скачивание модели", "note": "", "elapsed": 200.0})
        f = diag.analyze(data)
        self.assertEqual(f[0].level, "error")
        self.assertIn("скачивание модели", f[0].title)
        self.assertIn("base", f[0].fix)

    def test_not_stuck_when_short(self):
        data = base(service={"state": "loading", "detail": ""},
                    current_stage={"stage": "скачивание модели", "note": "", "elapsed": 20.0})
        self.assertNotIn("error", [x.level for x in diag.analyze(data)])

    def test_model_missing_and_huggingface_down_but_mirror_up(self):
        data = base(model_cache={"name": "small", "found": False, "path": "p"})
        data["network"]["Hugging Face"] = {"ok": False, "ms": 6000, "error": "timeout"}
        f = diag.analyze(data)
        self.assertEqual(f[0].level, "warn")
        self.assertIn("зеркало", f[0].title)

    def test_model_missing_and_nothing_reachable_is_error_with_manual_path(self):
        data = base(model_cache={"name": "small", "found": False, "path": "C:/x/models/small"})
        data["network"]["Hugging Face"] = {"ok": False, "ms": 6000, "error": "timeout"}
        data["network"]["Зеркало Hugging Face"] = {"ok": False, "ms": 6000, "error": "timeout"}
        f = diag.analyze(data)
        self.assertEqual(f[0].level, "error")
        self.assertIn("C:/x/models/small", f[0].fix)

    def test_low_disk_space_blocks_model(self):
        data = base(model_cache={"name": "small", "found": False, "path": "p"}, paths={"data": "x", "log": "y", "free_gb": 0.4})
        self.assertIn("Мало места на диске для модели речи", titles(diag.analyze(data), "error"))

    def test_no_hermes(self):
        data = base(hermes={"exe": "", "version": "", "provider": "", "model": "", "base_url": ""})
        self.assertIn("Hermes не найден", titles(diag.analyze(data), "error"))

    def test_model_server_down(self):
        data = base()
        data["network"]["Сервер модели"] = {"ok": False, "ms": 5000, "error": "ConnectionRefusedError", "url": "https://s.ru/v1"}
        self.assertIn("Сервер модели ответов недоступен", titles(diag.analyze(data), "error"))

    def test_no_microphone(self):
        self.assertIn("Микрофон не найден", titles(diag.analyze(base(mic={"count": 0, "error": ""})), "error"))

    def test_edge_voice_down_is_only_a_warning(self):
        data = base()
        data["network"]["Голос Microsoft"] = {"ok": False, "ms": 6000, "error": "x"}
        levels = {f.title: f.level for f in diag.analyze(data)}
        self.assertEqual(levels["Голос Microsoft недоступен"], "warn")

    def test_recorded_errors_are_explained_in_russian(self):
        err = {"source": "x", "message": "No usable credentials found for provider 'deepseek'. Set DEEPSEEK_API_KEY.",
               "count": 4, "last": "2026-11-09T10:00:00"}
        f = diag.analyze(base(errors=[err]))
        hit = next(x for x in f if "×4" in x.title)
        self.assertIn("ключ", hit.fix)

    def test_errors_sorted_most_severe_first(self):
        data = base(mic={"count": 0, "error": ""}, errors=[{"source": "s", "message": "m", "count": 1, "last": "t"}])
        levels = [f.level for f in diag.analyze(data)]
        self.assertEqual(levels, sorted(levels, key=lambda x: diag.LEVEL_ORDER[x]))


class NetworkTests(unittest.TestCase):
    def test_probe_reachable_and_unreachable(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        ok = diag.probe(f"http://127.0.0.1:{srv.server_address[1]}/x", timeout=3)
        self.assertTrue(ok["ok"], "любой ответ сервера, даже 404, означает «связь есть»")
        dead = diag.probe("http://127.0.0.1:1/", timeout=2)
        self.assertFalse(dead["ok"])
        self.assertTrue(dead["error"])

    def test_check_network_runs_in_parallel_and_hides_query(self):
        res = diag.check_network({"А": "http://127.0.0.1:1/?key=secret", "Б": "http://127.0.0.1:2/"}, timeout=2)
        self.assertEqual(set(res), {"А", "Б"})
        self.assertNotIn("secret", json.dumps(res))


class ReportTests(unittest.TestCase):
    def test_gather_without_network_has_all_sections(self):
        data = diag.gather(("listening", ""), net=False, hermes_probe=False)
        for key in ("app", "paths", "settings", "hermes", "model_cache", "mic", "service", "timeline", "errors", "log_tail"):
            self.assertIn(key, data)
        self.assertEqual(data["network"], {})
        diag.analyze(data)      # не должно падать на реальных данных

    def test_render_text_is_scrubbed_and_complete(self):
        data = base(errors=[{"source": "s", "message": "ключ sk-abcdefabcdefabcdef", "count": 2, "last": "t", "tb": ""}],
                    log_tail=["12:00:01 INFO токен 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"])
        text = diag.render_text(data, diag.analyze(data))
        for part in ("ВЫВОДЫ", "СОСТОЯНИЕ", "СВЯЗЬ", "ОШИБКИ", "ЖУРНАЛ"):
            self.assertIn(part, text)
        self.assertNotIn("abcdefabcdefabcdef", text)
        self.assertNotIn("AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", text)

    def test_issue_url_points_to_repo_and_is_short(self):
        data = base(mic={"count": 0, "error": ""})
        findings = diag.analyze(data)
        url = diag.issue_url(findings, "отчёт")
        self.assertTrue(url.startswith("https://github.com/kitlame7-lab/jarvis67/issues/new?"))
        self.assertLess(len(url), 1800)
        self.assertIn("%D0%9C%D0%B8%D0%BA%D1%80%D0%BE%D1%84%D0%BE%D0%BD", url)    # «Микрофон» в заголовке


if __name__ == "__main__":
    unittest.main()
