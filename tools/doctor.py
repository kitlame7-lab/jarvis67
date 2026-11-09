#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Анализатор кода Джарвиса: ищет места, из-за которых приложение зависает, ломается на Windows
или теряет ошибки. Запускается без аргументов и без участия человека:

    python tools/doctor.py            # отчёт, код возврата 1 при ошибках уровня E
    python tools/doctor.py --json     # то же в JSON (для CI и скриптов)
    python tools/doctor.py --deep     # дополнительно mypy

Строка с осознанным исключением помечается комментарием `# doctor: ok` (все правила)
или `# doctor: ok(имя-правила)`.

Правила (E — ошибка, W — предупреждение):
    E net-timeout        сетевой вызов без таймаута: при плохой сети висит вечно
    E proc-timeout       subprocess.run/check_output без таймаута
    E open-encoding      текстовый файл без encoding: на Windows это cp1252/cp1251
    E top-import-win     импорт winreg/msvcrt на верхнем уровне: модуль не импортируется на Linux
    E mutable-default    изменяемое значение аргумента по умолчанию
    E ps-interp          PowerShell-код собирается f-строкой: кавычки и `$` в данных ломают команду
    E skill-drift        команда из SKILL.md отсутствует в pc.py
    W wait-forever       .join()/.result()/.wait() без таймаута
    W queue-get          очередь .get() без таймаута
    W swallow            `except: pass` без записи в журнал
    W thread-daemon      поток без daemon=True: процесс не завершится при выходе
    W hot-loop           `while True` без ожидания, выхода и блокирующего вызова
    W deprecated-tool    wmic и другие утилиты, которых нет в новых Windows
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("jarvis", "tools", "packaging")
SKIP_PARTS = {"__pycache__", "build", "dist", ".git"}

SEVERITY = {
    "net-timeout": "E", "proc-timeout": "E", "open-encoding": "E", "top-import-win": "E",
    "mutable-default": "E", "ps-interp": "E", "skill-drift": "E",
    "wait-forever": "W", "queue-get": "W", "swallow": "W", "thread-daemon": "W",
    "hot-loop": "W", "deprecated-tool": "W",
}
HINTS = {
    "net-timeout": "добавьте timeout=… (10–15 с)",
    "proc-timeout": "добавьте timeout=… и обработайте subprocess.TimeoutExpired",
    "open-encoding": "укажите encoding='utf-8' (на Windows по умолчанию не UTF-8)",
    "top-import-win": "импортируйте внутри функции под проверкой IS_WIN",
    "mutable-default": "используйте None и создавайте значение внутри функции",
    "ps-interp": "передайте значение через переменную окружения и читайте $env:ИМЯ",
    "skill-drift": "добавьте команду в pc.py или уберите её из SKILL.md",
    "wait-forever": "задайте timeout и обработайте его истечение",
    "queue-get": "используйте get(timeout=…) и проверяйте флаг остановки",
    "swallow": "запишите ошибку в журнал (log.debug/log.warning), даже если она ожидаема",
    "thread-daemon": "создавайте поток с daemon=True или гарантируйте остановку",
    "hot-loop": "добавьте ожидание (sleep/wait/get с таймаутом) или условие выхода",
    "deprecated-tool": "замените на ctypes/PowerShell Get-CimInstance: утилиты может не быть",
}

WIN_ONLY_MODULES = {"winreg", "msvcrt", "winsound", "_winapi", "_winreg"}
PRAGMA = re.compile(r"#\s*doctor:\s*ok(?:\(([\w\-, ]+)\))?")


@dataclass
class Finding:
    rule: str
    path: str
    line: int
    message: str

    @property
    def severity(self) -> str:
        return SEVERITY.get(self.rule, "W")

    @property
    def hint(self) -> str:
        return HINTS.get(self.rule, "")


def _call_name(node: ast.Call) -> str:
    """Полное имя вызова вида 'subprocess.run' или 'x.join'."""
    f = node.func
    parts: List[str] = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    elif isinstance(f, ast.Call):
        parts.append("()")
    return ".".join(reversed(parts))


def _kw(node: ast.Call, name: str) -> Optional[ast.keyword]:
    for k in node.keywords:
        if k.arg == name:
            return k
    return None


def _has_kwargs_splat(node: ast.Call) -> bool:
    return any(k.arg is None for k in node.keywords)


def _const_str(node: ast.AST) -> Optional[str]:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _open_is_binary(node: ast.Call) -> bool:
    mode: Optional[ast.AST] = None
    if len(node.args) >= 2:
        mode = node.args[1]
    k = _kw(node, "mode")
    if k is not None:
        mode = k.value
    text = _const_str(mode) if mode is not None else None
    return bool(text and "b" in text)


