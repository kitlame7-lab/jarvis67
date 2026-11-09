"""Логика ассистента без микрофона и без настоящего Hermes."""
import asyncio
import os
import time
import unittest
from pathlib import Path


from helpers import CALLS, LAST_PROMPT, STUB, env, fake_exe
from jarvis import core
from jarvis.core import (Assistant, Hermes, NullRecorder, Recorder, Speaker, clean_for_speech, deep_merge,
                         detect_wake, explain_hermes_error, is_hallucination, load_settings)


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


def make(over=None):
    cfg = load_settings(Path("/nonexistent.json"))
    cfg["hermes"]["exe"] = str(STUB)
    cfg["hermes"]["ack_after_s"] = 0.4
    if over:
        cfg = deep_merge(cfg, over)
    sp = FakeSpeaker()
    a = Assistant(cfg, None, sp, Hermes(cfg["hermes"]), NullRecorder())
    a.run_pc = lambda tokens: (True, "pc: " + " ".join(tokens))      # настоящие команды Windows в тестах не нужны
    return a, sp


class WakeTests(unittest.TestCase):
    W = core.DEFAULTS["wake"]["words"]
    R = core.DEFAULTS["wake"]["fuzzy_ratio"]

    def test_variants(self):
        for phrase in ["Джарвис, сделай ещё одну копию", "джарвиз почисти диск", "Жарвис погода",
                       "Jarvis create a task", "Эй, Джарвис, напомни", "Джервис привет",
                       "Шарвис включи свет", "Джарвист, привет", "Джарфис открой блокнот",
                       "Чарвис сделай что-нибудь"]:
            self.assertTrue(detect_wake(phrase, self.W, self.R).triggered, phrase)

    def test_no_false_positive(self):
        for phrase in ["сервис доставки работает плохо", "просто разговор", "какая погода завтра",
                       "дарвин был прав", "марвин пришёл", "карвис купил машину", "джаз сегодня играет",
                       "сарвис не работает", "джинсы постирай"]:
            self.assertFalse(detect_wake(phrase, self.W, self.R).triggered, phrase)

    def test_command_keeps_original_text(self):
        r = detect_wake("Джарвис, найди файл «Договор №5.pdf» и пришли мне", self.W, self.R)
        self.assertEqual(r.command, "найди файл «Договор №5.pdf» и пришли мне")
        self.assertEqual(detect_wake("Чарвис сделай ещё", self.W, self.R).command, "сделай ещё")

    def test_name_only(self):
        self.assertEqual(detect_wake("Джарвис.", self.W, self.R).command, "")


class TextTests(unittest.TestCase):
    def test_hallucination(self):
        self.assertTrue(is_hallucination("Субтитры сделал DimaTorzok"))
        self.assertTrue(is_hallucination("да да да да да"))
        self.assertFalse(is_hallucination("Джарвис, расскажи анекдот"))

    def test_clean_speech(self):
        out = clean_for_speech("**Готово**, сэр! 😀 Смотрите https://x.org `код`")
        for bad in ("*", "http", "😀"):
            self.assertNotIn(bad, out)
        self.assertIn("ссылка", out)

    def test_clean_speech_truncates_on_sentence(self):
        out = clean_for_speech("Первое предложение. " * 80, 200)
        self.assertIn("Остальное смотрите в журнале", out)
        self.assertLess(len(out), 260)


