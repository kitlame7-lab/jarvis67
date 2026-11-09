"""Сборка шагов из событий, голосовые команды записи и хранилище задач."""
import tempfile
import unittest
from pathlib import Path

from jarvis import intents, tasks, trace

EXCEL = {"title": "Книга1 - Excel", "process": "EXCEL.EXE", "rect": [0, 0, 1000, 500]}
CHROME = {"title": "Почта - Chrome", "process": "chrome.exe", "rect": [100, 100, 1100, 600]}
LOGIN = {"title": "Вход в систему - Chrome", "process": "chrome.exe", "rect": [100, 100, 1100, 600]}
JARVIS = {"title": "Джарвис", "process": "Jarvis.exe", "rect": [0, 0, 500, 500]}


def click(t, x, y, win=EXCEL, button="left", hold=0.08):
    return [{"t": t, "type": "mouse", "action": "down", "button": button, "x": x, "y": y, "win": win},
            {"t": t + hold, "type": "mouse", "action": "up", "button": button, "x": x, "y": y, "win": win}]


def typed(t, text, win=EXCEL, step=0.1):
    return [{"t": t + i * step, "type": "key", "name": ch, "char": ch, "mods": [], "win": win} for i, ch in enumerate(text)]


def key(t, name, mods=(), win=EXCEL, char=None):
    return {"t": t, "type": "key", "name": name, "char": char, "mods": list(mods), "win": win}


