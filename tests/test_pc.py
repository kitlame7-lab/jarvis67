"""Тесты для инструментов управления ПК (pc.py) в режиме сухого прогона."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

# pc.py лежит в ресурсах, не в обычном пакете: подгружаем напрямую
ROOT = Path(__file__).resolve().parent.parent
PC_PATH = ROOT / "jarvis" / "resources" / "jarvis-pc" / "scripts" / "pc.py"
sys.path.insert(0, str(PC_PATH.parent))

os.environ["JARVIS_DRYRUN"] = "1"  # Никаких реальных действий

import importlib  # noqa: E402
pc = importlib.import_module("pc")


class ParseTests(unittest.TestCase):
    """build_parser собирается и разбирает все известные команды."""
    def test_all_subcommands_parse(self):
        p = pc.build_parser()
        cases = [
            ["minimize-all"], ["restore-all"], ["maximize"], ["minimize"],
            ["timer", "10"], ["timer-cancel"], ["lock"], ["now"],
            ["volume", "up"], ["volume", "set", "50"],
            ["open", "блокнот"], ["game-mode", "on"],
            ["weather"], ["weather", "Москва"], ["rate"], ["rate", "доллар"],
            ["files", "list"], ["files", "search", "test"],
            ["files", "size", "/tmp"], ["files", "create-folder", "/tmp/jtest"],
            ["files", "read", str(PC_PATH)],
            ["processes"], ["processes", "python"],
            ["process-kill", "notepad"],
            ["clipboard", "get"], ["clipboard", "set", "hello"],
            ["screenshot"], ["run", "echo", "test"],
            ["notify", "Привет"], ["sysinfo"], ["network"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                args = p.parse_args(argv)
                self.assertTrue(hasattr(args, "func"), f"{argv} не имеет func")


class ToolFuncTests(unittest.TestCase):
    """Основные команды в режиме сухого прогона."""

    def test_now(self):
        result = pc.main(["now"])
        self.assertEqual(result, 0)

    def test_files_list_tmp(self):
        result = pc.main(["files", "list", "/tmp"])
        self.assertEqual(result, 0)

    def test_files_search(self):
        result = pc.main(["files", "search", "pc.py", "--to", str(ROOT / "jarvis")])
        self.assertEqual(result, 0)

    def test_files_size(self):
        result = pc.main(["files", "size", str(PC_PATH)])
        self.assertEqual(result, 0)

    def test_files_read(self):
        result = pc.main(["files", "read", str(PC_PATH)])
        self.assertEqual(result, 0)

    def test_files_create_folder(self):
        # DRY=1 не создаёт по-настоящему, но не ломается при наличии пути
        # Отключим DRY на секунду для реальной проверки
        old = os.environ.pop("JARVIS_DRYRUN", None)
        try:
            pc.DRY = False
            with tempfile.TemporaryDirectory() as d:
                result = pc.main(["files", "create-folder", os.path.join(d, "sub")])
                self.assertEqual(result, 0)
                self.assertTrue(Path(d, "sub").is_dir())
        finally:
            if old:
                os.environ["JARVIS_DRYRUN"] = old
            pc.DRY = True

    def test_files_list_missing(self):
        result = pc.main(["files", "list", "/nonexistent_path_xyz"])
        self.assertEqual(result, 1)

    def test_files_delete_missing(self):
        result = pc.main(["files", "delete", "/nonexistent_xyz"])
        self.assertEqual(result, 1)

    def test_run_echo(self):
        result = pc.main(["run", "echo", "hello"])
        self.assertEqual(result, 0)

    def test_notify_dry(self):
        result = pc.main(["notify", "test", "message"])
        self.assertEqual(result, 0)

    def test_human_size(self):
        self.assertIn("Б", pc._human_size(500))
        self.assertIn("КБ", pc._human_size(2048))
        self.assertIn("МБ", pc._human_size(5 * 1024 * 1024))
        self.assertIn("ГБ", pc._human_size(3 * 1024 ** 3))

    def test_plural(self):
        self.assertEqual(pc.plural(1, "файл", "файла", "файлов"), "файл")
        self.assertEqual(pc.plural(3, "файл", "файла", "файлов"), "файла")
        self.assertEqual(pc.plural(5, "файл", "файла", "файлов"), "файлов")
        self.assertEqual(pc.plural(11, "файл", "файла", "файлов"), "файлов")
        self.assertEqual(pc.plural(21, "файл", "файла", "файлов"), "файл")


if __name__ == "__main__":
    unittest.main()
