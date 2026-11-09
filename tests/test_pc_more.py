"""Новые возможности jarvis-pc: заметки, папки, окна, клавиатура, интернет, программы «Пуск»."""
import argparse
import importlib
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PC_PATH = ROOT / "jarvis" / "resources" / "jarvis-pc" / "scripts" / "pc.py"
sys.path.insert(0, str(PC_PATH.parent))
os.environ["JARVIS_DRYRUN"] = "1"
pc = importlib.import_module("pc")


def ns(**kw):
    return argparse.Namespace(**kw)


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "Заметки"
        patcher = mock.patch.dict(os.environ, {"JARVIS_NOTES": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, text, title=""):
        return pc.cmd_notes(ns(action="add", text=text.split(), title=title))

    def test_add_list_read_search(self):
        out = self.add("купить молоко и йогурт к завтраку")
        self.assertIn("сохранена", out)
        files = list(self.dir.glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("купить молоко и йогурт", files[0].read_text(encoding="utf-8"))
        self.add("позвонить Ёжикову", title="Звонки")
        self.assertIn("Звонки", pc.cmd_notes(ns(action="list", text=[], title="")))
        self.assertIn("Ёжикову", pc.cmd_notes(ns(action="read", text=["звонки"], title="")))
        self.assertIn("купить", pc.cmd_notes(ns(action="search", text=["йогурт"], title="")))
        self.assertIn("нет", pc.cmd_notes(ns(action="search", text=["самолёт"], title="")))

    def test_same_title_same_day_appends(self):
        self.add("первое", title="План")
        out = self.add("второе", title="План")
        self.assertIn("Дописал", out)
        body = next(self.dir.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("первое", body)
        self.assertIn("второе", body)

    def test_bad_characters_removed_from_file_name(self):
        self.add('что: сделать / сегодня? "срочно"')
        name = next(self.dir.glob("*.md")).name
        for ch in ':/?"':
            self.assertNotIn(ch, name)

    def test_errors(self):
        with self.assertRaises(pc.ToolError):
            pc.cmd_notes(ns(action="add", text=[], title=""))
        with self.assertRaises(pc.ToolError):
            pc.cmd_notes(ns(action="read", text=["ничего"], title=""))

    def test_settings_folder_used(self):
        os.environ.pop("JARVIS_NOTES")
        with mock.patch.object(pc, "load_settings", lambda: {"notes": {"dir": "/tmp/мои заметки"}}):
            self.assertEqual(pc.notes_dir(), Path("/tmp/мои заметки"))


class FolderTests(unittest.TestCase):
    def test_known_folder_names(self):
        self.assertTrue(str(pc.resolve_place("загрузки")).endswith("Downloads"))
        self.assertTrue(str(pc.resolve_place("Рабочий стол")).endswith("Desktop"))

    def test_absolute_and_relative(self):
        self.assertEqual(pc.resolve_place("/tmp"), Path("/tmp"))
        self.assertTrue(str(pc.resolve_place("Новая папка")).endswith("Новая папка"))
        with self.assertRaises(pc.ToolError):
            pc.resolve_place("")

    def test_dry_actions(self):
        for action, target in [("create", ["/tmp/jarvis-x"]), ("close", ["загрузки"]), ("close", []), ("list", [])]:
            with self.subTest(action=action):
                self.assertTrue(pc.cmd_folder(ns(action=action, target=target)))
        self.assertFalse(Path("/tmp/jarvis-x").exists())       # сухой прогон ничего не создал
        with self.assertRaises(pc.ToolError):
            pc.cmd_folder(ns(action="open", target=["/нет/такой/папки"]))

    def test_close_script_matches_by_path_or_leaf(self):
        self.assertIn("Split-Path $p -Leaf", pc.PS_EXPLORER_WINDOWS)
        self.assertIn("$env:JARVIS_TARGET", pc.PS_EXPLORER_WINDOWS)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.io = pc.DryInput()
        self.io.wins = [{"hwnd": 1, "title": "Program Manager", "process": "explorer.exe"},
                        {"hwnd": 2, "title": "Документ1 - Word", "process": "WINWORD.EXE"},
                        {"hwnd": 3, "title": "YouTube — Google Chrome", "process": "chrome.exe"}]
        patcher = mock.patch.object(pc, "make_input", lambda: self.io)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_hides_shell_windows(self):
        out = pc.cmd_windows(ns())
        self.assertIn("Word", out)
        self.assertNotIn("Program Manager", out)

    def test_find_by_title_or_process(self):
        self.assertEqual(pc._find_window(self.io, "chrome")["hwnd"], 3)
        self.assertEqual(pc._find_window(self.io, "word")["hwnd"], 2)
        self.assertEqual(pc._find_window(self.io, "ютуб".replace("ютуб", "youtube"))["hwnd"], 3)
        with self.assertRaises(pc.ToolError):
            pc._find_window(self.io, "фотошоп")

    def test_actions(self):
        self.assertIn("Word", pc.cmd_window(ns(action="close", target=["word"])))
        pc.cmd_window(ns(action="focus", target=["chrome"]))
        self.assertIn(("окно", "YouTube — Google Chrome"), self.io.log)


class KeysTests(unittest.TestCase):
    def setUp(self):
        self.io = pc.DryInput()
        patcher = mock.patch.object(pc, "make_input", lambda: self.io)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_type_and_press(self):
        pc.cmd_keys(ns(action="type", text=["Привет,", "мир"]))
        self.assertIn(("ввод", 11), self.io.log)
        pc.cmd_keys(ns(action="press", text=["ctrl+s"]))
        self.assertIn(("сочетание", "ctrl+s"), self.io.log)
        pc.cmd_keys(ns(action="press", text=["Enter"]))
        self.assertIn(("клавиша", "enter", 1), self.io.log)

    def test_unknown_key(self):
        with self.assertRaises(pc.ToolError):
            pc.cmd_keys(ns(action="press", text=["ctrl+щ"]))

    def test_refuses_password_windows(self):
        self.io.foreground = lambda: {"title": "Вход в учётную запись — Сбербанк Онлайн"}
        with self.assertRaises(pc.ToolError):
            pc.cmd_keys(ns(action="type", text=["secret"]))


DDG_PAGE = """
<div class="result results_links results_links_deep result--ad"><a class="result__a" href="https://ad.example">Реклама</a></div>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fru.wikipedia.org%2Fwiki%2F%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0&amp;rut=x">Москва &mdash; <b>Википедия</b></a></h2>
  <a class="result__snippet" href="x">Столица <b>России</b>, город федерального значения.</a>
</div>
<div class="result results_links web-result">
  <h2><a class="result__a" href="https://mos.ru/">Официальный сайт Мэра Москвы</a></h2>
  <div class="result__snippet">Новости и услуги.</div>
</div>
"""
BING_PAGE = """<ol><li class="b_algo"><h2><a href="https://example.com/a" h="x">Пример &amp; тест</a></h2>
<div class="b_caption"><p>Описание <strong>страницы</strong>.</p></div></li></ol>"""


class WebTests(unittest.TestCase):
    def test_parse_ddg(self):
        res = pc.parse_ddg(DDG_PAGE)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["url"], "https://ru.wikipedia.org/wiki/Москва")
        self.assertEqual(res[0]["title"], "Москва — Википедия")
        self.assertIn("Столица России", res[0]["snippet"])
        self.assertEqual(res[1]["url"], "https://mos.ru/")

    def test_parse_bing(self):
        res = pc.parse_bing(BING_PAGE)
        self.assertEqual(res, [{"title": "Пример & тест", "url": "https://example.com/a", "snippet": "Описание страницы ."}])

    def test_page_text_drops_scripts_and_menus(self):
        page = ("<html><head><title>Статья</title><script>var x='секрет';</script></head><body><nav>Меню меню меню меню "
                "меню меню</nav><p>Это основной текст статьи, достаточно длинный, чтобы его оставить.</p>"
                "<footer>Подвал подвал подвал подвал подвал</footer></body></html>")
        title, text = pc.page_text(page)
        self.assertEqual(title, "Статья")
        self.assertIn("основной текст", text)
        self.assertNotIn("секрет", text)
        self.assertNotIn("Меню", text)

    def test_search_falls_back_to_bing(self):
        pages = {"duckduckgo": "<html>капча</html>", "bing": BING_PAGE}
        with mock.patch.object(pc, "_fetch", lambda url, **k: (pages["duckduckgo" if "duckduckgo" in url else "bing"], url)):
            res = pc.web_search("тест")
        self.assertEqual(res[0]["url"], "https://example.com/a")

    def test_real_fetch_with_charset(self):
        body = "<html><head><meta charset='windows-1251'><title>Заголовок</title></head><body><p>" + \
               "Текст в кодировке windows-1251, достаточно длинный для страницы." + "</p></body></html>"

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = body.encode("cp1251")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        old = pc.DRY
        pc.DRY = False
        self.addCleanup(setattr, pc, "DRY", old)
        out = pc.cmd_web(ns(action="read", query=[f"http://127.0.0.1:{srv.server_port}/"]))
        self.assertIn("Заголовок", out)
        self.assertIn("кодировке windows-1251", out)


class AppsTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        for name in ["Telegram.lnk", "Adobe Photoshop 2024.lnk", "Удалить Photoshop.lnk", "Steam.url", "readme.txt"]:
            (root / name).write_text("x")
        patcher = mock.patch.dict(os.environ, {"JARVIS_START_MENU": str(root), "ProgramData": "/нет", "APPDATA": "/нет"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_find(self):
        self.assertEqual(pc.find_app("telegram").name, "Telegram.lnk")
        self.assertEqual(pc.find_app("photoshop").name, "Adobe Photoshop 2024.lnk")
        self.assertIsNone(pc.find_app("excel"))

    def test_list_skips_uninstallers(self):
        out = pc.cmd_apps(ns(query=[]))
        self.assertIn("Telegram", out)
        self.assertNotIn("Удалить", out)
        self.assertNotIn("readme", out)

    def test_open_uses_start_menu(self):
        out = pc.cmd_open(ns(target=["photoshop"]))
        self.assertIn("photoshop", out)


class ParseNewCommandsTests(unittest.TestCase):
    def test_parse(self):
        p = pc.build_parser()
        for argv in [["notes", "add", "текст", "--title", "Заголовок"], ["notes", "list"], ["folder", "open", "загрузки"],
                     ["folder", "list"], ["windows"], ["window", "close", "блокнот"], ["keys", "press", "ctrl+s"],
                     ["web", "search", "погода", "в", "Казани"], ["apps"]]:
            with self.subTest(argv=argv):
                self.assertTrue(callable(p.parse_args(argv).func))


if __name__ == "__main__":
    unittest.main()
