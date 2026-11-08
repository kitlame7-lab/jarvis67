"""Маленькая консольная программа для Hermes: погода, курсы, окна, таймер выключения, игровой режим.

Собирается в jarvis-pc.exe отдельно от окна, чтобы запускаться за долю секунды и печатать результат в консоль.
Сам код команд лежит в jarvis/resources/jarvis-pc/scripts/pc.py и поставляется и как навык Hermes."""
import importlib.util
import sys
from pathlib import Path


def _script() -> Path:
    base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return base / "jarvis" / "resources" / "jarvis-pc" / "scripts" / "pc.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("jarvis_pc_tools", _script())
    module = importlib.util.module_from_spec(spec)
    sys.modules["jarvis_pc_tools"] = module
    spec.loader.exec_module(module)
    return module.main()


if __name__ == "__main__":
    sys.exit(main())
