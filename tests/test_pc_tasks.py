"""Воспроизведение записанных задач (pc.py task …) на подставном вводе."""
import contextlib
import io as iolib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis" / "resources" / "jarvis-pc" / "scripts"))
os.environ["JARVIS_DRYRUN"] = "1"

import importlib  # noqa: E402
pc = importlib.import_module("pc")
from jarvis import tasks as jtasks  # noqa: E402

EXCEL = {"hwnd": 1, "title": "Книга1 - Excel", "process": "EXCEL.EXE", "rect": [100, 100, 1100, 600]}
CHROME = {"hwnd": 2, "title": "Почта - Chrome", "process": "chrome.exe", "rect": [0, 0, 1000, 500]}


class FakeInput:
    def __init__(self, windows=None, esc_after=None, launchable=()):
        self.wins = [dict(w) for w in (windows if windows is not None else [EXCEL, CHROME])]
        self.fg = None
        self.log = []
        self.esc_after = esc_after            # после стольких действий пользователь «нажимает» Esc
        self.launchable = set(launchable)

    def screen_size(self): return (2000, 1000)
    def windows(self): return [dict(w) for w in self.wins]
    def foreground(self): return dict(self.fg) if self.fg else None
    def focus(self, win): self.fg = next(w for w in self.wins if w["hwnd"] == win["hwnd"]); self.log.append(("focus", win["process"]))

    def launch(self, process):
        self.log.append(("launch", process))
        if process in self.launchable:
            self.wins.append({"hwnd": 9, "title": "Новая", "process": process, "rect": [0, 0, 800, 600]})
            return True
        return False

    def click(self, x, y, button="left", double=False): self.log.append(("click", x, y, button, double))
    def drag(self, x1, y1, x2, y2, button="left"): self.log.append(("drag", x1, y1, x2, y2))
    def type_text(self, text): self.log.append(("type", text))
    def key(self, name, count=1): self.log.append(("key", name, count))
    def hotkey(self, keys): self.log.append(("hotkey", tuple(keys)))

    def abort_requested(self):
        return self.esc_after is not None and len(self.log) >= self.esc_after


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def clock(self): return self.t
    def sleep(self, s): self.t += s


def play(task, io, speed=1.0):
    ft = FakeTime()
    p = pc.Player(io, task, speed, sleep=ft.sleep, clock=ft.clock)
    return p.run(), p, ft


def ref(win): return {"process": win["process"], "title": win["title"]}


def click_step(win=EXCEL, x=0.5, y=0.5, **kw):
    w, h = win["rect"][2] - win["rect"][0], win["rect"][3] - win["rect"][1]
    return {"kind": "click", "button": "left", "double": False, "window": ref(win), "size": [w, h], "x": x, "y": y,
            "sx": 0.3, "sy": 0.4, "delay": 0.5, **kw}


