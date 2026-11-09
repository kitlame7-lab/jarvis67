"""Реальное время: поток модели, озвучка по фразам, перебивание, эхо, ранняя нарезка речи."""
import io
import json
import queue
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np

from jarvis import agent, audio, core, llm
from jarvis.core import Assistant, Hermes, NullRecorder, Recorder, Speaker, deep_merge, load_settings
from jarvis.speech import StreamSplitter, echo_score, split_sentences
from tests.helpers import STUB


def cfg_for_tests(over=None):
    cfg = load_settings(Path("/nonexistent.json"))
    cfg["hermes"]["exe"] = str(STUB)
    cfg["hermes"]["ack_after_s"] = 5
    return deep_merge(cfg, over) if over else cfg


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def feed(chunks):
    s = StreamSplitter()
    out = []
    for c in chunks:
        out += s.feed(c)
    return out + s.flush()


class SplitterTests(unittest.TestCase):
    def test_sentences_come_out_as_soon_as_they_end(self):
        s = StreamSplitter()
        self.assertEqual(s.feed("Сейчас посмотрю, сэр. Сек"), ["Сейчас посмотрю, сэр."])
        self.assertEqual(s.feed("унду."), [])            # слишком коротко: ждём продолжения
        self.assertEqual(s.flush(), ["Секунду."])

    def test_action_lines_are_never_spoken(self):
        self.assertEqual(feed(["ДЕЙ", "СТВИЕ: sysinfo\n"]), [])
        self.assertEqual(feed(["Сейчас проверю.\nДЕЙСТВИЕ: now\n"]), ["Сейчас проверю."])
        self.assertEqual(feed(["action: volume up"]), [])

    def test_text_that_merely_starts_like_action_is_spoken(self):
        self.assertEqual(feed(["Дейст", "вительно, сэр, так и есть."]), ["Действительно, сэр, так и есть."])

    def test_long_phrase_without_dot_is_cut_at_comma(self):
        long = "Это очень длинное перечисление без точки, " * 5
        ready, rest = split_sentences(long, final=False)
        self.assertTrue(ready)
        self.assertTrue(ready[0].endswith(","))

    def test_echo_score(self):
        self.assertGreater(echo_score("сейчас посмотрю сэр", "Сейчас посмотрю, сэр. Секунду."), 0.9)
        self.assertLess(echo_score("а завтра будет дождь?", "Сейчас посмотрю, сэр."), 0.3)
        self.assertEqual(echo_score("", "что угодно"), 0.0)