class HermesTests(unittest.TestCase):
    def test_argv(self):
        h = Hermes({"exe": str(STUB), "skills": "jarvis-pc", "yolo": False, "extra_args": []})
        a = h.build_argv("q.txt")
        self.assertEqual(a[1:7], ["chat", "--oneshot", "--quiet", "--source", "tool", "--query-file"])
        self.assertIn("--skills", a)
        self.assertNotIn("--yolo", a)
        h.cfg["yolo"] = True
        self.assertIn("--yolo", h.build_argv("q.txt"))

    def test_provider_and_model_flags_only_when_set(self):
        h = Hermes({"exe": str(STUB), "provider": "", "model": ""})
        self.assertNotIn("--provider", h.build_argv("q"))
        h.cfg.update(provider="deepseek", model="deepseek-chat")
        a = h.build_argv("q")
        self.assertEqual(a[a.index("--provider") + 1], "deepseek")
        self.assertEqual(a[a.index("-m") + 1], "deepseek-chat")

    def test_ask_and_cleanup(self):
        tmp = Path(os.environ.get("TMPDIR", "/tmp"))
        h = Hermes({"exe": str(STUB), "skills": "jarvis-pc", "timeout_s": 20, "extra_args": []})
        before = set(tmp.glob("jarvis-q-*"))
        out = h.ask("первая строка\nПользователь сейчас говорит: привет «ёжик» \"кавычки\"")
        self.assertIn("привет «ёжик»", out)
        self.assertEqual(set(tmp.glob("jarvis-q-*")), before)

    def test_failure_message(self):
        with env(STUB_FAIL="1"):
            ok, out = Hermes({"exe": str(STUB), "timeout_s": 20, "extra_args": []}).run("x")
        self.assertFalse(ok)
        self.assertIn("не смог ответить", out)

    def test_missing_exe(self):
        h = Hermes({"exe": "/нет/такого", "timeout_s": 5})
        h.exe = None
        self.assertIn("Не нашёл Hermes", h.ask("x"))

    def test_no_keyboard_input(self):
        ok, text = Hermes({"exe": fake_exe("readlink /proc/self/fd/0"), "timeout_s": 10}).run("x")
        self.assertTrue(ok)
        self.assertEqual(text.strip(), "/dev/null")


class FlowTests(unittest.TestCase):
    def test_wake_with_command(self):
        a, sp = make()
        self.assertIsNotNone(a.handle_transcript("Джарвис, расскажи анекдот"))
        self.assertTrue(any("расскажи анекдот" in s for s in sp.said))

    def test_events_reach_the_window(self):
        a, _ = make()
        events = []
        a.on_event = lambda kind, text: events.append(kind)
        a.handle_transcript("Джарвис, привет")
        self.assertEqual(events, ["heard", "reply"])

    def test_broken_event_handler_does_not_break_assistant(self):
        a, sp = make()

        def boom(kind, text):
            raise RuntimeError("окно закрыто")
        a.on_event = boom
        self.assertIsNotNone(a.handle_transcript("Джарвис, привет"))
        self.assertTrue(sp.said)

    def test_name_only_then_followup(self):
        a, sp = make()
        self.assertEqual(a.handle_transcript("Джарвис"), "")
        self.assertIn("Слушаю, сэр.", sp.said[0])
        self.assertIsNotNone(a.handle_transcript("включи игровой режим"))

    def test_ignores_chatter_without_name(self):
        a, sp = make()
        self.assertIsNone(a.handle_transcript("я думаю нам пора обедать"))
        self.assertEqual(sp.said, [])

    def test_hallucination_ignored_even_in_window(self):
        a, _ = make()
        a.last_reply_at = time.time()
        self.assertIsNone(a.handle_transcript("Продолжение следует"))

    def test_followup_expires(self):
        a, _ = make()
        a.last_reply_at = time.time() - 60
        self.assertIsNone(a.handle_transcript("включи игровой режим"))

    def test_single_word_in_window_ignored(self):
        a, _ = make()
        a.last_reply_at = time.time()
        self.assertIsNone(a.handle_transcript("угу"))

    def test_history_goes_into_next_prompt(self):
        a, _ = make()
        a.handle_transcript("Джарвис, запомни слово арбуз")
        a.handle_transcript("Джарвис, какое слово я назвал")
        text = LAST_PROMPT.read_text(encoding="utf-8")
        for frag in ("Недавний разговор", "арбуз", "сэр", "jarvis-pc", "проактивным"):
            self.assertIn(frag, text)

    def test_history_expires(self):
        a, _ = make({"history_ttl_s": 0.1})
        a.handle_transcript("Джарвис, запомни слово арбуз")
        time.sleep(0.2)
        self.assertNotIn("Недавний разговор", a.build_prompt("привет"))

    def test_ack_when_slow(self):
        with env(STUB_SLEEP="1.2"):
            a, sp = make()
            a.handle_transcript("Джарвис, сделай что-нибудь долгое")
        self.assertTrue(sp.said[0].startswith("Одну секунду"))
        self.assertEqual(len(sp.said), 2)

    def test_no_ack_when_fast(self):
        a, sp = make()
        a.handle_transcript("Джарвис, привет")
        self.assertEqual(len(sp.said), 1)


