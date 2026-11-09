"""Тесты для инструментов управления ПК (pc.py) в режиме сухого прогона."""
import os
import subprocess
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


class _Real:
    """Временно выключает сухой прогон и подменяет вызов программ, чтобы проверить, что именно было бы запущено."""
    def __init__(self, win=True, stdout="", returncode=0):
        self.win, self.stdout, self.returncode, self.calls = win, stdout, returncode, []

    def __enter__(self):
        self.saved = (pc.DRY, pc.IS_WIN, subprocess.run)
        pc.DRY, pc.IS_WIN = False, self.win

        def fake_run(argv, **kw):
            self.calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, self.returncode, self.stdout.encode("cp866", "replace"), b"")
        subprocess.run = fake_run
        return self

    def __exit__(self, *exc):
        pc.DRY, pc.IS_WIN, subprocess.run = self.saved


class SafetyTests(unittest.TestCase):
    """Дефекты, найденные анализатором: кодировка, кавычки в PowerShell, зависания."""

    def test_powershell_gets_data_only_through_environment(self):
        nasty = 'C:\\Users\\Иван\\a$b "q" `x`.txt'
        with _Real() as r:
            pc.run_ps("Test-Path $env:JARVIS_ARG", {"JARVIS_ARG": nasty})
        argv, kw = r.calls[0]
        self.assertNotIn(nasty, " ".join(argv))
        self.assertEqual(kw["env"]["JARVIS_ARG"], nasty)
        self.assertEqual(kw["timeout"], 30.0)

    def test_delete_uses_recycle_bin_and_env_for_path(self):
        target = Path(tempfile.mkdtemp()) / 'a$b "x".txt'
        target.write_text("x", encoding="utf-8")
        with _Real() as r:
            out = pc.main(["files", "delete", str(target)])
        self.assertEqual(out, 0)
        argv, kw = r.calls[0]
        script = argv[-1]
        self.assertIn("SendToRecycleBin", script)
        self.assertIn("DeleteDirectory", script, "папки тоже должны удаляться в корзину")
        self.assertNotIn(str(target), script)
        self.assertEqual(kw["env"]["JARVIS_ARG"], str(target))

    def test_every_subprocess_call_has_a_timeout(self):
        with _Real(stdout="") as r:
            pc.run_cmd(["ping", "x"])
            pc.run_ps("1")
        for _argv, kw in r.calls:
            self.assertIn("timeout", kw)

    def test_hung_program_becomes_a_readable_error(self):
        saved = subprocess.run

        def hang(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        subprocess.run = hang
        pc.DRY = False
        try:
            with self.assertRaises(pc.ToolError) as ctx:
                pc.run_cmd(["tasklist"], timeout=1)
            self.assertIn("не ответила", str(ctx.exception))
        finally:
            subprocess.run = saved
            pc.DRY = True

    def test_processes_parse_russian_tasklist_with_nbsp_and_sort_by_memory(self):
        csv_text = ('"chrome.exe","100","Console","1","412\u00a0300 КБ"\n'
                    '"notepad.exe","200","Console","1","8\u00a0100 КБ"\n'
                    '"Code.exe","300","Console","1","1\u00a0200\u00a0000 КБ"\n')
        with _Real(stdout=csv_text) as r:
            r.stdout = csv_text
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = pc.main(["processes"])
        self.assertEqual(code, 0)
        lines = [ln.strip() for ln in buf.getvalue().splitlines()[1:]]
        self.assertTrue(lines[0].startswith("Code.exe"), lines)
        self.assertTrue(lines[1].startswith("chrome.exe"), lines)

    def test_sysinfo_works_without_wmic(self):
        text = pc.cmd_sysinfo(None)
        self.assertIn("Процессор", text)
        self.assertIn("диск", text)


    def test_file_search_finds_nested_file_and_skips_junk_dirs(self):
        base = Path(tempfile.mkdtemp())
        (base / "sub" / "deep").mkdir(parents=True)
        (base / "sub" / "deep" / "Отчёт_2026.xlsx").write_text("x", encoding="utf-8")
        (base / "node_modules").mkdir()
        (base / "node_modules" / "отчёт_мусор.txt").write_text("x", encoding="utf-8")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pc.main(["files", "search", "отчёт", "--to", str(base)])
        out = buf.getvalue()
        self.assertIn("Отчёт_2026.xlsx", out)
        self.assertNotIn("мусор", out)

    def test_file_search_respects_time_budget(self):
        base = Path(tempfile.mkdtemp())
        for i in range(30):
            (base / f"d{i}").mkdir()
        saved = pc.SEARCH_BUDGET_S
        pc.SEARCH_BUDGET_S = -1.0          # бюджет исчерпан сразу
        try:
            walked = list(pc._walk(base, budget=-1.0))
        finally:
            pc.SEARCH_BUDGET_S = saved
        self.assertEqual(len(walked), 1, "обход должен остановиться по времени после первой папки")

    def test_clipboard_text_goes_through_environment(self):
        with _Real() as r:
            pc.main(["clipboard", "set", 'привет "мир" $x'])
        argv, kw = r.calls[0]
        self.assertNotIn("привет", argv[-1])
        self.assertEqual(kw["env"]["JARVIS_ARG"], 'привет "мир" $x')

    def test_screenshot_is_dpi_aware_and_uses_env_path(self):
        dest = str(Path(tempfile.mkdtemp()) / "s.png")
        with _Real() as r:
            pc.main(["screenshot", dest])
        argv, kw = r.calls[0]
        self.assertIn("SetProcessDPIAware", argv[-1])
        self.assertNotIn(dest, argv[-1])
        self.assertEqual(kw["env"]["JARVIS_ARG"], dest)


if __name__ == "__main__":
    unittest.main()