def _body_logs(body: List[ast.stmt]) -> bool:
    for sub in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(sub, ast.Call):
            name = _call_name(sub)
            if name.startswith(("log.", "logging.", "logger.", "print", "traceback.")) or name.endswith(".exception"):
                return True
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.found: List[Finding] = []
        self._func_depth = 0

    def add(self, rule: str, node: ast.AST, message: str) -> None:
        self.found.append(Finding(rule, self.path, getattr(node, "lineno", 1), message))

    # --- импорты ---------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        if self._func_depth == 0:
            for a in node.names:
                if a.name.split(".")[0] in WIN_ONLY_MODULES:
                    self.add("top-import-win", node, f"import {a.name} на верхнем уровне")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._func_depth == 0 and node.module and node.module.split(".")[0] in WIN_ONLY_MODULES:
            self.add("top-import-win", node, f"from {node.module} import … на верхнем уровне")

    # --- функции ---------------------------------------------------------
    def _func(self, node) -> None:
        for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)) or (
                    isinstance(d, ast.Call) and _call_name(d) in ("list", "dict", "set")):
                self.add("mutable-default", node, f"{node.name}(): изменяемое значение по умолчанию")
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    # --- вызовы ----------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        last = name.rsplit(".", 1)[-1]
        splat = _has_kwargs_splat(node)

        if last == "urlopen" and not splat and _kw(node, "timeout") is None and len(node.args) < 3:
            self.add("net-timeout", node, f"{name}() без timeout")
        if last == "create_connection" and not splat and _kw(node, "timeout") is None and len(node.args) < 2:
            self.add("net-timeout", node, f"{name}() без timeout")

        if name in ("subprocess.run", "subprocess.check_output", "subprocess.check_call", "subprocess.call"):
            if not splat and _kw(node, "timeout") is None:
                self.add("proc-timeout", node, f"{name}() без timeout")
            text = _kw(node, "text") or _kw(node, "universal_newlines")
            if text is not None and isinstance(text.value, ast.Constant) and text.value.value and _kw(node, "encoding") is None:
                self.add("open-encoding", node, f"{name}(text=True) без encoding")

        if last == "open" and (name == "open" or name.endswith(".open")) and not splat:
            if name == "open" and node.args and not _open_is_binary(node) and _kw(node, "encoding") is None:
                self.add("open-encoding", node, "open() в текстовом режиме без encoding")
        if last in ("read_text", "write_text") and not splat and _kw(node, "encoding") is None:
            # read_text(encoding) может передаваться позиционно
            if len(node.args) < (1 if last == "read_text" else 2):
                self.add("open-encoding", node, f"{name}() без encoding")

        if last in ("join", "result", "wait") and not node.args and not node.keywords:
            recv = name.rsplit(".", 1)[0] if "." in name else ""
            if not recv.endswith(("path", "Path", "os.path", "str")) and recv not in ("", "()"):
                self.add("wait-forever", node, f"{name}() без timeout")
        if last == "get" and not node.args and not node.keywords:
            recv = name.rsplit(".", 1)[0].lower() if "." in name else ""
            if recv.endswith(("q", "queue", "_q")):
                self.add("queue-get", node, f"{name}() без timeout")

        if name in ("threading.Thread", "Thread") and _kw(node, "daemon") is None and not splat:
            self.add("thread-daemon", node, "Thread без daemon=True")

        self.generic_visit(node)

    # --- except ----------------------------------------------------------
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        only_pass = all(isinstance(s, ast.Pass) for s in node.body)
        broad = node.type is None or (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"))
        if only_pass and broad and not _body_logs(node.body):
            self.add("swallow", node, "except Exception: pass — любая ошибка теряется молча")
        self.generic_visit(node)

    # --- циклы -----------------------------------------------------------
    def visit_While(self, node: ast.While) -> None:
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            waits = ("sleep", "wait", "get", "join", "accept", "recv", "read", "readline", "poll", "select", "result")
            calm = False
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Break, ast.Return, ast.Raise)):
                    calm = True
                elif isinstance(sub, ast.Call) and _call_name(sub).rsplit(".", 1)[-1] in waits:
                    calm = True
            if not calm:
                self.add("hot-loop", node, "while True без выхода и ожидания")
        self.generic_visit(node)

    # --- строки ----------------------------------------------------------
    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        literal = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        has_value = any(isinstance(v, ast.FormattedValue) for v in node.values)
        if has_value and re.search(r"Add-Type|\[System\.|\[Microsoft\.|\$[A-Za-z_]+\s*=", literal):
            self.add("ps-interp", node, "PowerShell-код собирается f-строкой")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value.lower() == "wmic":  # doctor: ok(deprecated-tool)
            self.add("deprecated-tool", node, "wmic удалён из новых сборок Windows 11")


