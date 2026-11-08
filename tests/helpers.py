"""Подставной Hermes для тестов: настоящий ему не нужен."""
import os
import stat
import tempfile
from pathlib import Path

STUB_DIR = Path(tempfile.mkdtemp(prefix="jarvis-stub-"))
STUB = STUB_DIR / "hermes"
LAST_PROMPT = STUB_DIR / "last_prompt.txt"
CALLS = STUB_DIR / "calls.log"

STUB.write_text(f"""#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in
    --query-file) QF="$2"; shift 2;;
    *) ARGS="$ARGS $1"; shift;;
  esac
done
echo "ARGS:$ARGS" >> "{CALLS}"
cp "$QF" "{LAST_PROMPT}"
LAST=$(tail -n 1 "$QF")
[ -n "$STUB_SLEEP" ] && sleep "$STUB_SLEEP"
if [ -n "$STUB_FAIL" ]; then echo "model not configured" >&2; exit 1; fi
echo "**Выполнено**, сэр: ${{LAST#*говорит: }} https://example.com"
""", encoding="utf-8")
STUB.chmod(STUB.stat().st_mode | stat.S_IEXEC)


def fake_exe(body: str) -> str:
    path = Path(tempfile.mkdtemp()) / "hermes"
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class env:
    """with env(STUB_FAIL="1"): ..."""
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