class CompileTests(unittest.TestCase):
    def kinds(self, events):
        return [s["kind"] for s in trace.compile_steps(events)]

    def test_window_then_click_with_window_relative_coordinates(self):
        steps = trace.compile_steps(click(1.0, 500, 250))
        self.assertEqual([s["kind"] for s in steps], ["window", "click"])
        c = steps[1]
        self.assertEqual((c["x"], c["y"]), (0.5, 0.5))
        self.assertEqual(c["window"]["process"], "EXCEL.EXE")
        self.assertEqual(c["button"], "left")
        self.assertFalse(c["double"])

    def test_typing_is_merged_and_backspace_edits_the_text(self):
        events = typed(1.0, "приветт") + [key(1.9, "backspace")] + typed(2.0, " мир")
        steps = trace.compile_steps(events)
        types = [s for s in steps if s["kind"] == "type"]
        self.assertEqual(len(types), 1)
        self.assertEqual(types[0]["text"], "привет мир")
        self.assertEqual(types[0]["length"], 10)

    def test_long_pause_splits_typing(self):
        steps = trace.compile_steps(typed(1.0, "один") + typed(20.0, "два"))
        self.assertEqual([s["text"] for s in steps if s["kind"] == "type"], ["один", "два"])

    def test_enter_and_tab_are_separate_key_steps(self):
        steps = trace.compile_steps(typed(1.0, "да") + [key(1.5, "enter"), key(2.0, "tab")])
        self.assertEqual([s["kind"] for s in steps], ["window", "type", "key", "key"])
        self.assertEqual([s.get("key") for s in steps[2:]], ["enter", "tab"])

    def test_repeated_key_is_counted(self):
        steps = trace.compile_steps([key(1.0, "down"), key(1.2, "down"), key(1.4, "down")])
        keys = [s for s in steps if s["kind"] == "key"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["count"], 3)

    def test_hotkey(self):
        steps = trace.compile_steps([key(1.0, "s", ["ctrl"]), key(2.0, "t", ["ctrl", "shift"])])
        hk = [s["keys"] for s in steps if s["kind"] == "hotkey"]
        self.assertEqual(hk, [["ctrl", "s"], ["ctrl", "shift", "t"]])

    def test_double_click(self):
        steps = trace.compile_steps(click(1.0, 300, 200) + click(1.2, 301, 200))
        clicks = [s for s in steps if s["kind"] == "click"]
        self.assertEqual(len(clicks), 1)
        self.assertTrue(clicks[0]["double"])

    def test_two_separate_clicks_stay_two(self):
        steps = trace.compile_steps(click(1.0, 300, 200) + click(3.0, 300, 200))
        self.assertEqual(len([s for s in steps if s["kind"] == "click"]), 2)

    def test_drag(self):
        events = [{"t": 1.0, "type": "mouse", "action": "down", "button": "left", "x": 100, "y": 100, "win": EXCEL},
                  {"t": 1.6, "type": "mouse", "action": "up", "button": "left", "x": 400, "y": 300, "win": EXCEL}]
        drag = [s for s in trace.compile_steps(events) if s["kind"] == "drag"]
        self.assertEqual(len(drag), 1)
        self.assertEqual((drag[0]["x"], drag[0]["y"], drag[0]["x2"], drag[0]["y2"]), (0.1, 0.2, 0.4, 0.6))

    def test_right_click(self):
        steps = trace.compile_steps(click(1.0, 10, 10, button="right"))
        self.assertEqual(steps[-1]["button"], "right")

    def test_switching_programs_adds_window_step(self):
        steps = trace.compile_steps(click(1.0, 10, 10, EXCEL) + click(5.0, 200, 200, CHROME))
        self.assertEqual([s["kind"] for s in steps], ["window", "click", "window", "click"])
        self.assertEqual(steps[2]["process"], "chrome.exe")

    def test_title_change_in_same_program_does_not_add_window_step(self):
        other = dict(CHROME, title="Другая вкладка - Chrome")
        steps = trace.compile_steps(click(1.0, 200, 200, CHROME) + click(4.0, 300, 300, other))
        self.assertEqual([s["kind"] for s in steps].count("window"), 1)

    def test_password_window_text_is_not_stored(self):
        steps = trace.compile_steps(typed(1.0, "hunter2", LOGIN))
        t = next(s for s in steps if s["kind"] == "type")
        self.assertTrue(t["secret"])
        self.assertNotIn("text", t)
        self.assertEqual(t["length"], 7)
        self.assertNotIn("hunter2", str(steps))

    def test_inbox_window_is_not_mistaken_for_login(self):
        inbox = dict(CHROME, title="Входящие - Gmail")
        t = next(s for s in trace.compile_steps(typed(1.0, "привет", inbox)) if s["kind"] == "type")
        self.assertEqual(t["text"], "привет")
        self.assertNotIn("secret", t)

    def test_clicks_on_jarvis_itself_are_ignored(self):
        steps = trace.compile_steps(click(1.0, 50, 50, JARVIS) + click(3.0, 500, 250, EXCEL))
        self.assertEqual([s["kind"] for s in steps], ["window", "click"])
        self.assertEqual(steps[0]["process"], "EXCEL.EXE")

    def test_delays_are_clamped_and_numbered(self):
        steps = trace.compile_steps(click(1.0, 10, 10) + click(60.0, 10, 10))
        self.assertEqual([s["i"] for s in steps], list(range(1, len(steps) + 1)))
        self.assertLessEqual(max(s["delay"] for s in steps), trace.MAX_DELAY)
        self.assertGreaterEqual(min(s["delay"] for s in steps), 0)
        self.assertEqual(steps[-1]["delay"], trace.MAX_DELAY)

    def test_unsorted_events_are_ordered_by_time(self):
        events = click(3.0, 10, 10) + click(1.0, 900, 400)
        clicks = [s for s in trace.compile_steps(events) if s["kind"] == "click"]
        self.assertGreater(clicks[0]["x"], clicks[1]["x"])

    def test_step_count_is_capped(self):
        events = []
        for i in range(600):
            events += click(i * 2.0, 10 + (i % 50) * 10, 10)
        self.assertLessEqual(len(trace.compile_steps(events)), trace.MAX_STEPS)

    def test_empty_input(self):
        self.assertEqual(trace.compile_steps([]), [])

    def test_describe_hides_typed_text_by_default(self):
        steps = trace.compile_steps(typed(1.0, "секретный отчёт"))
        t = next(s for s in steps if s["kind"] == "type")
        self.assertNotIn("секретный", trace.describe_step(t))
        self.assertIn("15 симв.", trace.describe_step(t))
        self.assertIn("секретный", trace.describe_step(t, mask_text=False))
        self.assertIn("Ctrl+S", trace.describe_step({"kind": "hotkey", "keys": ["ctrl", "s"]}))


