"""Где лежат ресурсы приложения и данные пользователя."""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
IS_WIN = os.name == "nt"

# Ресурсы, поставляемые вместе с приложением (навык, значки)
RES_DIR = Path(__file__).resolve().parent / "resources"


def _data_dir() -> Path:
    env = os.environ.get("JARVIS_HOME")
    if env:
        return Path(env)
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Jarvis"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "jarvis"


# Настройки, журнал, кэш. Живут отдельно от программы, поэтому обновление их не трогает
DATA_DIR = _data_dir()
SETTINGS_PATH = Path(os.environ.get("JARVIS_SETTINGS") or DATA_DIR / "settings.json")
LOG_PATH = DATA_DIR / "jarvis.log"
CACHE_DIR = DATA_DIR / "cache"


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def exe_path() -> Path:
    """Путь к запущенной программе (Jarvis.exe или интерпретатор в режиме разработки)."""
    return Path(sys.executable)
