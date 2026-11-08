"""Запуск приложения: ключи командной строки и проверка сборки."""
import unittest

from jarvis import app, paths


class ArgsTests(unittest.TestCase):
    def test_flags(self):
        a = app.parse_args(["--minimized", "--no-voice"])
        self.assertTrue(a.minimized and a.no_voice)
        self.assertFalse(a.smoke or a.selftest or a.version)

    def test_autostart_command_matches_parser(self):
        from jarvis import backend
        flag = backend.autostart_command().split()[-1]
        self.assertTrue(app.parse_args([flag]).minimized)


class SmokeTests(unittest.TestCase):
    def test_resources_are_found(self):
        failed = app.smoke_check()
        self.assertEqual([f for f in failed if f.startswith("нет файла")], [])

    def test_missing_resources_are_reported(self):
        real = paths.RES_DIR
        paths.RES_DIR = real / "нет-такой-папки"
        try:
            failed = app.smoke_check()
        finally:
            paths.RES_DIR = real
        self.assertTrue(any("SKILL.md" in f for f in failed))

    def test_server_name_is_stable(self):
        self.assertEqual(app.server_name(), app.server_name())
        self.assertTrue(app.server_name().startswith("Jarvis-"))