class IntentTests(unittest.TestCase):
    def test_start_phrases(self):
        for phrase in ("запомни как я это делаю", "запиши мои действия", "Запиши экран", "смотри как я работаю",
                       "научись этой задаче", "начни запись", "создай задачу по моим действиям",
                       "запомни, что я сейчас делаю", "повторяй за мной"):
            with self.subTest(phrase):
                self.assertEqual(getattr(intents.parse(phrase), "kind", None), "teach_start")

    def test_ordinary_requests_are_not_recording(self):
        for phrase in ("какая погода", "открой блокнот", "запомни слово арбуз", "выполни команду dir", "запусти хром", "найди файл"):
            with self.subTest(phrase):
                self.assertIsNone(intents.parse(phrase))

    def test_stop_only_counts_while_recording(self):
        self.assertIsNone(intents.parse("стоп"))
        self.assertEqual(intents.parse("стоп", recording=True).kind, "teach_stop")
        for phrase in ("хватит", "готово", "я закончил", "это всё", "останови запись", "сохрани"):
            with self.subTest(phrase):
                self.assertEqual(intents.parse(phrase, recording=True).kind, "teach_stop")

    def test_cancel_wins_over_stop(self):
        self.assertEqual(intents.parse("отмени запись, стоп", recording=True).kind, "teach_cancel")
        self.assertEqual(intents.parse("не сохраняй", recording=True).kind, "teach_cancel")

    def test_name_is_extracted(self):
        self.assertEqual(intents.parse("стоп, назови задачу отчёт по продажам", recording=True).arg, "отчёт по продажам")
        self.assertEqual(intents.parse("запиши мои действия, задача называется утренний запуск").arg, "утренний запуск")

    def test_run_task(self):
        i = intents.parse("выполни задачу отчёт по продажам пожалуйста")
        self.assertEqual((i.kind, i.arg), ("task_run", "отчёт по продажам"))
        self.assertEqual(intents.parse("запусти мою задачу утренний запуск").arg, "утренний запуск")
        self.assertEqual(intents.parse("Повтори задание бекап").kind, "task_run")
        self.assertIsNone(intents.parse("выполни задачу", recording=True), "во время записи запуск задач отключён")

    def test_other_speech_during_recording_is_ignored(self):
        self.assertIsNone(intents.parse("какая погода", recording=True))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def make(self, name, phrases=()):
        t = tasks.Task(name=name, phrases=list(phrases), steps=[{"kind": "key", "key": "enter", "i": 1}])
        tasks.save_task(t, root=self.root)
        return t

    def test_slugify(self):
        self.assertEqual(tasks.slugify("Отчёт по продажам"), "otchet-po-prodazham")
        self.assertEqual(tasks.slugify("???"), "task")
        self.assertEqual(tasks.slugify("Report 2026"), "report-2026")
        self.assertLessEqual(len(tasks.slugify("а" * 200)), 48)

    def test_save_load_roundtrip_and_unique_slug(self):
        a = self.make("Отчёт")
        b = self.make("Отчёт")
        self.assertEqual((a.slug, b.slug), ("otchet", "otchet-2"))
        loaded = tasks.load_task("otchet", self.root)
        self.assertEqual(loaded.name, "Отчёт")
        self.assertEqual(loaded.steps[0]["key"], "enter")
        self.assertTrue(loaded.created)

    def test_shots_are_moved_into_the_task(self):
        shots = self.root / "_rec" / "shots"
        shots.mkdir(parents=True)
        (shots / "001.jpg").write_bytes(b"x")
        t = tasks.Task(name="Со снимками")
        folder = tasks.save_task(t, shots_from=shots, root=self.root)
        self.assertTrue((folder / "shots" / "001.jpg").exists())
        self.assertFalse(shots.exists())

    def test_list_skips_service_folders_and_broken_files(self):
        self.make("Первая")
        (self.root / "_recording").mkdir()
        (self.root / "сломанная").mkdir()
        (self.root / "сломанная" / "task.json").write_text("{не json", encoding="utf-8")
        self.assertEqual([t.name for t in tasks.list_tasks(self.root)], ["Первая"])

    def test_find_by_name_phrase_and_fuzzy(self):
        self.make("Отчёт по продажам", ["ежемесячный отчёт"])
        self.make("Утренний запуск рабочих программ", ["запусти рабочее место"])
        ts = tasks.list_tasks(self.root)
        self.assertEqual(tasks.find_task("отчет по продажам", ts).name, "Отчёт по продажам")
        self.assertEqual(tasks.find_task("ежемесячный отчет", ts).name, "Отчёт по продажам")
        self.assertEqual(tasks.find_task("утренний запуск", ts).name, "Утренний запуск рабочих программ")
        self.assertEqual(tasks.find_task("отчет по продажем", ts).name, "Отчёт по продажам", "опечатка Whisper")
        self.assertIsNone(tasks.find_task("совсем другое", ts))
        self.assertIsNone(tasks.find_task("", ts))

    def test_delete_only_inside_tasks_folder(self):
        self.make("Удаляемая")
        self.assertFalse(tasks.delete_task("../etc", self.root))
        self.assertFalse(tasks.delete_task("", self.root))
        self.assertTrue(tasks.delete_task("udalyaemaya", self.root))
        self.assertEqual(tasks.list_tasks(self.root), [])


if __name__ == "__main__":
    unittest.main()