class HermesErrorTests(unittest.TestCase):
    def test_known_errors_are_russian(self):
        cases = {
            "Hermes is not connected to any AI provider": "не подключён",
            "DeepSeek rejected your API key (HTTP 401)": "не приняла ключ",
            "HTTP 402 Insufficient Balance": "закончились средства",
            "HTTP 429 rate limit": "перегружена",
        }
        for raw, frag in cases.items():
            msg = explain_hermes_error(raw)
            self.assertIsNotNone(msg, raw)
            self.assertIn(frag, msg)

    def test_missing_credentials_message(self):
        raw = "No usable credentials found for provider 'deepseek'. Set DEEPSEEK_API_KEY."
        msg = explain_hermes_error(raw)
        self.assertIsNotNone(msg)
        self.assertIn("ключ", msg)

    def test_normal_answer_untouched(self):
        self.assertIsNone(explain_hermes_error("В Москве сейчас +5, облачно."))
        self.assertIsNone(explain_hermes_error(""))


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(core.DEFAULTS["tts"])
        self.sp = Speaker(self.cfg)

    def test_defaults_have_no_elevenlabs(self):
        self.assertNotIn("elevenlabs", self.cfg)
        self.assertFalse(hasattr(self.sp, "eleven_ready"))

    def test_attempts_order_and_remembering(self):
        a = self.sp.attempts()
        self.assertEqual(a[0][0], self.cfg["voice"])
        self.assertEqual(a[-1][0], "ru-RU-SvetlanaNeural")
        self.sp._choice = a[-1]
        self.assertEqual(self.sp.attempts()[0], a[-1])
        self.assertEqual(len(self.sp.attempts()), len(a))

    def test_spare_differs_when_svetlana_is_main(self):
        sp = Speaker(dict(self.cfg, voice="ru-RU-SvetlanaNeural"))
        self.assertEqual(sp.attempts()[-1][0], "ru-RU-DmitryNeural")

    def test_synth_falls_through_variants(self):
        calls = []

        async def fake(text, path, voice, rate, pitch):
            calls.append((voice, rate, pitch))
            if len(calls) < 3:
                raise RuntimeError("No audio was received")
            Path(path).write_bytes(b"x" * 1000)
        self.sp._save = fake
        out = Path(os.environ["JARVIS_HOME"]) / "t.mp3"
        self.sp.synth("Привет", str(out))
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sp._choice[0], "ru-RU-SvetlanaNeural")

    def test_synth_raises_when_all_fail(self):
        async def fake(*a, **k):
            raise RuntimeError("No audio was received")
        self.sp._save = fake
        with self.assertRaises(RuntimeError):
            self.sp.synth("Привет", str(Path(os.environ["JARVIS_HOME"]) / "t2.mp3"))

    def test_cooldown_skips_neural(self):
        old, core.IS_WIN = core.IS_WIN, True
        self.addCleanup(setattr, core, "IS_WIN", old)
        hits = []
        self.sp._say_neural = lambda t: (_ for _ in ()).throw(RuntimeError("down"))
        self.sp._say_sapi = lambda t: hits.append(t)
        self.sp.say("Раз")
        self.assertGreater(self.sp._down_until, time.time())
        neural = []
        self.sp._say_neural = lambda t: neural.append(t)
        self.sp.say("Два")
        self.assertEqual(neural, [])
        self.assertEqual(len(hits), 2)

    def test_neural_first_when_available(self):
        old, core.IS_WIN = core.IS_WIN, True
        self.addCleanup(setattr, core, "IS_WIN", old)
        order = []
        self.sp._say_neural = lambda t: order.append("neural")
        self.sp._say_sapi = lambda t: order.append("sapi")
        self.sp.say("Раз")
        self.assertEqual(order, ["neural"])

    def test_sapi_backend_skips_neural(self):
        old, core.IS_WIN = core.IS_WIN, True
        self.addCleanup(setattr, core, "IS_WIN", old)
        sp = Speaker(dict(self.cfg, backend="sapi"))
        order = []
        sp._say_neural = lambda t: order.append("neural")
        sp._say_sapi = lambda t: order.append("sapi")
        sp.say("Раз")
        self.assertEqual(order, ["sapi"])

    def test_disabled_is_silent(self):
        old, core.IS_WIN = core.IS_WIN, True
        self.addCleanup(setattr, core, "IS_WIN", old)
        sp = Speaker(dict(self.cfg, enabled=False))
        sp._say_neural = lambda t: self.fail("не должен говорить")
        sp._say_sapi = lambda t: self.fail("не должен говорить")
        sp.say("Раз")

    def test_real_edge_save_signature(self):
        # асинхронная функция существует и принимает те же аргументы, что и подставная
        self.assertTrue(asyncio.iscoroutinefunction(Speaker._save))


