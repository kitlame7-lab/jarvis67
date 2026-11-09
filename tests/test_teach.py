"""Опрос ввода, запись и сборка задачи (без Windows: вместо системы подставлен FakeApi)."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from jarvis import tasks, teach, trace, winio

WIN = {"title": "Книга1 - Excel", "process": "EXCEL.EXE", "rect": [0, 0, 1000, 500]}
VK_A, VK_ENTER, VK_CTRL, VK_S, VK_SHIFT = 0x41, 0x0D, 0x11, 0x53, 0x10


class FakeApi:
    def __init__(self):
        self.pos, self.pressed, self.down, self.win = (0, 0), set(), set(), WIN
        self.password = False

    def cursor(self):
        return self.pos

    def buttons(self):
        return {n: n in self.pressed for n in winio.MOUSE_BUTTONS}

    def keys(self):
        return set(self.down)

    def window_at(self, x, y):
        return self.win

    def foreground(self):
        return self.win

    def char_for(self, vk, mods):
        if 0x41 <= vk <= 0x5A:
            c = chr(vk).lower()
            return c.upper() if "shift" in mods else c
        return " " if vk == 0x20 else None

    def focus_is_password(self):
        return self.password

    def screen_size(self):
        return (1000, 500)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class ManualSampler(winio.InputSampler):
    """Опрос делает тест, а не поток."""
    def start(self):
        self._t0 = self.clock()
        self._started = True

    def stop(self):
        return list(self.events)


class VkTests(unittest.TestCase):
    def test_names_do_not_depend_on_layout(self):
        self.assertEqual(winio.vk_name(0x41), "a")
        self.assertEqual(winio.vk_name(0x31), "1")
        self.assertEqual(winio.vk_name(0x0D), "enter")
        self.assertEqual(winio.vk_name(0x70), "f1")
        self.assertEqual(winio.vk_name(0x7B), "f12")
        self.assertEqual(winio.vk_name(0xBA), ";")
        self.assertEqual(winio.vk_name(0xFE), "vk254")

    def test_modifier_order(self):
        self.assertEqual(winio.modifiers_from({0xA0, 0x11, 0x5B}), ["win", "ctrl", "shift"])


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self.api, self.clock = FakeApi(), Clock()
        self.s = ManualSampler(self.api, clock=self.clock)

    def tick(self, dt=0.1):
        self.clock.t += dt
        self.s.poll()

    def test_click_produces_down_and_up_with_window(self):
        self.api.pos = (500, 250)
        self.tick()
        self.api.pressed = {"left"}
        self.tick()
        self.api.pressed = set()
        self.tick()
        kinds = [(e["type"], e["action"]) for e in self.s.events]
        self.assertEqual(kinds, [("mouse", "down"), ("mouse", "up")])
        self.assertEqual(self.s.events[0]["win"]["process"], "EXCEL.EXE")
        steps = trace.compile_steps(self.s.events, (1000, 500))
        self.assertEqual(steps[-1]["x"], 0.5)

    def test_held_button_gives_one_pair_only(self):
        self.api.pressed = {"left"}
        for _ in range(5):
            self.tick()
        self.assertEqual(len(self.s.events), 1)

    def test_typing_letters_and_shift(self):
        self.api.down = {VK_A}
        self.tick()
        self.api.down = set()
        self.tick()
        self.api.down = {VK_SHIFT, VK_A}
        self.tick()
        self.api.down = set()
        self.tick()
        chars = [e["char"] for e in self.s.events if e["type"] == "key"]
        self.assertEqual(chars, ["a", "A"])

    def test_ctrl_s_is_hotkey_without_char(self):
        self.api.down = {VK_CTRL, VK_S}
        self.tick()
        ev = [e for e in self.s.events if e["type"] == "key"]
        self.assertEqual(len(ev), 1)
        self.assertIsNone(ev[0]["char"])
        self.assertEqual((ev[0]["name"], ev[0]["mods"]), ("s", ["ctrl"]))
        self.assertEqual(trace.compile_steps(self.s.events)[-1]["keys"], ["ctrl", "s"])

    def test_password_field_marks_typing_secret(self):
        self.api.password = True
        self.api.down = {VK_A}
        self.tick()
        steps = trace.compile_steps(self.s.events)
        t = next(s for s in steps if s["kind"] == "type")
        self.assertTrue(t["secret"])
        self.assertNotIn("text", t)

    def test_listener_failure_does_not_stop_recording(self):
        self.s.listeners.append(lambda ev: 1 / 0)
        self.api.pressed = {"left"}
        self.tick()
        self.assertEqual(len(self.s.events), 1)

    def test_event_cap(self):
        s = ManualSampler(self.api, clock=self.clock, max_events=3)
        for i in range(10):
            self.api.pressed = {"left"} if i % 2 == 0 else set()
            self.clock.t += 0.1
            s.poll()
        self.assertEqual(len(s.events), 3)


class AnnotationTests(unittest.TestCase):
    STEPS = [{"i": 1, "kind": "window", "process": "x.exe", "title": "x"},
             {"i": 2, "kind": "click", "button": "left", "double": False, "window": {"title": "x", "process": "x.exe"}, "x": .1, "y": .1},
             {"i": 3, "kind": "type", "text": "секрет", "length": 6, "window": {}},
             {"i": 4, "kind": "click", "button": "left", "double": False, "window": {}, "x": .2, "y": .2},
             {"i": 5, "kind": "key", "key": "enter", "count": 1}]

    def test_prompt_hides_typed_text(self):
        p = teach.build_prompt(self.STEPS)
        self.assertNotIn("секрет", p)
        self.assertIn("6 симв.", p)

    def test_parse_plain_and_fenced_json(self):
        raw = '```json\n{"name": "Отчёт по продажам.", "phrases": ["сделай отчёт"], "notes": {"2": "открыть меню", "99": "x"}, "drop": [4, 77, "x"]}\n```'
        a = teach.parse_annotation(raw, 5)
        self.assertEqual(a["name"], "Отчёт по продажам")
        self.assertEqual(a["notes"], {2: "открыть меню"})
        self.assertEqual(a["drop"], [4])

    def test_parse_rejects_garbage(self):
        for bad in ("", "просто текст", "{не json}", '{"name": ""}', "[1,2]"):
            with self.subTest(bad), self.assertRaises(teach.TeachError):
                teach.parse_annotation(bad, 5)

    def test_only_clicks_can_be_dropped_and_not_too_many(self):
        steps = [dict(s) for s in self.STEPS]
        out = teach.apply_annotation(steps, {"drop": [1, 3, 4, 5], "notes": {2: "п"}})
        kinds = [s["kind"] for s in out]
        self.assertIn("type", kinds)
        self.assertIn("key", kinds)
        self.assertIn("window", kinds)
        self.assertEqual([s["i"] for s in out], list(range(1, len(out) + 1)))
        self.assertEqual(out[1]["note"], "п")
        self.assertEqual(len(out), 4)          # из 5 шагов можно убрать не больше двух, и только щелчок №4 из запрошенных годится

    def test_cannot_drop_more_than_share(self):
        steps = [{"i": n, "kind": "click"} for n in range(1, 11)]
        out = teach.apply_annotation(steps, {"drop": list(range(1, 11))})
        self.assertEqual(len(out), 6)


class TeacherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.api, self.clock = FakeApi(), Clock()
        self.sampler = ManualSampler(self.api, clock=self.clock)
        self.done = []
        self.states = []
        self.finished = threading.Event()
        self.asked = []

    def make(self, ask=None, grab=None, **kw):
        def on_done(task, message):
            self.done.append((task, message))
            self.finished.set()
        return teach.Teacher(lambda: self.sampler, ask=ask, grab=grab, root=self.root, on_done=on_done,
                             on_state=self.states.append, screen=lambda: (1000, 500), **kw)

    def act(self):
        """Щелчок и набор слова «аа» и Enter."""
        self.api.pos = (500, 250)
        for pressed, down in (({"left"}, set()), (set(), set()), (set(), {VK_A}), (set(), set()), (set(), {VK_A}),
                              (set(), set()), (set(), {VK_ENTER}), (set(), set())):
            self.api.pressed, self.api.down = pressed, down
            self.clock.t += 0.2
            self.sampler.poll()

    def wait(self):
        self.assertTrue(self.finished.wait(5), "сборка задачи не завершилась")
        return self.done[-1]

    def test_full_cycle_with_model_annotation(self):
        def ask(prompt):
            self.asked.append(prompt)
            return True, json.dumps({"name": "Пример", "description": "Делает пример", "phrases": ["сделай пример"],
                                     "notes": {"2": "щёлкнуть в ячейку"}, "drop": []}, ensure_ascii=False)
        t = self.make(ask=ask)
        self.assertTrue(t.start())
        self.assertFalse(t.start(), "повторный старт не должен начинать вторую запись")
        self.assertTrue(t.recording)
        self.act()
        self.assertGreater(t.stats()["events"], 0)
        self.assertTrue(t.stop())
        task, message = self.wait()
        self.assertEqual(task.name, "Пример")
        self.assertIn("готова", message)
        self.assertFalse(t.recording)
        self.assertEqual(self.states, [True, False])
        saved = tasks.load_task(task.slug, self.root)
        self.assertEqual([s["kind"] for s in saved.steps], ["window", "click", "type", "key"])
        self.assertEqual(saved.steps[2]["text"], "aa")
        self.assertEqual(saved.steps[1]["note"], "щёлкнуть в ячейку")
        self.assertNotIn("aa", self.asked[0], "набранный текст не уходит в нейросеть")
        self.assertFalse((self.root / "_recording").exists())

    def test_name_from_user_beats_model(self):
        t = self.make(ask=lambda p: (True, '{"name": "Другое", "phrases": []}'))
        t.start("Моя задача")
        self.act()
        t.stop()
        self.assertEqual(self.wait()[0].name, "Моя задача")

    def test_model_failure_falls_back_to_dated_name(self):
        t = self.make(ask=lambda p: (False, "сеть недоступна"))
        t.start()
        self.act()
        t.stop()
        task, _ = self.wait()
        self.assertTrue(task.name.startswith("Задача от "))
        self.assertTrue(task.steps)

    def test_bad_json_is_retried_once_then_default(self):
        calls = []

        def ask(prompt):
            calls.append(prompt)
            return True, "не json"
        t = self.make(ask=ask)
        t.start()
        self.act()
        t.stop()
        task, _ = self.wait()
        self.assertEqual(len(calls), 2)
        self.assertTrue(task.name.startswith("Задача от "))

    def test_empty_recording_creates_no_task(self):
        t = self.make(ask=lambda p: (True, '{"name": "x"}'))
        t.start()
        t.stop()
        task, message = self.wait()
        self.assertIsNone(task)
        self.assertIn("ничего", message)
        self.assertEqual(tasks.list_tasks(self.root), [])

    def test_discard_drops_everything(self):
        t = self.make()
        t.start()
        self.act()
        self.assertTrue(t.discard())
        self.assertFalse(t.discard())
        self.assertFalse(t.stop())
        self.assertEqual(self.states, [True, False])
        self.assertEqual(tasks.list_tasks(self.root), [])

    def test_auto_stop_on_time_limit(self):
        t = self.make(ask=lambda p: (True, '{"name": "Авто"}'), max_minutes=0.001)
        t.start()
        self.act()
        task, message = self.wait()
        self.assertEqual(task.name, "Авто")
        self.assertIn("по времени", message)

    def test_screenshots_taken_on_clicks_and_linked_to_steps(self):
        taken = []

        def grab(path):
            Path(path).write_bytes(b"jpg")
            taken.append(path)
        t = self.make(ask=lambda p: (True, '{"name": "Снимки"}'), grab=grab)
        t.start()
        self.act()
        t.stop()
        task, _ = self.wait()
        self.assertEqual(len(taken), 1)
        click = next(s for s in task.steps if s["kind"] == "click")
        self.assertEqual(click["shot"], "shots/001.jpg")
        self.assertTrue((self.root / task.slug / "shots" / "001.jpg").exists())

    def test_failing_screenshot_does_not_break_recording(self):
        def grab(path):
            raise OSError("нет доступа")
        t = self.make(ask=lambda p: (True, '{"name": "Без снимков"}'), grab=grab)
        t.start()
        self.act()
        t.stop()
        self.assertEqual(self.wait()[0].name, "Без снимков")

    def test_api_unavailable_reports_failure(self):
        def boom():
            raise OSError("только Windows")
        t = teach.Teacher(boom, root=self.root)
        self.assertFalse(t.start())
        self.assertIn("Windows", t.last_error)
        self.assertFalse(t.recording)


if __name__ == "__main__":
    unittest.main()
