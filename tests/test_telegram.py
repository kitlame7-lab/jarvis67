"""Свой Telegram-бот: привязка кодом, задания, голосовые, файлы, /stop, 409 и неверный токен — на поддельном Bot API."""
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from jarvis import agent, telegram
from jarvis.core import Reply

TOKEN = "123456789:AAHfakefakefakefakefakefakefakefake1"


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeBotApi:
    """Минимальный Bot API: getMe, getUpdates (long polling), sendMessage, editMessageText, upload, getFile, файлы."""

    def __init__(self):
        self.updates = queue.Queue()
        self.calls = []
        self.uploads = []
        self.files = {}
        self.fail = None            # (код, описание) для getUpdates
        self.next_id = 1
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, payload, code=200):
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith(f"/file/bot{TOKEN}/"):
                    data = srv.files.get(self.path.rsplit("/", 1)[-1], b"")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                if not self.path.startswith(f"/bot{TOKEN}/"):
                    return self.reply({"ok": False, "error_code": 401, "description": "Unauthorized"}, 401)
                method = self.path.rsplit("/", 1)[-1]
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if "multipart" in self.headers.get("Content-Type", ""):
                    srv.uploads.append((method, raw))
                    return self.reply({"ok": True, "result": {"message_id": 99}})
                params = json.loads(raw or b"{}")
                srv.calls.append((method, params))
                if method == "getMe":
                    return self.reply({"ok": True, "result": {"id": 1, "username": "jarvis_test_bot"}})
                if method == "getUpdates":
                    if srv.fail:
                        code, desc = srv.fail
                        return self.reply({"ok": False, "error_code": code, "description": desc}, code)
                    if params.get("offset") == -1:
                        return self.reply({"ok": True, "result": []})
                    items = []
                    try:
                        items.append(srv.updates.get(timeout=min(1.0, params.get("timeout", 0))))
                    except queue.Empty:
                        pass
                    return self.reply({"ok": True, "result": items})
                if method == "getFile":
                    return self.reply({"ok": True, "result": {"file_path": f"docs/{params['file_id']}"}})
                if method in ("sendMessage", "editMessageText"):
                    srv.next_id += 1
                    return self.reply({"ok": True, "result": {"message_id": srv.next_id}})
                return self.reply({"ok": True, "result": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.uid = 0

    def push(self, user, text="", **extra):
        self.uid += 1
        msg = dict({"message_id": self.uid, "from": {"id": user}, "chat": {"id": user}, "date": int(time.time()),
                    "text": text}, **extra)
        self.updates.put({"update_id": self.uid, "message": msg})

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]

    def texts(self):
        return [p["text"] for p in self.sent()]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeHandler(telegram.Handler):
    def __init__(self):
        self.asked = []
        self.block = threading.Event()

    def ask(self, job):
        self.asked.append(job.text)
        if job.text == "долго":
            job.progress("files search отчёт")
            while not job.cancel.is_set():
                time.sleep(0.02)
            return "не должен прийти"
        if job.text.startswith("пришли"):
            job.progress("send C:\\a.txt")
            job.files.append(self.file)
            return "Отправляю файл."
        return f"Сделано: {job.text}"

    def screenshot(self):
        return self.file

    def transcribe(self, data):
        return "открой загрузки" if data == b"OGG" else ""

    def run_routine(self, job, name):
        return f"Шаблон {name} выполнен."


class BotTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"JARVIS_HOME": home})
        patcher.start()
        self.addCleanup(patcher.stop)
        p2 = mock.patch.object(telegram.paths, "DATA_DIR", Path(home))
        p2.start()
        self.addCleanup(p2.stop)
        self.api = FakeBotApi()
        self.addCleanup(self.api.close)
        self.handler = FakeHandler()
        self.handler.file = str(Path(home) / "screen.png")
        Path(self.handler.file).write_bytes(b"\x89PNG fake")

    def start(self, allowed=(), **cfg):
        telegram.save_config(token=TOKEN, allowed=list(allowed), **cfg)
        bot = telegram.TelegramBot(self.handler, base=self.api.url, downloads=Path(tempfile.mkdtemp()))
        bot.poll_timeout = 1
        bot.start()
        self.addCleanup(bot.stop)
        self.assertTrue(wait_for(lambda: bot.state == "ok"))
        return bot

    def test_stranger_is_ignored_until_code(self):
        bot = self.start()
        self.assertIn(bot.pair_code, bot.status)
        self.api.push(555, "открой загрузки")
        self.assertTrue(wait_for(lambda: any("код" in t for t in self.api.texts())))
        self.assertEqual(self.handler.asked, [])
        code = bot.pair_code
        self.api.push(555, f"/start {code}")
        self.assertTrue(wait_for(lambda: any("привязаны" in t for t in self.api.texts())))
        self.assertEqual(telegram.load_config()["allowed"], [555])
        self.assertNotEqual(bot.pair_code, code)              # код одноразовый
        self.api.push(555, "открой загрузки")
        self.assertTrue(wait_for(lambda: "Сделано: открой загрузки" in self.api.texts()))

    def test_wrong_codes_do_not_bind_and_bot_goes_quiet(self):
        bot = self.start()
        for _ in range(5):
            self.api.push(777, "000000")
        time.sleep(1.5)
        self.assertEqual(telegram.load_config()["allowed"], [])
        self.assertLessEqual(len([t for t in self.api.texts() if "код" in t]), 3)
        self.assertTrue(bot.running)

    def test_task_with_progress_and_file(self):
        self.start(allowed=[1])
        self.api.push(1, "пришли файл")
        self.assertTrue(wait_for(lambda: "Отправляю файл." in self.api.texts()))
        self.assertTrue(wait_for(lambda: self.api.uploads))
        self.assertEqual(self.api.uploads[0][0], "sendPhoto")
        self.assertTrue(any("Выполняю" in t for t in self.api.texts()))
        self.assertTrue(any("typing" == p.get("action") for p in self.api.sent("sendChatAction")))

    def test_voice_message_is_transcribed(self):
        self.api.files["v1"] = b"OGG"
        self.start(allowed=[1])
        self.api.push(1, "", voice={"file_id": "v1", "duration": 2})
        self.assertTrue(wait_for(lambda: "Сделано: открой загрузки" in self.api.texts()))
        self.assertIn("🎙 открой загрузки", self.api.texts())

    def test_incoming_document_is_saved_and_mentioned(self):
        self.api.files["d1"] = b"hello"
        bot = self.start(allowed=[1])
        self.api.push(1, "", caption="сохрани", document={"file_id": "d1", "file_name": "отчёт.txt"})
        self.assertTrue(wait_for(lambda: self.handler.asked))
        saved = bot.downloads / "отчёт.txt"
        self.assertEqual(saved.read_bytes(), b"hello")
        self.assertIn(str(saved), self.handler.asked[0])

    def test_stop_cancels_running_job(self):
        self.start(allowed=[1])
        self.api.push(1, "долго")
        self.assertTrue(wait_for(lambda: any("Выполняю" in t for t in self.api.texts())))
        self.api.push(1, "/stop")
        self.assertTrue(wait_for(lambda: "Остановил." in self.api.texts()))
        time.sleep(0.3)
        self.assertNotIn("не должен прийти", self.api.texts())

    def test_commands(self):
        self.start(allowed=[1])
        self.api.push(1, "/screen")
        self.assertTrue(wait_for(lambda: self.api.uploads))
        self.api.push(1, "/run утро")
        self.assertTrue(wait_for(lambda: "Шаблон утро выполнен." in self.api.texts()))
        self.api.push(1, "/voice on")
        self.assertTrue(wait_for(lambda: telegram.load_config()["voice_reply"]))
        self.api.push(1, "/help")
        self.assertTrue(wait_for(lambda: any("/screen" in t for t in self.api.texts())))

    def test_old_messages_are_skipped(self):
        self.start(allowed=[1])
        self.api.push(1, "выключи компьютер", date=int(time.time()) - 3600)
        self.api.push(1, "привет")
        self.assertTrue(wait_for(lambda: "Сделано: привет" in self.api.texts()))
        self.assertNotIn("выключи компьютер", self.handler.asked)

    def test_conflict_409_is_explained(self):
        self.api.fail = (409, "Conflict: terminated by other getUpdates request")
        telegram.save_config(token=TOKEN, allowed=[1])
        states = []
        bot = telegram.TelegramBot(self.handler, base=self.api.url)
        bot.on_status = lambda: states.append(bot.state)
        bot.start()
        self.addCleanup(bot.stop)
        self.assertTrue(wait_for(lambda: bot.state == "conflict"))
        self.assertIn("шлюз Hermes", bot.status)
        self.api.fail = None
        bot._stop.set()

    def test_bad_token_stops_bot(self):
        telegram.save_config(token="1:bad", allowed=[1])
        bot = telegram.TelegramBot(self.handler, base=self.api.url)
        bot.start()
        self.addCleanup(bot.stop)
        self.assertTrue(wait_for(lambda: bot.state == "bad_token"))
        self.assertTrue(wait_for(lambda: not bot.running))

    def test_no_token(self):
        telegram.save_config(token="")
        bot = telegram.TelegramBot(self.handler, base=self.api.url)
        bot.start()
        self.assertEqual(bot.state, "no_token")
        self.assertFalse(bot.running)

    def test_long_reply_is_split(self):
        bot = telegram.TelegramBot(self.handler, base=self.api.url)
        telegram.save_config(token=TOKEN)
        bot.api = telegram.BotApi(TOKEN, self.api.url)
        bot.send(1, "а" * 9000)
        self.assertEqual(len(self.api.sent()), 3)

    def test_token_hidden(self):
        self.assertNotIn("fakefake", repr(telegram.BotApi(TOKEN)))
        self.assertEqual(telegram.mask_token(TOKEN), "…ake1")
        self.assertTrue(telegram.TOKEN_RE.match(TOKEN))
        self.assertFalse(telegram.TOKEN_RE.match("123:abc"))

    def test_import_from_hermes(self):
        from jarvis import backend
        home = Path(tempfile.mkdtemp())
        (home / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USERS=11, 22\n", encoding="utf-8")
        with mock.patch.object(backend, "hermes_home", lambda: home):
            self.assertTrue(telegram.import_from_hermes())
            self.assertFalse(telegram.import_from_hermes())    # второй раз не перезаписывает
        cfg = telegram.load_config()
        self.assertEqual((cfg["token"], cfg["allowed"]), (TOKEN, [11, 22]))

    def test_screenshot_paths(self):
        res = [("screenshot", True, "Скриншот сохранён: C:\\Users\\a\\Desktop\\s.png"), ("now", True, "12:00")]
        self.assertEqual(telegram.screenshot_paths(res), ["C:\\Users\\a\\Desktop\\s.png"])


class BridgeTests(unittest.TestCase):
    """Связка с движком: send-инструмент, шаблоны, отдельная история на чат."""

    def setUp(self):
        from jarvis import service
        home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"JARVIS_HOME": home})
        p.start()
        self.addCleanup(p.stop)
        self.svc = service.AssistantService()
        self.bridge = service.TelegramBridge(self.svc)
        self.service = service

    def test_send_tool_attaches_existing_file_only(self):
        job = telegram.Job("пришли", 1, 1)
        tools = self.bridge._tools(job)
        f = Path(tempfile.mkdtemp()) / "a.txt"
        f.write_text("x")
        self.assertEqual(tools["send"].run([str(f)])[0], True)
        self.assertEqual(tools["send"].run(["/нет/файла"])[0], False)
        self.assertEqual(job.files, [str(f)])

    def test_ask_uses_engine_with_telegram_channel(self):
        seen = {}
        a = self.svc.tg_assistant(5)

        def think(text, cancel=None, on_action=None, channel="voice", tools=None, **kw):
            seen.update(text=text, channel=channel, tools=sorted(tools))
            r = Reply(True, "Готово.", "native")
            r.results = [("screenshot", True, "Скриншот сохранён: /tmp/x.png")]
            return r
        a.think = think
        job = telegram.Job("сделай скриншот и пришли", 5, 5)
        self.assertEqual(self.bridge.ask(job), "Готово.")
        self.assertEqual(seen["channel"], "telegram")
        self.assertIn("send", seen["tools"])
        self.assertEqual(job.files, ["/tmp/x.png"])
        self.assertIs(self.svc.tg_assistant(5), a)
        self.assertIsNot(self.svc.tg_assistant(6), a)
        self.bridge.reset(5)
        self.assertIsNot(self.svc.tg_assistant(5), a)

    def test_routine_tool_saves_and_lists(self):
        tool = self.service.routine_tool()
        ok, text = tool.run(["save", "утро:", "открой", "почту;", "скажи", "погоду"])
        self.assertTrue(ok, text)
        ok, text = tool.run(["list"])
        self.assertIn("утро (2 шаг.)", text)
        ok, text = tool.run(["show", "утро"])
        self.assertIn("открой почту | скажи погоду", text)
        self.assertTrue(tool.run(["delete", "утро"])[0])
        self.assertFalse(tool.run(["save", "пусто"])[0])

    def test_routine_requires_native_engine(self):
        from jarvis import routines
        routines.save("утро", "- скажи погоду")
        a = self.svc.tg_assistant(1)
        a.agent = None
        self.assertIn("встроенный движок", self.service.run_routine_text(a, "утро"))
        self.assertIn("Есть: утро", self.service.run_routine_text(a, "вечер"))

    def test_routine_runs_through_agent_with_budget(self):
        from jarvis import routines
        routines.save("отчёт", "# Отчёт\n- найди файлы за {1}\n- создай заметку {дата}")
        a = self.svc.tg_assistant(1)
        a.agent = mock.Mock()
        a.agent.ask.return_value = agent.Answer("Сделал оба шага.")
        out = self.service.run_routine_text(a, "отчёт за март")
        self.assertEqual(out, "Сделал оба шага.")
        prompt = a.agent.ask.call_args[0][0]
        self.assertIn("1. найди файлы за за март", prompt)
        self.assertEqual(a.agent.ask.call_args.kwargs["max_steps"], 6)


if __name__ == "__main__":
    unittest.main()
