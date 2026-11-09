"""Анализатор кода: каждое правило ловит плохой пример и пропускает хороший; сам репозиторий должен быть чист."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import doctor  # noqa: E402


def rules(src: str):
    return sorted({f.rule for f in doctor.analyze_source(src)})


class RuleTests(unittest.TestCase):
    def test_net_timeout(self):
        self.assertEqual(rules("import urllib.request\nurllib.request.urlopen(u)"), ["net-timeout"])
        self.assertEqual(rules("import urllib.request\nurllib.request.urlopen(u, timeout=5)"), [])

    def test_proc_timeout(self):
        self.assertIn("proc-timeout", rules("import subprocess\nsubprocess.run(['x'])"))
        self.assertEqual(rules("import subprocess\nsubprocess.run(['x'], timeout=3)"), [])
        self.assertEqual(rules("import subprocess\nsubprocess.Popen(['x'])"), [], "Popen не блокирует")

    def test_open_encoding(self):
        self.assertEqual(rules("open('a.txt')"), ["open-encoding"])
        self.assertEqual(rules("open('a.txt', encoding='utf-8')"), [])
        self.assertEqual(rules("open('a.bin', 'rb')"), [])
        self.assertEqual(rules("p.read_text()"), ["open-encoding"])
        self.assertEqual(rules("p.read_text(encoding='utf-8')"), [])
        self.assertEqual(rules("p.write_text('x')"), ["open-encoding"])
        self.assertEqual(rules("p.write_text('x', encoding='utf-8')"), [])
        self.assertIn("open-encoding", rules("import subprocess\nsubprocess.run(['x'], text=True, timeout=1)"))

    def test_top_level_windows_import(self):
        self.assertEqual(rules("import winreg"), ["top-import-win"])
        self.assertEqual(rules("def f():\n    import winreg\n"), [])

    def test_mutable_default(self):
        self.assertEqual(rules("def f(a=[]): pass"), ["mutable-default"])
        self.assertEqual(rules("def f(a=None): pass"), [])

    def test_powershell_interpolation(self):
        self.assertEqual(rules("def f(p):\n    return f'Add-Type -A X; $x = \"{p}\"'"), ["ps-interp"])
        self.assertEqual(rules("def f(p):\n    return 'Add-Type -A X; $x = $env:JARVIS_ARG'"), [])
        self.assertEqual(rules("def f(p):\n    return f'просто {p}'"), [])

    def test_waits(self):
        self.assertEqual(rules("t.join()"), ["wait-forever"])
        self.assertEqual(rules("t.join(5)"), [])
        self.assertEqual(rules("fut.result()"), ["wait-forever"])
        self.assertEqual(rules("self.q.get()"), ["queue-get"])
        self.assertEqual(rules("self.q.get(timeout=1)"), [])
        self.assertEqual(rules("os.path.join()"), [])

    def test_swallow_only_for_broad_except(self):
        self.assertEqual(rules("try:\n    f()\nexcept Exception:\n    pass"), ["swallow"])
        self.assertEqual(rules("try:\n    f()\nexcept:\n    pass"), ["swallow"])
        self.assertEqual(rules("try:\n    f()\nexcept OSError:\n    pass"), [])
        self.assertEqual(rules("try:\n    f()\nexcept Exception:\n    log.debug('x')"), [])

    def test_thread_daemon(self):
        self.assertEqual(rules("import threading\nthreading.Thread(target=f)"), ["thread-daemon"])
        self.assertEqual(rules("import threading\nthreading.Thread(target=f, daemon=True)"), [])

    def test_hot_loop(self):
        self.assertEqual(rules("while True:\n    x += 1"), ["hot-loop"])
        self.assertEqual(rules("while True:\n    time.sleep(1)"), [])
        self.assertEqual(rules("while True:\n    if x:\n        break"), [])

    def test_deprecated_tool(self):
        self.assertEqual(rules("run(['wmic', 'cpu'])"), ["deprecated-tool"])

    def test_pragma_silences_a_rule(self):
        self.assertEqual(rules("t.join()  # doctor: ok"), [])
        self.assertEqual(rules("t.join()  # doctor: ok(wait-forever)"), [])
        self.assertEqual(rules("t.join()  # doctor: ok(open-encoding)"), ["wait-forever"])

    def test_syntax_error_is_reported(self):
        self.assertEqual(rules("def ("), ["syntax"])


class RepoTests(unittest.TestCase):
    def test_repo_has_no_errors_or_warnings(self):
        report = doctor.collect(ROOT)
        bad = [f"{f['path']}:{f['line']} [{f['rule']}] {f['message']}" for f in report["findings"]]
        self.assertEqual(bad, [], "анализатор нашёл замечания: запустите python tools/doctor.py")
        self.assertEqual(report["pyflakes"], [])

    def test_skill_commands_exist_in_pc(self):
        self.assertEqual(doctor.check_skill_drift(ROOT), [])

    def test_skill_drift_is_detected(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res = Path(tmp) / "jarvis/resources/jarvis-pc"
            (res / "scripts").mkdir(parents=True)
            shutil.copy(ROOT / "jarvis/resources/jarvis-pc/scripts/pc.py", res / "scripts/pc.py")
            (res / "SKILL.md").write_text("| Просьба | Команда |\n|---|---|\n| сделай | `made-up-command x` |\n"
                                          "| время | `now` |\n", encoding="utf-8")
            found = doctor.check_skill_drift(Path(tmp))
            self.assertEqual([f.rule for f in found], ["skill-drift"])


if __name__ == "__main__":
    unittest.main()