class VoiceListTests(unittest.TestCase):
    def test_fallback_when_offline(self):
        import builtins
        real_import = builtins.__import__

        def no_edge(name, *a, **k):
            if name == "edge_tts":
                raise ImportError("нет")
            return real_import(name, *a, **k)
        builtins.__import__ = no_edge
        try:
            voices = core.list_voices()
        finally:
            builtins.__import__ = real_import
        self.assertTrue(voices)
        self.assertTrue(all(v["name"].startswith("ru-") for v in voices))
        self.assertIn("ru-RU-DmitryNeural", [v["name"] for v in voices])


class SelftestTests(unittest.TestCase):
    def test_selftest_prints_live_and_skips_skill_when_model_is_broken(self):
        exe = fake_exe("echo 'Hermes is not connected to any AI provider'; exit 1")
        cfg = load_settings(Path("/nonexistent.json"))
        cfg["hermes"]["exe"] = exe
        lines, t0 = [], time.time()
        rc = core.run_selftest(cfg, lines.append)
        out = "\n".join(lines)
        self.assertEqual(rc, 1)
        self.assertLess(time.time() - t0, 60)
        self.assertIn("модель отвечает", out)
        self.assertIn("не подключён", out)
        self.assertIn("пропущено", out)

    def test_selftest_reports_missing_credentials_in_russian(self):
        exe = fake_exe("echo \"No usable credentials found for provider 'deepseek'. Set DEEPSEEK_API_KEY.\"; exit 1")
        cfg = load_settings(Path("/nonexistent.json"))
        cfg["hermes"]["exe"] = exe
        lines = []
        core.run_selftest(cfg, lines.append)
        self.assertNotIn("No usable credentials", "\n".join(l for l in lines if "модель отвечает" in l))


class RecorderMuteTests(unittest.TestCase):
    def test_mute_refcount(self):
        import queue
        import threading
        r = Recorder.__new__(Recorder)
        r.q, r._mutes, r._lock = queue.Queue(), 0, threading.Lock()
        r.mute()
        r.mute()
        r.unmute()
        self.assertEqual(r._mutes, 1)
        r.unmute()
        self.assertEqual(r._mutes, 0)
        r.unmute()
        self.assertEqual(r._mutes, 0)


class SettingsLoadTests(unittest.TestCase):
    def test_reads_current_settings_path(self):
        from jarvis import paths
        paths.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        paths.SETTINGS_PATH.write_text('{"city": "Казань"}', encoding="utf-8")
        self.assertEqual(load_settings()["city"], "Казань")

    def test_broken_file_falls_back_to_defaults(self):
        from jarvis import paths
        paths.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        paths.SETTINGS_PATH.write_text("{не json", encoding="utf-8")
        self.assertEqual(load_settings()["city"], core.DEFAULTS["city"])

    def test_calls_log_exists_after_run(self):
        Hermes({"exe": str(STUB), "timeout_s": 20}).ask("x")
        self.assertTrue(CALLS.exists())