def analyze_source(source: str, path: str = "<memory>") -> List[Finding]:
    """Применить все правила к одному исходному тексту."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding("syntax", path, exc.lineno or 1, f"синтаксическая ошибка: {exc.msg}")]
    v = _Visitor(path)
    v.visit(tree)
    lines = source.splitlines()
    kept: List[Finding] = []
    for f in v.found:
        text = lines[f.line - 1] if 0 < f.line <= len(lines) else ""
        m = PRAGMA.search(text)
        if m and (m.group(1) is None or f.rule in [x.strip() for x in m.group(1).split(",")]):
            continue
        kept.append(f)
    return kept


def python_files(root: Path = ROOT) -> Iterable[Path]:
    for d in SCAN_DIRS:
        base = root / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if not SKIP_PARTS.intersection(p.parts):
                yield p


def check_skill_drift(root: Path = ROOT) -> List[Finding]:
    """Каждая команда из таблиц SKILL.md должна существовать в pc.py."""
    skill = root / "jarvis/resources/jarvis-pc/SKILL.md"
    pc = root / "jarvis/resources/jarvis-pc/scripts/pc.py"
    if not skill.exists() or not pc.exists():
        return []
    import importlib.util
    spec = importlib.util.spec_from_file_location("jarvis_pc_for_doctor", pc)
    if spec is None or spec.loader is None:
        return []
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    known: Set[str] = set()
    for action in parser._actions:  # noqa: SLF001 — у argparse нет публичного списка подкоманд
        if getattr(action, "choices", None):
            known.update(action.choices)
    found: List[Finding] = []
    for no, line in enumerate(skill.read_text(encoding="utf-8").splitlines(), 1):
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.match(r"`([a-z][a-z\-]*)(?: [^`]*)?`$", cells[-1]) if cells else None
        if m and m.group(1) not in known:
            found.append(Finding("skill-drift", str(skill.relative_to(root)), no,
                                 f"команда «{m.group(1)}» есть в SKILL.md, но нет в pc.py"))
    return found


def run_pyflakes(root: Path = ROOT) -> List[str]:
    out = subprocess.run([sys.executable, "-m", "pyflakes", "jarvis", "tools", "tests"], cwd=root,
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    return [ln for ln in (out.stdout + out.stderr).splitlines() if ln.strip()]


def run_mypy(root: Path = ROOT) -> List[str]:
    out = subprocess.run([sys.executable, "-m", "mypy", "jarvis", "tools", "--ignore-missing-imports",
                          "--check-untyped-defs", "--no-error-summary", "--python-version", "3.12"],
                         cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=300)
    noise = ("windll", "Module has no attribute", "does not return a value", "SignalInstance")
    return [ln for ln in out.stdout.splitlines() if ": error:" in ln and not any(n in ln for n in noise)]


def collect(root: Path = ROOT, deep: bool = False) -> dict:
    findings: List[Finding] = []
    for p in python_files(root):
        findings += analyze_source(p.read_text(encoding="utf-8"), str(p.relative_to(root)))
    findings += check_skill_drift(root)
    return {
        "findings": [dict(asdict(f), severity=f.severity, hint=f.hint) for f in findings],
        "pyflakes": run_pyflakes(root),
        "mypy": run_mypy(root) if deep else [],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Анализатор кода Джарвиса")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--deep", action="store_true", help="добавить mypy")
    ap.add_argument("--strict", action="store_true", help="считать предупреждения ошибками")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    report = collect(ROOT, args.deep)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        rows = sorted(report["findings"], key=lambda f: (f["severity"], f["path"], f["line"]))
        for f in rows:
            print(f"{f['severity']} {f['path']}:{f['line']}  [{f['rule']}] {f['message']}\n    → {f['hint']}")
        for ln in report["pyflakes"]:
            print(f"E {ln}  [pyflakes]")
        for ln in report["mypy"]:
            print(f"W {ln}  [mypy]")
        errs = sum(1 for f in rows if f["severity"] == "E") + len(report["pyflakes"])
        warns = sum(1 for f in rows if f["severity"] == "W") + len(report["mypy"])
        print(f"\nИтого: ошибок {errs}, предупреждений {warns}")
    errors = sum(1 for f in report["findings"] if f["severity"] == "E") + len(report["pyflakes"])
    warnings = sum(1 for f in report["findings"] if f["severity"] == "W") + len(report["mypy"])
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