class SseServer:
    """Поддельный сервер модели, отдающий ответ потоком (SSE) с паузами."""

    def __init__(self, pieces, delay=0.05, json_only=False, status=200):
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                srv.bodies.append(json.loads(self.rfile.read(n)))
                if status != 200:
                    body = json.dumps({"error": {"message": "bad key"}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if json_only:
                    body = json.dumps({"choices": [{"message": {"content": "".join(pieces)}}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for p in pieces:
                    chunk = {"choices": [{"delta": {"content": p}}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    srv.sent_at.append(time.monotonic())
                    time.sleep(delay)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.bodies, self.sent_at = [], []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class StreamChatTests(unittest.TestCase):
    def ep(self, srv):
        return llm.Endpoint(srv.url, "sk-secret", "m")

    def test_pieces_arrive_before_the_answer_is_finished(self):
        srv = SseServer(["Привет", ", сэр", ". Как", " дела?"], delay=0.2)
        self.addCleanup(srv.close)
        got = []
        for piece in llm.stream_chat(self.ep(srv), [{"role": "user", "content": "x"}]):
            got.append((piece, time.monotonic()))
        self.assertEqual("".join(p for p, _ in got), "Привет, сэр. Как дела?")
        self.assertTrue(srv.bodies[0]["stream"])
        self.assertLess(got[0][1], srv.sent_at[-1])          # первый кусок получен раньше последнего

    def test_think_tags_removed_across_pieces(self):
        srv = SseServer(["<thi", "nk>долго думаю</th", "ink>Ответ", " готов."], delay=0.01)
        self.addCleanup(srv.close)
        self.assertEqual("".join(llm.stream_chat(self.ep(srv), [])), "Ответ готов.")

    def test_server_without_streaming_still_works(self):
        srv = SseServer(["Целиком."], json_only=True)
        self.addCleanup(srv.close)
        self.assertEqual(list(llm.stream_chat(self.ep(srv), [])), ["Целиком."])

    def test_errors_explained_and_key_hidden(self):
        srv = SseServer([], status=401)
        self.addCleanup(srv.close)
        with self.assertRaises(llm.LLMError) as cm:
            list(llm.stream_chat(self.ep(srv), []))
        self.assertNotIn("sk-secret", str(cm.exception))

    def test_cancel_stops_reading(self):
        srv = SseServer([f"слово{i} " for i in range(50)], delay=0.05)
        self.addCleanup(srv.close)
        stop = threading.Event()
        got = []
        for piece in llm.stream_chat(self.ep(srv), [], cancel=stop):
            got.append(piece)
            if len(got) == 3:
                stop.set()
        self.assertLess(len(got), 10)


class AgentStreamingTests(unittest.TestCase):
    def make(self, steps, results=None):
        script = list(steps)
        self.ran = []

        def stream(ep, msgs, **kw):
            for piece in script.pop(0):
                yield piece

        def runner(tokens):
            self.ran.append(tokens)
            return (results or {}).get(" ".join(tokens), (True, "готово"))
        return agent.Agent(cfg_for_tests(), llm.Endpoint("http://x", "k", "m"), runner=runner, stream=stream,
                           chat=lambda *a, **k: self.fail("должен идти поток"))

    def test_sentences_spoken_while_streaming_and_actions_hidden(self):
        ag = self.make([["Сейчас проверю, сэр.", "\nДЕЙСТВИЕ: sysinfo"], ["Процессор свободен", " на 95 процентов."]],
                       {"sysinfo": (True, "CPU 5%")})
        spoken = []
        out = ag.ask("как компьютер", on_text=spoken.append)
        self.assertEqual(spoken, ["Сейчас проверю, сэр.", "Процессор свободен на 95 процентов."])
        self.assertEqual(self.ran, [["sysinfo"]])
        self.assertTrue(out.spoken)
        self.assertEqual(out.results, [("sysinfo", True, "CPU 5%")])

    def test_tools_and_progress_callback(self):
        sent = []
        tool = agent.Tool("send <путь>", "прислать файл", lambda args: (sent.append(args), (True, "отправлено"))[1])
        ag = self.make([["ДЕЙСТВИЕ: send C:\\a.txt"], ["Отправил."]])
        progress = []
        out = ag.ask("пришли файл", on_text=lambda s: None, on_action=progress.append, tools={"send": tool})
        self.assertEqual(sent, [["C:\\a.txt"]])
        self.assertEqual(progress, ["send C:\\a.txt"])
        self.assertEqual(self.ran, [])
        self.assertEqual(out.text, "Отправил.")

    def test_tool_crash_is_reported_not_raised(self):
        tool = agent.Tool("boom", "падает", lambda args: 1 / 0)
        ag = self.make([["ДЕЙСТВИЕ: boom"], ["Не вышло."]])
        out = ag.ask("x", on_text=lambda s: None, tools={"boom": tool})
        self.assertFalse(out.results[0][1])

    def test_unknown_tool_still_refused(self):
        ag = self.make([["ДЕЙСТВИЕ: send x"], ["Нельзя."]])
        out = ag.ask("x", on_text=lambda s: None)
        self.assertEqual(out.results[0][1], False)


class FakePlayer:
    def __init__(self):
        self.played, self.playing, self.gain = [], False, 1.0
        self._stop = threading.Event()

    def play(self, pcm, rate, cancel=None):
        self._stop.clear()
        self.playing = True
        self.played.append(pcm)
        try:
            end = time.monotonic() + len(pcm) / rate
            while time.monotonic() < end:
                if self._stop.is_set() or (cancel is not None and cancel.is_set()):
                    return False
                time.sleep(0.005)
            return True
        finally:
            self.playing = False

    def stop(self):
        self._stop.set()

    def duck(self, g=0.25):
        self.gain = g

    def unduck(self):
        self.gain = 1.0


def fake_speaker(seconds_per_phrase=0.05, render_delay=0.0):
    sp = Speaker(dict(core.DEFAULTS["tts"]), player=FakePlayer())
    rate = 1000
    rendered = []

    def render(text):
        time.sleep(render_delay)
        rendered.append(text)
        return np.zeros(int(rate * seconds_per_phrase), dtype="float32") + len(rendered), rate
    sp.render = render
    sp.rendered = rendered
    return sp


class SpeechSessionTests(unittest.TestCase):
    def test_phrases_play_in_order_and_next_is_prepared_while_playing(self):
        sp = fake_speaker(0.1, render_delay=0.05)
        s = sp.session()
        for t in ["Раз.", "Два.", "Три."]:
            s.push(t)
        s.close()
        self.assertTrue(s.wait(5))
        self.assertEqual(s.said, ["Раз.", "Два.", "Три."])
        self.assertEqual([int(p[0]) for p in sp.player.played], [1, 2, 3])

    def test_stop_is_immediate(self):
        sp = fake_speaker(2.0)
        s = sp.session()
        s.push("Очень длинная фраза.")
        s.push("Ещё одна.")
        self.assertTrue(wait_for(lambda: sp.playing, 3))
        t0 = time.monotonic()
        s.stop()
        self.assertFalse(s.wait(2))
        self.assertTrue(wait_for(lambda: not sp.playing, 1))
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertEqual(len(sp.player.played), 1)

    def test_recent_text_for_echo_filter(self):
        sp = fake_speaker(0.01)
        s = sp.session()
        s.push("Погода отличная, сэр.")
        s.close()
        s.wait(3)
        self.assertIn("Погода отличная", sp.recent_text(5))

    def test_disabled_voice_finishes_silently(self):
        sp = Speaker(dict(core.DEFAULTS["tts"], enabled=False), player=FakePlayer())
        s = sp.session()
        s.push("Тишина.")
        s.close()
        self.assertTrue(s.wait(3))
        self.assertEqual(sp.player.played, [])


class BargeInTests(unittest.TestCase):
    def make(self, over=None):
        cfg = cfg_for_tests(over)
        self.sp = fake_speaker(0.6)
        a = Assistant(cfg, None, self.sp, Hermes(cfg["hermes"]), NullRecorder())
        a.run_pc = lambda tokens: (True, "pc: " + " ".join(tokens))
        self.calls = []

        def ask(command, history, cancel, on_text=None, **kw):
            self.calls.append((command, list(history)))
            if command.startswith("долг"):
                for i in range(20):
                    if cancel.is_set():
                        break
                    on_text(f"Предложение номер {i} про погоду.")
                    time.sleep(0.05)
                return agent.Answer("…", spoken=True)
            on_text("Ответ на уточнение готов.")
            return agent.Answer("Ответ на уточнение готов.", spoken=True)
        a.agent = mock.Mock()
        a.agent.ask.side_effect = ask
        return a

    def test_first_words_spoken_before_answer_is_complete(self):
        a = self.make()
        t = threading.Thread(target=a.answer, args=("долгий рассказ",), daemon=True)
        t.start()
        self.assertTrue(wait_for(lambda: self.sp.playing, 2))
        self.assertTrue(t.is_alive())                      # модель ещё пишет, а Джарвис уже говорит
        a.interrupt()
        t.join(3)

    def test_new_phrase_interrupts_and_is_answered(self):
        a = self.make()
        t = threading.Thread(target=a.answer, args=("долгий рассказ",), daemon=True)
        t.start()
        self.assertTrue(wait_for(lambda: self.sp.playing, 2))
        t0 = time.monotonic()
        a.on_heard("а завтра что обещают")
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(wait_for(lambda: "Ответ на уточнение готов." in self.sp.recent_text(10), 3))
        # уточнение знает, о чём шла речь
        self.assertTrue(wait_for(lambda: len(self.calls) == 2, 2))
        history = self.calls[1][1]
        self.assertEqual(history[-1][0], "долгий рассказ")
        self.assertIn("перебили", history[-1][1])

    def test_own_echo_does_not_interrupt(self):
        a = self.make()
        t = threading.Thread(target=a.answer, args=("долгий рассказ",), daemon=True)
        t.start()
        self.assertTrue(wait_for(lambda: self.sp.playing, 2))
        a.on_heard("предложение номер про погоду")
        time.sleep(0.2)
        self.assertTrue(t.is_alive())
        self.assertEqual(len(self.calls), 1)
        a.interrupt()
        t.join(3)

    def test_stop_word_silences_without_new_answer(self):
        a = self.make()
        t = threading.Thread(target=a.answer, args=("долгий рассказ",), daemon=True)
        t.start()
        self.assertTrue(wait_for(lambda: self.sp.playing, 2))
        a.on_heard("Джарвис, хватит")
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertFalse(self.sp.playing)
        time.sleep(0.2)
        self.assertEqual(len(self.calls), 1)

    def test_wake_mode_needs_the_name_to_interrupt(self):
        a = self.make({"audio": {"barge_in": "wake"}})
        t = threading.Thread(target=a.answer, args=("долгий рассказ",), daemon=True)
        t.start()
        self.assertTrue(wait_for(lambda: self.sp.playing, 2))
        a.on_heard("какая-то посторонняя речь в комнате")
        time.sleep(0.2)
        self.assertTrue(t.is_alive())
        a.on_heard("Джарвис, а завтра")
        t.join(2)
        self.assertFalse(t.is_alive())

    def test_idle_phrase_without_name_is_ignored(self):
        a = self.make()
        a.last_reply_at = 0
        a.on_heard("я просто разговариваю с другом")
        time.sleep(0.2)
        self.assertEqual(self.calls, [])

    def test_quick_command_is_spoken(self):
        a = self.make()
        a.answer("громче")
        self.assertIn("pc: volume up", self.sp.recent_text(5))


class SegmentTests(unittest.TestCase):
    def make_rec(self, levels, over=None):
        rec = Recorder.__new__(Recorder)          # без звуковой карты
        rec.np, rec.cfg = np, deep_merge(dict(core.DEFAULTS["audio"]), over or {})
        rec.q, rec.stop = queue.Queue(), threading.Event()
        rec._mutes, rec._lock, rec.paused, rec._stream = 0, threading.Lock(), False, None
        rec.boost = lambda: 1.0
        block = rec._block()
        for lv in levels:
            rec.q.put(np.full(block, lv, dtype="float32"))
        return rec

    def events(self, rec):
        out = []
        threading.Timer(1.0, rec.stop.set).start()
        for kind, snd in rec.segments():
            if kind != "voice":
                out.append((kind, None if snd is None else len(snd)))
            else:
                self.voiced = True
        return out

    def test_draft_comes_before_final(self):
        speech = [0.2] * 20                           # 600 мс речи
        rec = self.make_rec([0.0] * 5 + speech + [0.0] * 25)
        kinds = [k for k, _ in self.events(rec)]
        self.assertEqual(kinds, ["start", "draft", "final"])

    def test_resumed_speech_makes_new_draft(self):
        rec = self.make_rec([0.2] * 20 + [0.0] * 12 + [0.2] * 15 + [0.0] * 25)
        kinds = [k for k, _ in self.events(rec)]
        self.assertEqual(kinds, ["start", "draft", "draft", "final"])

    def test_short_noise_dropped(self):
        rec = self.make_rec([0.2] * 3 + [0.0] * 25)
        self.assertEqual([k for k, _ in self.events(rec)], ["start", "drop"])

    def test_boost_ignores_quiet_echo_while_speaking(self):
        rec = self.make_rec([0.02] * 30 + [0.0] * 25)
        rec.boost = lambda: 3.0                       # порог 0.036: тихое эхо не считается речью
        self.assertEqual(self.events(rec), [])


class ListenerTests(unittest.TestCase):
    def test_final_uses_ready_draft(self):
        cfg = cfg_for_tests()
        sp = fake_speaker(0.01)
        calls = []

        class Stt:
            def transcribe(self, audio):
                calls.append(len(audio))
                return "Джарвис, громче"

        class Rec:
            boost = None

            def segments(self):
                a = np.zeros(1600, dtype="float32")
                yield "start", None
                yield "draft", a
                yield "final", a

            def mute(self):
                pass

            def unmute(self):
                pass
        a = Assistant(cfg, Stt(), sp, Hermes(cfg["hermes"]), Rec())
        a.run_pc = lambda tokens: (True, "громкость выше")
        a.run(threading.Event())
        self.assertEqual(calls, [1600])                # распознано один раз, на черновике
        self.assertTrue(wait_for(lambda: "громкость выше" in sp.recent_text(5), 3))


class AudioDecodeTests(unittest.TestCase):
    def test_wav_decoded_to_mono_float(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes((np.ones((100, 2)) * 16384).astype("<i2").tobytes())
        pcm, rate = audio.decode_audio(buf.getvalue())
        self.assertEqual((rate, len(pcm)), (22050, 100))
        self.assertAlmostEqual(float(pcm[0]), 0.5, places=3)

    def test_compressed_audio_via_pyav(self):
        import av
        buf = io.BytesIO()
        with av.open(buf, "w", format="ogg") as out:
            st = out.add_stream("libopus" if "libopus" in av.codecs_available else "flac", rate=48000)
            st.layout = "mono"
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 4800), dtype="int16"), format="s16", layout="mono")
            frame.rate = 48000
            for p in st.encode(frame):
                out.mux(p)
            for p in st.encode(None):
                out.mux(p)
        pcm, rate = audio.decode_audio(buf.getvalue())
        self.assertGreater(len(pcm), 1000)
        self.assertEqual(pcm.dtype, np.float32)


if __name__ == "__main__":
    unittest.main()
