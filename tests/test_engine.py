"""Быстрый ответ: прямой запрос к модели, цикл команд, быстрые правила, устойчивость к зависанию и остановке."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import agent, backend, llm, quick
from jarvis.core import TIMINGS, Assistant, Hermes, NullRecorder, deep_merge, load_settings
from tests.helpers import STUB
from tests.test_backend import FakeOpenAI

PC_SCRIPTS = Path(__file__).resolve().parent.parent / "jarvis" / "resources" / "jarvis-pc" / "scripts"


def cfg_for_tests(over=None):
    cfg = load_settings(Path("/nonexistent.json"))
    cfg["hermes"]["exe"] = str(STUB)
    cfg["hermes"]["ack_after_s"] = 0.3
    return deep_merge(cfg, over) if over else cfg


class Speaker:
    def __init__(self):
        self.said, self.cancel = [], threading.Event()

    def say(self, text):
        self.said.append(text)


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class LlmTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOpenAI()
        self.addCleanup(self.srv.close)

    def ep(self, **kw):
        return llm.Endpoint(self.srv.url, kw.get("key", "sk-good"), kw.get("model", "gpt-x"))

    def test_plain_answer(self):
        self.assertEqual(llm.chat(self.ep(), [{"role": "user", "content": "привет"}]), "Готов.")
        body = self.srv.requests[-1][2]
        self.assertEqual(body["model"], "gpt-x")
        self.assertEqual(body["messages"][0]["content"], "привет")

    def test_errors_are_explained_and_key_never_leaks(self):
        with self.assertRaises(llm.LLMError) as cm:
            llm.chat(self.ep(key="sk-wrong-secret"), [{"role": "user", "content": "x"}])
        self.assertNotIn("sk-wrong-secret", str(cm.exception))
        self.assertTrue(str(cm.exception))
        self.srv.balance = False
        with self.assertRaises(llm.LLMError) as cm:
            llm.chat(self.ep(), [{"role": "user", "content": "x"}])
        self.assertIn("средств", str(cm.exception).lower())

    def test_unreachable_server_gives_russian_reason(self):
        self.srv.close()
        with self.assertRaises(llm.LLMError) as cm:
            llm.chat(self.ep(), [{"role": "user", "content": "x"}], timeout=2)
        self.assertIn("Не удалось связаться", str(cm.exception))

    def test_slow_server_times_out_quickly(self):
        gate = threading.Event()

        class Slow(FakeOpenAI):
            pass
        srv = Slow()
        self.addCleanup(srv.close)
        orig = srv.server.RequestHandlerClass.do_POST

        def slow_post(handler):
            gate.wait(5)
            orig(handler)
        srv.server.RequestHandlerClass.do_POST = slow_post
        t0 = time.monotonic()
        with self.assertRaises(llm.LLMError) as cm:
            llm.chat(llm.Endpoint(srv.url, "sk-good", "gpt-x"), [{"role": "user", "content": "x"}], timeout=0.5)
        gate.set()
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("не ответила вовремя", str(cm.exception))

    def test_think_block_is_removed(self):
        with mock.patch("urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = (
                "{\"choices\":[{\"message\":{\"content\":\"<think>размышляю</think>Привет\"}}]}".encode())
            op.return_value.__enter__.return_value.status = 200
            self.assertEqual(llm.chat(self.ep(), [{"role": "user", "content": "x"}]), "Привет")

    def test_endpoint_repr_hides_key(self):
        self.assertNotIn("sk-good", repr(self.ep()))

    def test_resolve_endpoint_from_custom_server(self):
        cfg = cfg_for_tests({"hermes": {"provider": "custom", "model": "gpt-x"}})
        with mock.patch.object(backend, "read_hermes_model", lambda home: {"provider": "custom", "model": "gpt-x",
                                                                           "base_url": self.srv.url, "key_env": ""}), \
                mock.patch.object(backend, "stored_key", lambda env: "sk-good"):
            ep = llm.resolve_endpoint(cfg)
        self.assertIsNotNone(ep)
        self.assertEqual((ep.base_url, ep.key, ep.model), (self.srv.url, "sk-good", "gpt-x"))

    def test_unknown_provider_or_missing_key_means_no_native_engine(self):
        with mock.patch.object(backend, "read_hermes_model", lambda home: {"provider": "anthropic", "model": "m"}):
            self.assertIsNone(llm.resolve_endpoint(cfg_for_tests()))
        with mock.patch.object(backend, "read_hermes_model", lambda home: {"provider": "deepseek", "model": ""}), \
                mock.patch.object(backend, "stored_key", lambda env: ""), mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(llm.resolve_endpoint(cfg_for_tests()))


class ParseTests(unittest.TestCase):
    def test_parse_actions(self):
        acts, rest = agent.parse_actions("ДЕЙСТВИЕ: volume up\nдействие: files list C:\\Users\\a\\Desktop\nпросто текст")
        self.assertEqual(acts, ["volume up", "files list C:\\Users\\a\\Desktop"])
        self.assertEqual(rest, "просто текст")
        self.assertEqual(agent.parse_actions("Сейчас 12:05.")[0], [])

    def test_split_args_keeps_windows_paths_and_quotes(self):
        self.assertEqual(agent.split_args('files move "C:\\Мои файлы\\a.txt" --to D:\\x'),
                         ["files", "move", "C:\\Мои файлы\\a.txt", "--to", "D:\\x"])

    def test_safety(self):
        ok = agent.check_allowed
        self.assertIsNone(ok(["volume", "up"]))
        self.assertIsNotNone(ok([]))
        self.assertIn("неизвестная", ok(["rm", "-rf", "/"]))
        self.assertIsNotNone(ok(["run", "format", "c:", "/q"]))
        self.assertIsNotNone(ok(["run", "shutdown", "/s"]))
        self.assertIsNone(ok(["run", "ipconfig"]))
        self.assertIsNotNone(ok(["process-kill", "csrss.exe"]))
        self.assertIsNone(ok(["process-kill", "notepad"]))
        self.assertIsNotNone(ok(["files", "delete", "C:\\"]))
        self.assertIsNotNone(ok(["files", "delete", "C:\\Windows\\System32"]))
        self.assertIsNotNone(ok(["files", "move", "C:\\Users\\ivan", "--to", "D:\\"]))
        self.assertIsNone(ok(["files", "delete", "C:\\Users\\ivan\\Desktop\\old.txt"]))

    def test_catalog_commands_exist_in_pc(self):
        sys.path.insert(0, str(PC_SCRIPTS))
        try:
            import pc
            parser = pc.build_parser()
        finally:
            sys.path.remove(str(PC_SCRIPTS))
        known = set(parser._subparsers._group_actions[0].choices)
        self.assertLessEqual(agent.COMMANDS, known)

    def test_system_prompt_lists_every_command(self):
        text = agent.system_prompt("Джарвис", "сэр")
        for line, _ in agent.CATALOG:
            self.assertIn(line, text)
        self.assertIn("сэр", text)


class AgentTests(unittest.TestCase):
    def make(self, replies, results=None, **eng):
        self.sent, self.ran = [], []
        script = list(replies)

        def chat(ep, messages, **kw):
            self.sent.append([dict(m) for m in messages])
            r = script.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        def runner(tokens):
            self.ran.append(tokens)
            return (results or {}).get(" ".join(tokens), (True, "готово"))
        cfg = cfg_for_tests({"engine": eng} if eng else None)
        return agent.Agent(cfg, llm.Endpoint("http://x", "k", "m"), runner=runner, chat=chat)

    def test_plain_answer_is_one_call(self):
        ag = self.make(["Два плюс два — четыре, сэр."])
        out = ag.ask("сколько будет два плюс два")
        self.assertEqual(out.text, "Два плюс два — четыре, сэр.")
        self.assertEqual((out.steps, self.ran), (1, []))
        self.assertEqual(self.sent[0][0]["role"], "system")
        self.assertEqual(self.sent[0][-1], {"role": "user", "content": "сколько будет два плюс два"})

    def test_action_round_trip(self):
        ag = self.make(["ДЕЙСТВИЕ: sysinfo", "Процессор почти свободен, сэр."], {"sysinfo": (True, "CPU 5%")})
        out = ag.ask("как там компьютер")
        self.assertEqual(self.ran, [["sysinfo"]])
        self.assertEqual(out.text, "Процессор почти свободен, сэр.")
        self.assertEqual(out.steps, 2)
        self.assertIn("CPU 5%", self.sent[1][-1]["content"])

    def test_several_actions_in_one_reply(self):
        ag = self.make(["ДЕЙСТВИЕ: volume mute\nДЕЙСТВИЕ: minimize-all", "Готово."])
        ag.ask("тишина и чистый стол")
        self.assertEqual(self.ran, [["volume", "mute"], ["minimize-all"]])

    def test_dangerous_action_is_refused_not_run(self):
        ag = self.make(["ДЕЙСТВИЕ: files delete C:\\", "Это нельзя, сэр."])
        out = ag.ask("удали всё")
        self.assertEqual(self.ran, [])
        self.assertIn("ОШИБКА", self.sent[1][-1]["content"])
        self.assertEqual(out.text, "Это нельзя, сэр.")

    def test_failed_command_is_reported_to_model(self):
        ag = self.make(["ДЕЙСТВИЕ: files read C:\\x.txt", "Файл не найден."], {"files read C:\\x.txt": (False, "нет файла")})
        ag.ask("прочитай файл")
        self.assertIn("ОШИБКА: нет файла", self.sent[1][-1]["content"])

    def test_step_cap_stops_endless_loop(self):
        ag = self.make(["ДЕЙСТВИЕ: now"] * 10, max_steps=3)
        out = ag.ask("зациклись")
        self.assertEqual(len(self.ran), 3)
        self.assertTrue(out.text)

    def test_cancel_before_start_runs_nothing(self):
        ag = self.make(["не должен вызываться"])
        stop = threading.Event()
        stop.set()
        out = ag.ask("привет", cancel=stop)
        self.assertEqual(self.sent, [])
        self.assertTrue(out.cancelled)

    def test_history_is_included(self):
        ag = self.make(["Да."])
        ag.ask("а завтра?", history=[("какая погода", "Солнечно, сэр.")])
        roles = [m["role"] for m in self.sent[0]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])

    def test_make_agent_respects_mode(self):
        ep = llm.Endpoint("http://x", "k", "m")
        with mock.patch.object(llm, "resolve_endpoint", lambda cfg: ep):
            self.assertIsNone(agent.make_agent(cfg_for_tests({"engine": {"mode": "hermes"}})))
            self.assertIsNotNone(agent.make_agent(cfg_for_tests({"engine": {"mode": "auto"}})))
            self.assertIsNotNone(agent.make_agent(cfg_for_tests({"engine": {"mode": "native"}})))
        with mock.patch.object(llm, "resolve_endpoint", lambda cfg: None):
            self.assertIsNone(agent.make_agent(cfg_for_tests()))


class QuickTests(unittest.TestCase):
    def test_rules(self):
        cases = {
            "который час": ["now"], "Какое сегодня число?": ["now"], "громче, пожалуйста": ["volume", "up"],
            "потише": ["volume", "down"], "выключи звук": ["volume", "mute"], "громкость 30 процентов": ["volume", "set", "30"],
            "громкость 500": ["volume", "set", "100"], "сверни все окна": ["minimize-all"],
            "сделай скриншот": ["screenshot"], "открой блокнот": ["open", "блокнот"],
            "Открой диспетчер задач": ["open", "диспетчер задач"], "запусти хром": ["open", "хром"],
            "курс доллара": ["rate", "доллара"], "какая погода": ["weather"],
        }
        for phrase, tokens in cases.items():
            self.assertEqual(quick.match(phrase), tokens, phrase)

    def test_everything_else_goes_to_the_model(self):
        for phrase in ["открой файл отчёт", "включи музыку", "расскажи анекдот", "громко ли тут", "погода в Париже на неделю",
                       "открой блокнот и напиши привет", "удали все файлы", "выключи компьютер"]:
            self.assertIsNone(quick.match(phrase), phrase)

    def test_every_quick_command_is_allowed_and_known_to_pc(self):
        for phrase in ["который час", "громче", "потише", "выключи звук", "громкость 30", "сверни окна", "скриншот",
                       "открой блокнот", "курс евро", "погода", "заблокируй компьютер", "включи игровой режим"]:
            tokens = quick.match(phrase)
            self.assertIsNotNone(tokens, phrase)
            self.assertIsNone(agent.check_allowed(tokens), phrase)

    def test_apps_match_pc_table(self):
        sys.path.insert(0, str(PC_SCRIPTS))
        try:
            import pc
        finally:
            sys.path.remove(str(PC_SCRIPTS))
        for app in quick.APPS:
            self.assertIn(app, pc.BUILTIN_APPS)
            self.assertEqual(quick.match("открой " + app), ["open", app], app)


class AssistantEngineTests(unittest.TestCase):
    def make(self, over=None, hermes=None):
        cfg = cfg_for_tests(over)
        self.sp = Speaker()
        a = Assistant(cfg, None, self.sp, hermes or Hermes(cfg["hermes"]), NullRecorder())
        a.run_pc = lambda tokens: (True, "pc: " + " ".join(tokens))
        return a

    def test_quick_request_skips_the_model(self):
        a = self.make()
        a.agent = mock.Mock()
        reply = a.think("громче")
        a.agent.ask.assert_not_called()
        self.assertEqual((reply.source, reply.text, reply.ok), ("quick", "pc: volume up", True))

    def test_other_requests_use_native_agent_not_hermes(self):
        a = self.make()
        a.agent = mock.Mock()
        a.agent.ask.return_value = agent.Answer("Привет, сэр.", steps=1)
        a.hermes = mock.Mock()
        reply = a.think("как дела")
        self.assertEqual((reply.source, reply.text), ("native", "Привет, сэр."))
        a.hermes.run.assert_not_called()

    def test_llm_failure_is_spoken_not_silent(self):
        a = self.make()
        a.agent = mock.Mock()
        a.agent.ask.side_effect = llm.LLMError("Закончился баланс.")
        events = []
        a.on_event = lambda k, t: events.append((k, t))
        a.answer("как дела")
        self.assertIn("Закончился баланс.", self.sp.said)
        self.assertEqual(events[-1], ("error", "Закончился баланс."))

    def test_without_agent_hermes_answers(self):
        a = self.make()
        reply = a.think("расскажи анекдот")
        self.assertEqual(reply.source, "hermes")
        self.assertIn("расскажи анекдот", reply.text)

    def test_hung_request_does_not_block_the_next_one(self):
        """Раньше единственный рабочий поток занимался зависшим Hermes, и вторая просьба оставалась без ответа."""
        a = self.make({"engine": {"deadline_s": 0.8}})
        gate = threading.Event()
        calls = []

        def ask(command, history, cancel, **kw):
            calls.append(command)
            if command == "зависни":
                gate.wait(10)
            return agent.Answer("ответ на " + command)
        a.agent = mock.Mock()
        a.agent.ask.side_effect = ask
        t0 = time.monotonic()
        first = a.answer("зависни")
        self.assertIn("Не дождался ответа", first)
        second = a.answer("как дела")
        gate.set()
        self.assertEqual(second, "ответ на как дела")
        self.assertLess(time.monotonic() - t0, 4)
        self.assertEqual(calls, ["зависни", "как дела"])

    def test_stop_interrupts_waiting_immediately(self):
        a = self.make()
        gate = threading.Event()
        a.agent = mock.Mock()
        a.agent.ask.side_effect = lambda c, h, cancel, **kw: (gate.wait(10), agent.Answer("поздно"))[1]
        result = []
        t = threading.Thread(target=lambda: result.append(a.answer("долгий вопрос")), daemon=True)
        t.start()
        time.sleep(0.4)
        t0 = time.monotonic()
        a.stop()
        t.join(3)
        gate.set()
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(result, [""])
        self.assertNotIn("поздно", self.sp.said)
        self.assertTrue(self.sp.cancel.is_set())

    def test_ack_is_spoken_when_answer_is_slow(self):
        a = self.make()
        a.agent = mock.Mock()
        a.agent.ask.side_effect = lambda c, h, cancel, **kw: time.sleep(0.8) or agent.Answer("Вот.")
        a.answer("подумай")
        self.assertEqual(self.sp.said[0], "Одну секунду, сэр.")
        self.assertEqual(self.sp.said[-1], "Вот.")

    def test_unexpected_crash_still_answers(self):
        a = self.make()
        a.agent = mock.Mock()
        a.agent.ask.side_effect = RuntimeError("boom")
        a.answer("привет")
        self.assertIn("Что-то пошло не так", self.sp.said[-1])

    def test_history_remembers_only_successful_answers(self):
        a = self.make()
        a.agent = mock.Mock()
        a.agent.ask.side_effect = [agent.Answer("Хорошо."), llm.LLMError("сбой")]
        a.answer("первое")
        a.answer("второе")
        self.assertEqual([h[0] for h in a.history], ["первое"])

    def test_timings_are_recorded(self):
        before = len(TIMINGS)
        a = self.make()
        a.think("громче")
        self.assertEqual(TIMINGS[-1][1], "quick")
        self.assertGreaterEqual(len(TIMINGS), min(before + 1, TIMINGS.maxlen))

    def test_stuck_hermes_process_is_killed_with_children(self):
        from tests.helpers import fake_exe
        exe = fake_exe("sleep 30 & sleep 30")
        t0 = time.monotonic()
        ok, text = Hermes({"exe": exe, "timeout_s": 1}).run("x")
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t0, 6)
        self.assertIn("не ответил", text)


if __name__ == "__main__":
    unittest.main()