class PlayerTests(unittest.TestCase):
    def test_window_step_focuses_and_click_is_window_relative(self):
        io = FakeInput()
        task = {"name": "т", "steps": [{"kind": "window", "process": "EXCEL.EXE", "title": "Книга1", "delay": 0.5},
                                       click_step(x=0.5, y=0.25)]}
        (ok, msg), _, _ = play(task, io)
        self.assertTrue(ok, msg)
        self.assertIn(("focus", "EXCEL.EXE"), io.log)
        self.assertIn(("click", 600, 225, "left", False), io.log)        # 100 + 0.5*1000; 100 + 0.25*500

    def test_moved_window_keeps_click_inside_it(self):
        moved = dict(EXCEL, rect=[300, 200, 1300, 700])
        io = FakeInput([moved])
        (ok, _), _, _ = play({"name": "т", "steps": [click_step(x=0.1, y=0.2)]}, io)
        self.assertTrue(ok)
        self.assertIn(("click", 400, 300, "left", False), io.log)

    def test_resized_window_falls_back_to_screen_position_and_says_so(self):
        small = dict(EXCEL, rect=[0, 0, 200, 100])          # это, например, меню или другое окно
        io = FakeInput([small])
        (ok, msg), _, _ = play({"name": "т", "steps": [click_step()]}, io)
        self.assertTrue(ok)
        self.assertIn(("click", 600, 400, "left", False), io.log)       # 0.3*2000, 0.4*1000
        self.assertIn("положению на экране", msg)

    def test_missing_window_uses_screen_position(self):
        io = FakeInput([CHROME])
        (ok, _), _, _ = play({"name": "т", "steps": [click_step()]}, io)
        self.assertTrue(ok)
        self.assertIn(("click", 600, 400, "left", False), io.log)

    def test_program_not_running_is_launched_or_reported(self):
        step = {"kind": "window", "process": "EXCEL.EXE", "title": "", "delay": 0.5}
        (ok, msg), _, _ = play({"name": "т", "steps": [step]}, FakeInput([CHROME], launchable={"EXCEL.EXE"}))
        self.assertTrue(ok, msg)
        (ok, msg), _, _ = play({"name": "т", "steps": [step]}, FakeInput([CHROME]))
        self.assertFalse(ok)
        self.assertIn("шаге 1 из 1", msg)
        self.assertIn("EXCEL.EXE", msg)

    def test_typing_keys_hotkeys_and_double_click(self):
        io = FakeInput()
        steps = [{"kind": "type", "text": "привет", "delay": 0.5}, {"kind": "key", "key": "enter", "count": 2, "delay": 0.5},
                 {"kind": "hotkey", "keys": ["ctrl", "s"], "delay": 0.5}, click_step(double=True)]
        (ok, _), _, _ = play({"name": "т", "steps": steps}, io)
        self.assertTrue(ok)
        self.assertEqual(io.log[0], ("type", "привет"))
        self.assertEqual(io.log[1], ("key", "enter", 2))
        self.assertEqual(io.log[2], ("hotkey", ("ctrl", "s")))
        self.assertTrue(io.log[-1][4])

    def test_secret_step_stops_the_task(self):
        io = FakeInput()
        steps = [click_step(), {"kind": "type", "secret": True, "length": 8, "delay": 0.5}, {"kind": "key", "key": "enter", "delay": 0.5}]
        (ok, msg), _, _ = play({"name": "вход", "steps": steps}, io)
        self.assertFalse(ok)
        self.assertIn("шаге 2 из 3", msg)
        self.assertIn("пароль", msg)
        self.assertEqual([e[0] for e in io.log if e[0] in ("type", "key")], [], "после секретного шага ничего не нажимается")

    def test_escape_aborts_between_steps(self):
        io = FakeInput(esc_after=2)
        steps = [click_step() for _ in range(6)]
        (ok, msg), _, _ = play({"name": "т", "steps": steps}, io)
        self.assertFalse(ok)
        self.assertIn("Esc", msg)
        self.assertLess(len(io.log), 6)

    def test_recorded_escape_key_does_not_abort_itself(self):
        ft = FakeTime()

        class EscInput(FakeInput):
            """Нажатая задачей клавиша Esc какое-то время видна как «зажатая»."""
            pressed_until = -1.0

            def key(self, name, count=1):
                super().key(name, count)
                if name == "esc":
                    self.pressed_until = ft.t + 0.2

            def abort_requested(self):
                return ft.t < self.pressed_until
        io = EscInput()
        steps = [{"kind": "key", "key": "esc", "delay": 0.1}, {"kind": "key", "key": "enter", "delay": 0.1},
                 {"kind": "key", "key": "tab", "delay": 0.1}]
        ok, msg = pc.Player(io, {"name": "т", "steps": steps}, sleep=ft.sleep, clock=ft.clock).run()
        self.assertTrue(ok, msg)
        self.assertEqual([e[1] for e in io.log], ["esc", "enter", "tab"])

    def test_speed_scales_waits(self):
        steps = [{"kind": "key", "key": "tab", "delay": 2.0}] * 3
        (_, _), _, slow = play({"name": "т", "steps": steps}, FakeInput(), speed=1.0)
        (_, _), _, fast = play({"name": "т", "steps": steps}, FakeInput(), speed=2.0)
        self.assertAlmostEqual(slow.t, 6.0, delta=0.2)
        self.assertAlmostEqual(fast.t, 3.0, delta=0.2)

    def test_unknown_step_and_empty_task(self):
        (ok, msg), _, _ = play({"name": "т", "steps": [{"kind": "телепортация", "delay": 0}]}, FakeInput())
        self.assertFalse(ok)
        self.assertIn("неизвестный шаг", msg)
        (ok, msg), _, _ = play({"name": "пусто", "steps": []}, FakeInput())
        self.assertFalse(ok)

    def test_drag_uses_both_points(self):
        step = {"kind": "drag", "button": "left", "window": ref(EXCEL), "size": [1000, 500], "x": 0.1, "y": 0.1,
                "x2": 0.9, "y2": 0.9, "sx": 0.1, "sy": 0.1, "sx2": 0.5, "sy2": 0.5, "delay": 0.5}
        io = FakeInput()
        (ok, _), _, _ = play({"name": "т", "steps": [step]}, io)
        self.assertTrue(ok)
        self.assertEqual(io.log[-1], ("drag", 200, 150, 1000, 550))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.saved = os.environ.get("JARVIS_TASKS")
        os.environ["JARVIS_TASKS"] = str(self.root)
        self.addCleanup(lambda: os.environ.pop("JARVIS_TASKS", None) if self.saved is None else os.environ.__setitem__("JARVIS_TASKS", self.saved))

    def make(self, name, phrases=(), steps=None):
        t = jtasks.Task(name=name, phrases=list(phrases), steps=steps or [{"kind": "key", "key": "enter", "delay": 0.1, "i": 1}])
        jtasks.save_task(t, root=self.root)
        return t

    def run_cmd(self, *argv):
        buf = iolib.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pc.main(["task", *argv])
        return code, buf.getvalue()

    def test_list_show_run_delete(self):
        self.make("Отчёт по продажам", ["ежемесячный отчёт"])
        code, out = self.run_cmd("list")
        self.assertEqual(code, 0)
        self.assertIn("Отчёт по продажам — 1 шагов", out)
        code, out = self.run_cmd("show", "отчет", "по", "продажам")
        self.assertIn("клавиша enter", out)
        code, out = self.run_cmd("run", "ежемесячный", "отчет")
        self.assertEqual(code, 0)
        self.assertIn("Запустил задачу", out)
        self.assertIn("Esc", out)
        code, out = self.run_cmd("delete", "отчёт по продажам")
        self.assertIn("удалена", out)
        self.assertEqual(self.run_cmd("list")[1].startswith("Записанных задач пока нет"), True)

    def test_unknown_task_names_the_available_ones(self):
        self.make("Первая")
        code, out = self.run_cmd("run", "совсем другое")
        self.assertEqual(code, 1)
        self.assertIn("Не нашёл задачу", out)
        self.assertIn("Первая", out)

    def test_play_writes_last_run(self):
        self.make("Простая")
        code, out = self.run_cmd("play", "простая")
        self.assertEqual(code, 0, out)
        data = json.loads((self.root / "prostaya" / "last_run.json").read_text(encoding="utf-8"))
        self.assertTrue(data["ok"])

    def test_search_matches_the_app_side_implementation(self):
        self.make("Отчёт по продажам", ["ежемесячный отчёт"])
        self.make("Утренний запуск рабочих программ", ["запусти рабочее место"])
        self.make("Резервная копия")
        app_side = jtasks.list_tasks(self.root)
        pc_side = pc.load_tasks(self.root)
        for q in ("отчет по продажам", "ежемесячный отчет", "утренний запуск", "отчет по продажем", "рабочее место",
                  "копия", "резервная копия", "совсем другое", "", "запуск", "отчёт"):
            with self.subTest(q):
                a = jtasks.find_task(q, app_side)
                b = pc.find_task(q, pc_side)
                self.assertEqual(a.slug if a else None, b["slug"] if b else None)

    def test_broken_task_files_are_skipped(self):
        self.make("Хорошая")
        (self.root / "битая").mkdir()
        (self.root / "битая" / "task.json").write_text("{", encoding="utf-8")
        (self.root / "_recording").mkdir()
        self.assertEqual([t["name"] for t in pc.load_tasks(self.root)], ["Хорошая"])


if __name__ == "__main__":
    unittest.main()
