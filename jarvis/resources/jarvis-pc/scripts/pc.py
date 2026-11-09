#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Инструменты управления ПК для навыка jarvis-pc.

Каждая команда печатает ОДНУ короткую фразу по-русски, пригодную для
озвучки, и завершается кодом 0 при успехе или 1 при ошибке. Агент Hermes
вызывает их через терминал и пересказывает результат голосом.

Только стандартная библиотека. Переменная окружения JARVIS_DRYRUN=1 включает
сухой прогон: ничего не нажимается и не запускается, печатается только то,
что было бы сделано.

Примеры:
    python pc.py minimize-all
    python pc.py timer 30
    python pc.py timer-cancel
    python pc.py weather Москва
    python pc.py rate доллар
    python pc.py game-mode on
    python pc.py open блокнот
    python pc.py volume set 40
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import difflib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

IS_WIN = sys.platform == "win32"
DRY = os.environ.get("JARVIS_DRYRUN") == "1"
HERE = Path(__file__).resolve().parent
USER_AGENT = "Jarvis-Assistant/3.0"


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

DEFAULT_SETTINGS: Dict[str, Any] = {
    "city": "Москва",
    "game_mode": {"kill": []},
    "apps": {},
    "notes": {"dir": ""},
}


def settings_path() -> Optional[Path]:
    env = os.environ.get("JARVIS_SETTINGS")
    if env:
        return Path(env)
    pointer = HERE / "settings.path"
    if pointer.exists():
        text = pointer.read_text(encoding="utf-8-sig").strip()
        if text:
            return Path(text)
    # Запуск из jarvis-pc.exe: настройки лежат в папке данных приложения
    home = os.environ.get("JARVIS_HOME")
    if home:
        return Path(home) / "settings.json"
    base = os.environ.get("APPDATA") if IS_WIN else None
    if base:
        return Path(base) / "Jarvis" / "settings.json"
    return Path.home() / ".local" / "share" / "jarvis" / "settings.json"


def load_settings() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_SETTINGS))
    path = settings_path()
    if path and path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return cfg
        for key in ("city",):
            if user.get(key):
                cfg[key] = user[key]
        if isinstance(user.get("game_mode"), dict):
            cfg["game_mode"].update(user["game_mode"])
        if isinstance(user.get("apps"), dict):
            cfg["apps"].update(user["apps"])
        if isinstance(user.get("notes"), dict):
            cfg["notes"].update(user["notes"])
    return cfg


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------

class ToolError(Exception):
    """Ошибка, которую можно прочитать пользователю вслух."""


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def temp_words(value: float, after_from: bool = False) -> str:
    """Температура словами. after_from=True — для оборота «от ... до ...»."""
    r = int(round(value))
    unit = plural(r, "градус", "градуса", "градусов")
    if r > 0:
        return f"плюс {r} {unit}"
    if r < 0:
        return f"минус {abs(r)} {unit}"
    return "нуля градусов" if after_from else "ноль градусов"


def money(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def http_get(url: str, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        raise ToolError("Нет связи с сервисом. Проверьте интернет.") from exc
    except TimeoutError as exc:
        raise ToolError("Сервис не ответил вовремя.") from exc


def _oem_encoding() -> str:
    """Консольные утилиты Windows (tasklist, netsh, taskkill…) печатают в OEM-кодировке (cp866 на русской системе)."""
    if IS_WIN:
        try:
            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return "cp866"
    return "utf-8"


def run_cmd(argv: List[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Запускает программу без окна. Вывод декодируется в кодировке консоли, зависание обрывается по таймауту."""
    if DRY:
        print(f"[сухой прогон] {' '.join(argv)}", file=sys.stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")
    try:
        res = subprocess.run(argv, capture_output=True, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ToolError(f"Команда {argv[0]} не ответила за {int(timeout)} секунд.") from None
    except FileNotFoundError:
        raise ToolError(f"В системе нет программы {argv[0]}.") from None
    enc = _oem_encoding()
    return subprocess.CompletedProcess(argv, res.returncode, res.stdout.decode(enc, "replace"),
                                       res.stderr.decode(enc, "replace"))


PS_PREFIX = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ErrorActionPreference = 'Stop'; "


def run_ps(script: str, env: Optional[Dict[str, str]] = None, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Запускает PowerShell. Данные (пути, текст) передаются ТОЛЬКО через переменные окружения env и читаются как
    $env:ИМЯ: так кавычки, `$` и русские буквы в данных не ломают команду."""
    if DRY:
        print(f"[сухой прогон] powershell: {script[:80]}", file=sys.stderr)
        return subprocess.CompletedProcess([], 0, "", "")
    argv = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
            PS_PREFIX + script]
    try:
        res = subprocess.run(argv, capture_output=True, timeout=timeout, env=dict(os.environ, **(env or {})),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolError(f"PowerShell не ответил за {int(timeout)} секунд.") from None
    except FileNotFoundError:
        raise ToolError("В системе нет PowerShell.") from None
    return subprocess.CompletedProcess(argv, res.returncode, res.stdout.decode("utf-8", "replace"),
                                       res.stderr.decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# Клавиши
# --------------------------------------------------------------------------

VK = {
    "win": 0x5B, "shift": 0x10, "d": 0x44, "m": 0x4D,
    "up": 0x26, "down": 0x28,
    "vol_mute": 0xAD, "vol_down": 0xAE, "vol_up": 0xAF,
}


def press(*keys: str) -> None:
    """Нажимает сочетание клавиш и отпускает в обратном порядке."""
    if DRY or not IS_WIN:
        print(f"[сухой прогон] клавиши: {'+'.join(keys)}", file=sys.stderr)
        return
    user32 = ctypes.windll.user32
    for k in keys:
        user32.keybd_event(VK[k], 0, 0, 0)
    time.sleep(0.05)
    for k in reversed(keys):
        user32.keybd_event(VK[k], 0, 2, 0)


# --------------------------------------------------------------------------
# Окна
# --------------------------------------------------------------------------

def cmd_minimize_all(_a) -> str:
    press("win", "d")
    return "Все окна свёрнуты."


def cmd_restore_all(_a) -> str:
    press("win", "shift", "m")
    return "Окна возвращены."


def cmd_maximize(_a) -> str:
    press("win", "up")
    return "Окно развёрнуто."


def cmd_minimize(_a) -> str:
    press("win", "down")
    return "Окно свёрнуто."


# --------------------------------------------------------------------------
# Питание
# --------------------------------------------------------------------------

def cmd_timer(a) -> str:
    minutes = int(a.minutes)
    if not 1 <= minutes <= 1440:
        raise ToolError("Таймер можно поставить от одной минуты до суток.")
    res = run_cmd(["shutdown", "/s", "/t", str(minutes * 60)])
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise ToolError(f"Не удалось поставить таймер. {detail}".strip())
    unit = plural(minutes, "минуту", "минуты", "минут")
    return f"Компьютер выключится через {minutes} {unit}."


def cmd_timer_cancel(_a) -> str:
    res = run_cmd(["shutdown", "/a"])
    if res.returncode == 0:
        return "Выключение отменено."
    return "Выключение не было запланировано."


def cmd_lock(_a) -> str:
    run_cmd(["rundll32.exe", "user32.dll,LockWorkStation"])
    return "Компьютер заблокирован."


# --------------------------------------------------------------------------
# Громкость
# --------------------------------------------------------------------------

def cmd_volume(a) -> str:
    action = a.action
    if action == "mute":
        press("vol_mute")
        return "Звук включён или выключен."
    steps = max(1, min(25, int(a.value or 5) // 2)) if action in ("up", "down") else 0
    if action == "up":
        for _ in range(steps):
            press("vol_up")
        return "Громкость увеличена."
    if action == "down":
        for _ in range(steps):
            press("vol_down")
        return "Громкость уменьшена."
    if action == "set":
        if a.value is None or not 0 <= int(a.value) <= 100:
            raise ToolError("Громкость нужна числом от нуля до ста.")
        target = int(a.value)
        for _ in range(50):
            press("vol_down")
        for _ in range(target // 2):
            press("vol_up")
        return f"Громкость около {target} процентов."
    raise ToolError("Громкость: up, down, mute или set.")


# --------------------------------------------------------------------------
# Запуск программ
# --------------------------------------------------------------------------

BUILTIN_APPS = {
    "блокнот": "notepad.exe", "notepad": "notepad.exe",
    "калькулятор": "calc.exe", "calculator": "calc.exe",
    "проводник": "explorer.exe", "explorer": "explorer.exe",
    "диспетчер задач": "taskmgr.exe", "taskmgr": "taskmgr.exe",
    "paint": "mspaint.exe", "пейнт": "mspaint.exe",
    "командная строка": "cmd.exe", "терминал": "wt.exe",
    "настройки": "ms-settings:", "параметры": "ms-settings:",
    "панель управления": "control.exe",
    "браузер": "https://www.google.com",
    "ютуб": "https://www.youtube.com", "youtube": "https://www.youtube.com",
    "хром": "chrome", "chrome": "chrome",
    "эдж": "msedge", "edge": "msedge",
    "фаерфокс": "firefox", "firefox": "firefox",
    "телеграм": "telegram", "telegram": "telegram",
    "стим": "steam", "steam": "steam",
    "дискорд": "discord", "discord": "discord",
}


def cmd_open(a) -> str:
    raw = " ".join(a.target).strip()
    if not raw:
        raise ToolError("Не сказано, что открыть.")
    cfg = load_settings()
    table = dict(BUILTIN_APPS)
    table.update({k.lower(): v for k, v in cfg["apps"].items()})
    target = table.get(raw.lower(), raw)
    if target == raw and not raw.startswith(("http://", "https://", "ms-settings:", "mailto:")) \
            and not os.path.exists(raw):
        shortcut = find_app(raw)                     # программа из меню «Пуск»: «открой телеграм», «открой фотошоп»
        if shortcut is not None:
            target = str(shortcut)

    if DRY or not IS_WIN:
        print(f"[сухой прогон] открыть: {target}", file=sys.stderr)
        return f"Открываю {raw}."
    try:
        if target.startswith(("http://", "https://", "ms-settings:", "mailto:")) or os.path.exists(target):
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["cmd", "/c", "start", "", target],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise ToolError(f"Не удалось открыть {raw}.") from exc
    return f"Открываю {raw}."


# --------------------------------------------------------------------------
# Игровой режим
# --------------------------------------------------------------------------

def cmd_game_mode(a) -> str:
    cfg = load_settings()
    if a.state == "on":
        run_cmd(["powercfg", "/setactive", "SCHEME_MIN"])
        killed: List[str] = []
        for name in cfg["game_mode"].get("kill", []):
            exe = name if name.lower().endswith(".exe") else name + ".exe"
            res = run_cmd(["taskkill", "/F", "/IM", exe])
            if res.returncode == 0:
                killed.append(exe)
        extra = f" Закрыто программ: {len(killed)}." if killed else ""
        return "Игровой режим активирован." + extra
    run_cmd(["powercfg", "/setactive", "SCHEME_BALANCED"])
    return "Игровой режим выключен, схема питания обычная."


# --------------------------------------------------------------------------
# Время
# --------------------------------------------------------------------------

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def cmd_now(_a) -> str:
    n = dt.datetime.now()
    return (f"Сейчас {n:%H:%M}, {WEEKDAYS[n.weekday()]}, "
            f"{n.day} {MONTHS[n.month - 1]} {n.year} года.")


# --------------------------------------------------------------------------
# Погода (Open-Meteo, ключ не нужен)
# --------------------------------------------------------------------------

WMO = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "лёгкая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "небольшой ливень", 81: "ливень", 82: "сильный ливень",
    85: "снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}


def format_weather(city: str, data: Dict[str, Any]) -> str:
    cur = data["current"]
    day = data["daily"]
    desc = WMO.get(int(cur.get("weather_code", -1)), "без уточнений")
    wind = int(round(cur.get("wind_speed_10m", 0)))
    parts = [
        f"{city}: сейчас {temp_words(cur['temperature_2m'])}, {desc}",
        f"ощущается как {temp_words(cur['apparent_temperature'])}",
        f"ветер {wind} {plural(wind, 'метр', 'метра', 'метров')} в секунду",
        f"сегодня от {temp_words(day['temperature_2m_min'][0], after_from=True)} "
        f"до {temp_words(day['temperature_2m_max'][0])}",
    ]
    prob = day.get("precipitation_probability_max", [None])[0]
    if prob is not None:
        parts.append(f"вероятность осадков {int(prob)} {plural(int(prob), 'процент', 'процента', 'процентов')}")
    return ", ".join(parts) + "."


def cmd_weather(a) -> str:
    city = " ".join(a.city).strip() or load_settings()["city"]
    geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
               + urllib.parse.urlencode({"name": city, "count": 1, "language": "ru", "format": "json"}))
    geo = json.loads(http_get(geo_url))
    if not geo.get("results"):
        raise ToolError(f"Не нашёл город {city}.")
    place = geo["results"][0]
    wx_url = ("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": place["latitude"], "longitude": place["longitude"],
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "wind_speed_unit": "ms", "timezone": "auto", "forecast_days": 1,
    }))
    data = json.loads(http_get(wx_url))
    return format_weather(place.get("name", city), data)


# --------------------------------------------------------------------------
# Курсы валют (ЦБ РФ)
# --------------------------------------------------------------------------

CURRENCY_ALIASES = {
    "доллар": "USD", "доллара": "USD", "usd": "USD", "долларов": "USD",
    "евро": "EUR", "eur": "EUR",
    "юань": "CNY", "юаня": "CNY", "cny": "CNY",
    "фунт": "GBP", "gbp": "GBP",
    "иена": "JPY", "иен": "JPY", "jpy": "JPY",
    "тенге": "KZT", "kzt": "KZT",
    "гривна": "UAH", "uah": "UAH",
    "лира": "TRY", "try": "TRY",
}
CURRENCY_NAMES = {"USD": "доллара", "EUR": "евро", "CNY": "юаня", "GBP": "фунта",
                  "JPY": "иены", "KZT": "тенге", "UAH": "гривны", "TRY": "лиры"}


def parse_cbr_json(payload: Dict[str, Any], code: str) -> Dict[str, float]:
    item = payload["Valute"][code]
    nominal = float(item.get("Nominal", 1)) or 1.0
    return {"value": float(item["Value"]) / nominal,
            "previous": float(item["Previous"]) / nominal}


def parse_cbr_xml(raw: bytes, code: str) -> Dict[str, float]:
    root = ET.fromstring(raw)
    for node in root.findall("Valute"):
        if node.findtext("CharCode") == code:
            nominal = float(node.findtext("Nominal", "1").replace(",", ".")) or 1.0
            value = float(node.findtext("Value", "0").replace(",", ".")) / nominal
            return {"value": value, "previous": value}
    raise ToolError(f"Нет такой валюты: {code}.")


def cmd_rate(a) -> str:
    word = " ".join(a.currency).strip().lower() or "доллар"
    code = CURRENCY_ALIASES.get(word, word.upper())
    info: Optional[Dict[str, float]] = None
    last_error: Optional[ToolError] = None
    for url, parser in (
        ("https://www.cbr-xml-daily.ru/daily_json.js", lambda b: parse_cbr_json(json.loads(b), code)),
        ("https://www.cbr.ru/scripts/XML_daily.asp", lambda b: parse_cbr_xml(b, code)),
    ):
        try:
            info = parser(http_get(url))
            break
        except ToolError as exc:
            last_error = exc
        except (KeyError, ValueError, ET.ParseError):
            last_error = ToolError(f"Нет данных по валюте {code}.")
    if info is None:
        raise last_error or ToolError("Не удалось получить курс.")

    name = CURRENCY_NAMES.get(code, code)
    text = f"Курс {name} по данным Центробанка: {money(info['value'])} рубля"
    diff = info["value"] - info["previous"]
    if abs(diff) >= 0.005:
        text += f", {'вырос' if diff > 0 else 'упал'} на {money(abs(diff))} с прошлого дня"
    return text + "."


# --------------------------------------------------------------------------
# Файловая система
# --------------------------------------------------------------------------

def _human_size(size: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "Б" else f"{int(size)} Б"
        size /= 1024
    return f"{size:.1f} ПБ"


SEARCH_BUDGET_S = 20.0
SKIP_DIRS = {"$recycle.bin", "system volume information", "windows", "appdata", "node_modules", ".git",
             "program files", "program files (x86)", "programdata"}


def _walk(start: Path, budget: float = SEARCH_BUDGET_S):
    """Обход папки с ограничением по времени: в домашней папке могут быть сотни тысяч файлов (OneDrive)."""
    deadline = time.monotonic() + budget
    for root, dirs, files in os.walk(start):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        yield root, dirs, files, time.monotonic() > deadline
        if time.monotonic() > deadline:
            return


def cmd_files(a) -> str:
    action = a.action
    target = " ".join(a.path).strip() if a.path else ""

    if action == "list":
        p = Path(target) if target else Path.home() / "Desktop"
        if not p.exists():
            raise ToolError(f"Папка не найдена: {p}")
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        if not entries:
            return f"Папка {p.name} пуста."
        lines = []
        for e in entries[:30]:
            if e.is_dir():
                lines.append(f"  папка {e.name}")
            else:
                try:
                    lines.append(f"  файл {e.name} ({_human_size(e.stat().st_size)})")
                except OSError:
                    lines.append(f"  файл {e.name}")
        more = f"\n  … и ещё {len(entries) - 30}" if len(entries) > 30 else ""
        return f"Содержимое {p} (всего {len(entries)}):\n" + "\n".join(lines) + more

    if action == "search":
        if not target:
            raise ToolError("Укажите что искать.")
        start = Path(a.extra[0]) if a.extra else Path.home()
        needle = target.lower()
        found: List[str] = []
        timed_out = False
        for root, dirs, files, out_of_time in _walk(start):
            for name in dirs + files:
                if needle in name.lower():
                    found.append(os.path.join(root, name))
            if len(found) >= 15:
                break
            timed_out = out_of_time
        if not found:
            tail = " Поиск остановлен по времени, уточните папку." if timed_out else ""
            return f"Ничего не найдено по запросу «{target}».{tail}"
        return f"Найдено ({len(found)}):\n" + "\n".join(f"  {f}" for f in found[:15])

    if action == "size":
        p = Path(target) if target else Path.home() / "Desktop"
        if not p.exists():
            raise ToolError(f"Не найдено: {p}")
        if p.is_file():
            return f"{p.name}: {_human_size(p.stat().st_size)}"
        total = count = 0
        partial = False
        for root, _dirs, files, out_of_time in _walk(p):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                    count += 1
                except OSError:
                    continue
            partial = out_of_time
        note = " (посчитано не всё, папка очень большая)" if partial else ""
        return f"{p.name}: {_human_size(total)}, файлов: {count}{note}"

    if action in ("move", "copy"):
        if not target or not a.extra:
            raise ToolError("Нужны два пути: откуда и куда.")
        src, dst = Path(target), Path(" ".join(a.extra))
        if not src.exists():
            raise ToolError(f"Не найдено: {src}")
        verb = "Переместил" if action == "move" else "Скопировал"
        if DRY:
            return f"[сухой прогон] {verb} бы {src} → {dst}"
        import shutil
        if action == "move":
            shutil.move(str(src), str(dst))
        elif src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        return f"{'Перемещено' if action == 'move' else 'Скопировано'}: {src.name} → {dst}"

    if action == "delete":
        if not target:
            raise ToolError("Укажите что удалить.")
        p = Path(target)
        if not p.exists():
            raise ToolError(f"Не найдено: {p}")
        if DRY:
            return f"[сухой прогон] Удалил бы {p}"
        if not IS_WIN:
            raise ToolError("Удаление в корзину доступно только на Windows.")
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic;"
            "$p = $env:JARVIS_ARG;"
            "if (Test-Path -LiteralPath $p -PathType Container) {"
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p,'OnlyErrorDialogs','SendToRecycleBin')"
            "} else {"
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p,'OnlyErrorDialogs','SendToRecycleBin')}")
        res = run_ps(script, {"JARVIS_ARG": str(p)})
        if res.returncode != 0:
            raise ToolError(f"Не удалось удалить: {(res.stderr or res.stdout).strip()[:200]}")
        return f"Удалено в корзину: {p.name}"

    if action == "create-folder":
        if not target:
            raise ToolError("Укажите путь для новой папки.")
        p = Path(target)
        if DRY:
            return f"[сухой прогон] Создал бы папку {p}"
        p.mkdir(parents=True, exist_ok=True)
        return f"Папка создана: {p}"

    if action == "read":
        if not target:
            raise ToolError("Укажите файл для чтения.")
        p = Path(target)
        if not p.is_file():
            raise ToolError(f"Файл не найден: {p}")
        size = p.stat().st_size
        try:
            with open(p, "rb") as fh:
                raw = fh.read(4000)
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать: {exc}") from exc
        text = _decode_guess(raw)
        more = f"\n[показано начало, всего {_human_size(size)}]" if size > len(raw) else ""
        return f"Содержимое {p.name}:\n{text}{more}"

    raise ToolError(f"Неизвестное действие с файлами: {action}")


def _decode_guess(raw: bytes) -> str:
    """Текстовые файлы на русской Windows бывают в UTF-8 и в cp1251."""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Системная информация (ctypes: утилиты вроде wmic есть не во всех Windows)
# --------------------------------------------------------------------------

def _memory_status() -> Optional[Dict[str, int]]:
    if not IS_WIN:
        return None

    class MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MemStatus()
    st.dwLength = ctypes.sizeof(MemStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
        return None
    return {"total": st.ullTotalPhys, "free": st.ullAvailPhys, "load": st.dwMemoryLoad}


def _battery_status() -> Optional[str]:
    if not IS_WIN:
        return None

    class Power(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    st = Power()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):  # type: ignore[attr-defined]
        return None
    if st.BatteryFlag == 128 or st.BatteryLifePercent > 100:   # батареи нет
        return None
    plug = ", подключено к питанию" if st.ACLineStatus == 1 else ""
    return f"Батарея: {st.BatteryLifePercent}%{plug}"


def _cpu_load(interval: float = 0.4) -> Optional[int]:
    if not IS_WIN:
        return None

    class FileTime(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

    def sample():
        idle, kern, user = FileTime(), FileTime(), FileTime()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))  # type: ignore[attr-defined]
        return [(t.hi << 32) | t.lo for t in (idle, kern, user)]

    a = sample()
    time.sleep(interval)
    b = sample()
    idle, kern, user = (b[i] - a[i] for i in range(3))
    total = kern + user          # время ядра уже включает простой
    return max(0, min(100, int(round(100 * (total - idle) / total)))) if total else None


def _cpu_name() -> str:
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return f"{os.cpu_count() or '?'} ядер"


def _drives() -> List[str]:
    if IS_WIN:
        mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        letters = [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]
        return [d for d in letters if ctypes.windll.kernel32.GetDriveTypeW(d) == 3]  # type: ignore[attr-defined]
    return ["/"]


def cmd_sysinfo(_a) -> str:
    import shutil
    parts = [f"Процессор: {_cpu_name()}"]
    load = _cpu_load()
    if load is not None:
        parts.append(f"загрузка {load}%")
    mem = _memory_status()
    if mem:
        used = mem["total"] - mem["free"]
        parts.append(f"память: занято {_human_size(used)} из {_human_size(mem['total'])} ({mem['load']}%)")
    for drive in _drives():
        try:
            du = shutil.disk_usage(drive)
            parts.append(f"диск {drive.rstrip(chr(92))} свободно {_human_size(du.free)} из {_human_size(du.total)}")
        except OSError:
            continue
    battery = _battery_status()
    if battery:
        parts.append(battery)
    return "; ".join(parts) + "."


# --------------------------------------------------------------------------
# Процессы
# --------------------------------------------------------------------------

def cmd_processes(a) -> str:
    if not IS_WIN and not DRY:
        raise ToolError("Список процессов доступен только на Windows.")
    import csv
    import io
    import re
    query = " ".join(a.query).strip().lower() if a.query else ""
    res = run_cmd(["tasklist", "/FO", "CSV", "/NH"])
    procs = []
    for row in csv.reader(io.StringIO(res.stdout or "")):
        if len(row) < 5:
            continue
        name, pid = row[0], row[1]
        digits = re.sub(r"\D", "", row[4])          # «12 345 КБ» с неразрывным пробелом
        mem_kb = int(digits) if digits else 0
        if query and query not in name.lower():
            continue
        procs.append((name, pid, mem_kb))
    if query:
        if not procs:
            return f"Процесс «{query}» не найден."
        lines = [f"  {n} (PID {p}, {_human_size(m * 1024)})" for n, p, m in procs[:15]]
        return f"Найдено процессов «{query}»: {len(procs)}\n" + "\n".join(lines)
    procs.sort(key=lambda x: x[2], reverse=True)
    lines = [f"  {n} (PID {p}, {_human_size(m * 1024)})" for n, p, m in procs[:15]]
    return f"Всего процессов: {len(procs)}. Больше всего памяти занимают:\n" + "\n".join(lines)


def cmd_process_kill(a) -> str:
    target = " ".join(a.target).strip()
    if not target:
        raise ToolError("Укажите имя или PID процесса.")
    if DRY:
        return f"[сухой прогон] Завершил бы процесс {target}"
    if target.isdigit():
        res = run_cmd(["taskkill", "/F", "/PID", target])
    else:
        exe = target if target.lower().endswith(".exe") else target + ".exe"
        res = run_cmd(["taskkill", "/F", "/IM", exe])
    if res.returncode == 0:
        return f"Процесс {target} завершён."
    raise ToolError(f"Не удалось завершить процесс: {(res.stderr or res.stdout).strip()[:200]}")


# --------------------------------------------------------------------------
# Буфер обмена
# --------------------------------------------------------------------------

def cmd_clipboard(a) -> str:
    if a.action == "get":
        if not IS_WIN and not DRY:
            raise ToolError("Буфер обмена доступен только на Windows.")
        res = run_ps("Get-Clipboard -Raw")
        text = (res.stdout or "").strip()
        if not text:
            return "Буфер обмена пуст."
        return "В буфере обмена: " + (text[:500] + "…" if len(text) > 500 else text)
    content = " ".join(a.text) if a.text else ""
    if not content:
        raise ToolError("Укажите текст для буфера обмена.")
    if DRY:
        return f"[сухой прогон] Скопировал бы в буфер: {content[:100]}"
    res = run_ps("Set-Clipboard -Value $env:JARVIS_ARG", {"JARVIS_ARG": content[:30000]})
    if res.returncode != 0:
        raise ToolError("Не удалось записать в буфер обмена.")
    return "Текст скопирован в буфер обмена."


# --------------------------------------------------------------------------
# Скриншот
# --------------------------------------------------------------------------

SCREENSHOT_PS = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
    "Add-Type -Namespace J -Name D -MemberDefinition "
    "'[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();';"
    "[J.D]::SetProcessDPIAware() | Out-Null;"            # без этого при масштабе 125% снимается часть экрана
    "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen;"
    "$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height);"
    "$g = [System.Drawing.Graphics]::FromImage($bmp);"
    "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size);"
    "$bmp.Save($env:JARVIS_ARG, [System.Drawing.Imaging.ImageFormat]::Png);"
    "$g.Dispose(); $bmp.Dispose()")


def cmd_screenshot(a) -> str:
    if not IS_WIN and not DRY:
        raise ToolError("Скриншот доступен только на Windows.")
    dest = " ".join(a.path).strip() if a.path else ""
    if not dest:
        dest = str(Path.home() / "Desktop" / f"screenshot_{dt.datetime.now():%Y%m%d_%H%M%S}.png")
    if DRY:
        return f"[сухой прогон] Скриншот → {dest}"
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    res = run_ps(SCREENSHOT_PS, {"JARVIS_ARG": dest})
    if res.returncode != 0 or not Path(dest).exists():
        raise ToolError(f"Не удалось сделать скриншот: {(res.stderr or res.stdout).strip()[:200]}")
    return f"Скриншот сохранён: {dest}"


# --------------------------------------------------------------------------
# Выполнение команд
# --------------------------------------------------------------------------

def cmd_run(a) -> str:
    command = " ".join(a.command).strip()
    if not command:
        raise ToolError("Укажите команду для выполнения.")
    if DRY:
        return f"[сухой прогон] Выполнил бы: {command}"
    try:
        res = subprocess.run(command, shell=True, capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ToolError("Команда выполнялась слишком долго (более 30 секунд).") from None
    enc = _oem_encoding()
    out = res.stdout.decode(enc, "replace").strip()
    err = res.stderr.decode(enc, "replace").strip()
    if res.returncode != 0:
        return f"Команда завершилась с кодом {res.returncode}: {(err or out)[:300]}"
    return f"Результат: {out[:800]}" if out else "Команда выполнена."


# --------------------------------------------------------------------------
# Сеть
# --------------------------------------------------------------------------

def _local_ip() -> Optional[str]:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))            # UDP: пакетов не отправляется, лишь выбирается интерфейс
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def cmd_network(_a) -> str:
    import re
    parts = []
    ip = _local_ip()
    if ip:
        parts.append(f"IP: {ip}")
    if IS_WIN:
        res = run_cmd(["netsh", "wlan", "show", "interfaces"], timeout=10)
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", res.stdout or "", re.M)
        if m and m.group(1).strip():
            parts.append(f"Wi-Fi: {m.group(1).strip()}")
    try:
        http_get("https://www.google.com/generate_204", timeout=5)
        parts.append("интернет есть")
    except ToolError:
        parts.append("интернета нет")
    return "; ".join(parts) + "."


# --------------------------------------------------------------------------
# Уведомления
# --------------------------------------------------------------------------

NOTIFY_PS = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
    "$n = New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon = [System.Drawing.SystemIcons]::Information;"
    "$n.Visible = $true;"
    "$n.ShowBalloonTip(5000, 'Джарвис', $env:JARVIS_ARG, 'Info');"
    "Start-Sleep 6; $n.Dispose()")


def cmd_notify(a) -> str:
    text = " ".join(a.text).strip()
    if not text:
        raise ToolError("Укажите текст уведомления.")
    if DRY or not IS_WIN:
        return f"[сухой прогон] Уведомление: {text}"
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", NOTIFY_PS],
                     env=dict(os.environ, JARVIS_ARG=text[:500]),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Уведомление показано: {text[:100]}"


# --------------------------------------------------------------------------
# Задачи, записанные по действиям пользователя
# --------------------------------------------------------------------------
# Формат и поиск совпадают с jarvis/tasks.py (это проверяет тест). Записывает задачи приложение Джарвис,
# здесь они только читаются и воспроизводятся.

KEY_VK: Dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D,
    "delete": 0x2E, "win": 0x5B, "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "capslock": 0x14,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC,
    "]": 0xDD, "'": 0xDE,
}
for _i in range(10):
    KEY_VK[str(_i)] = 0x30 + _i
for _i in range(26):
    KEY_VK[chr(ord("a") + _i)] = 0x41 + _i
for _i in range(1, 13):
    KEY_VK[f"f{_i}"] = 0x6F + _i
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}
SIZE_TOLERANCE = 0.25            # окно изменилось по размеру сильнее — координаты берутся от экрана
ABORT_VK = 0x1B                  # Esc прерывает выполнение задачи


def tasks_dir() -> Path:
    env = os.environ.get("JARVIS_TASKS")
    if env:
        return Path(env)
    path = settings_path()
    return (path.parent if path else Path.home()) / "tasks"


def load_tasks(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    root = root or tasks_dir()
    out: List[Dict[str, Any]] = []
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.name.startswith("_"):
                continue
            try:
                data = json.loads((folder / "task.json").read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("steps"), list):
                data["slug"] = str(data.get("slug") or folder.name)
                data["_dir"] = str(folder)
                out.append(data)
    return sorted(out, key=lambda t: str(t.get("created", "")), reverse=True)


def _tnorm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е"))).strip()


def find_task(query: str, items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Название или фраза вызова: точно, по вхождению, затем нечётко (как jarvis.tasks.find_task)."""
    q = _tnorm(query)
    if not q:
        return None
    labels = [(t, [_tnorm(t.get("name", "")), _tnorm(t.get("slug", ""))] + [_tnorm(p) for p in t.get("phrases", [])])
              for t in items]
    for t, names in labels:
        if q in names:
            return t
    for t, names in labels:
        if any(q in n or n in q for n in names if n):
            return t
    best, score = None, 0.0
    for t, names in labels:
        for n in names:
            r = difflib.SequenceMatcher(None, q, n).ratio() if n else 0.0
            if r > score:
                best, score = t, r
    return best if score >= 0.62 else None


def describe_task_step(step: Dict[str, Any]) -> str:
    k = step.get("kind")
    if k == "window":
        return f"программа {step.get('process') or '?'}"
    if k == "click":
        return ("двойной щелчок" if step.get("double") else "щелчок") + f" ({step.get('button', 'left')})"
    if k == "drag":
        return "перетаскивание"
    if k == "type":
        return "секретный ввод" if step.get("secret") else f"ввод текста, {step.get('length', 0)} симв."
    if k == "key":
        return f"клавиша {step.get('key')}" + (f" ×{step['count']}" if step.get("count", 1) > 1 else "")
    if k == "hotkey":
        return "сочетание " + "+".join(step.get("keys", []))
    return str(k)


class DryInput:
    """Ничего не делает, а только записывает действия: для проверки и для не-Windows."""

    def __init__(self) -> None:
        self.log: List[Any] = []
        self.wins: List[Dict[str, Any]] = []

    def _note(self, *what: Any) -> None:
        self.log.append(what)
        print("[сухой прогон]", *what, file=sys.stderr)

    def screen_size(self): return (1920, 1080)
    def windows(self): return list(self.wins)
    def foreground(self): return None
    def focus(self, win): self._note("окно", win.get("title"))
    def launch(self, process): self._note("запуск", process); return False
    def click(self, x, y, button="left", double=False): self._note("щелчок", x, y, button, double)
    def drag(self, x1, y1, x2, y2, button="left"): self._note("перетаскивание", x1, y1, x2, y2)
    def type_text(self, text): self._note("ввод", len(text))
    def key(self, name, count=1): self._note("клавиша", name, count)
    def hotkey(self, keys): self._note("сочетание", "+".join(keys))
    def abort_requested(self): return False


class Win32Input:
    """Настоящий ввод через SendInput. Процесс должен быть DPI-aware, иначе координаты съедут на масштабируемых экранах."""

    def __init__(self) -> None:
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.u32.SetProcessDPIAware()
        except OSError:
            pass
        ulong_ptr = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", UNION)]
        self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT = MOUSEINPUT, KEYBDINPUT, INPUT
        self.u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.u32.IsIconic.argtypes = [wintypes.HWND]
        self.u32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.u32.GetForegroundWindow.restype = wintypes.HWND
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.u32.GetAsyncKeyState.restype = ctypes.c_short

    # --- отправка ввода -------------------------------------------------------
    def _send(self, *items) -> None:
        arr = (self.INPUT * len(items))(*items)
        sent = self.u32.SendInput(len(items), arr, ctypes.sizeof(self.INPUT))
        if sent != len(items):
            raise ToolError("Windows не принял ввод (возможно, окно запущено от администратора).")

    def _mouse(self, flags: int, data: int = 0):
        i = self.INPUT(0)
        i.u.mi = self.MOUSEINPUT(0, 0, data, flags, 0, 0)
        return i

    def _key(self, vk: int = 0, scan: int = 0, flags: int = 0):
        i = self.INPUT(1)
        i.u.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def _nudge(self) -> None:
        """Любой ввод от процесса даёт ему право выводить окна на передний план."""
        self._send(self._mouse(0x0001))                 # MOUSEEVENTF_MOVE на 0 точек

    # --- экран и окна -----------------------------------------------------------
    def screen_size(self):
        return self.u32.GetSystemMetrics(0), self.u32.GetSystemMetrics(1)

    def _proc_name(self, pid: int) -> str:
        handle = self.k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = self.wt.DWORD(520)
            if self.k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            self.k32.CloseHandle(handle)
        return ""

    def _describe(self, hwnd) -> Optional[Dict[str, Any]]:
        if not hwnd:
            return None
        title = ctypes.create_unicode_buffer(256)
        self.u32.GetWindowTextW(hwnd, title, 256)
        rect = self.wt.RECT()
        self.u32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = self.wt.DWORD()
        self.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {"hwnd": hwnd, "title": title.value, "process": self._proc_name(pid.value),
                "rect": [rect.left, rect.top, rect.right, rect.bottom]}

    def windows(self) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        callback_t = ctypes.WINFUNCTYPE(ctypes.c_bool, self.wt.HWND, self.wt.LPARAM)

        def visit(hwnd, _lparam):
            if self.u32.IsWindowVisible(hwnd):
                info = self._describe(hwnd)
                if info and info["title"]:
                    found.append(info)
            return True
        self.u32.EnumWindows(callback_t(visit), 0)
        return found

    def foreground(self) -> Optional[Dict[str, Any]]:
        return self._describe(self.u32.GetForegroundWindow())

    def focus(self, win: Dict[str, Any]) -> None:
        hwnd = win["hwnd"]
        if self.u32.IsIconic(hwnd):
            self.u32.ShowWindow(hwnd, 9)                # SW_RESTORE
        self._nudge()
        self.u32.SetForegroundWindow(hwnd)

    def launch(self, process: str) -> bool:
        stem = os.path.splitext(process)[0]
        try:
            os.startfile(stem)                          # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    # --- действия -----------------------------------------------------------------
    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        down, up = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}[button]
        self.u32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        for n in range(2 if double else 1):
            self._send(self._mouse(down))
            time.sleep(0.03)
            self._send(self._mouse(up))
            if n == 0 and double:
                time.sleep(0.06)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        down, up = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}[button]
        self.u32.SetCursorPos(int(x1), int(y1))
        time.sleep(0.08)
        self._send(self._mouse(down))
        for i in range(1, 13):
            time.sleep(0.02)
            self.u32.SetCursorPos(int(x1 + (x2 - x1) * i / 12), int(y1 + (y2 - y1) * i / 12))
        time.sleep(0.05)
        self._send(self._mouse(up))

    def type_text(self, text: str) -> None:
        units = text.encode("utf-16-le")
        for n in range(0, len(units), 2):
            code = units[n] | (units[n + 1] << 8)
            self._send(self._key(0, code, 0x0004), self._key(0, code, 0x0004 | 0x0002))     # UNICODE, затем отпускание
            time.sleep(0.01)

    def _vk_events(self, vk: int, up: bool):
        flags = (0x0001 if vk in EXTENDED_VKS else 0) | (0x0002 if up else 0)
        return self._key(vk, self.u32.MapVirtualKeyW(vk, 0), flags)

    def key(self, name: str, count: int = 1) -> None:
        vk = KEY_VK.get(name)
        if vk is None:
            raise ToolError(f"Неизвестная клавиша «{name}».")
        for _ in range(max(1, count)):
            self._send(self._vk_events(vk, False), self._vk_events(vk, True))
            time.sleep(0.04)

    def hotkey(self, keys: List[str]) -> None:
        vks = [KEY_VK.get(k) for k in keys]
        if None in vks:
            raise ToolError("Неизвестная клавиша в сочетании: " + "+".join(keys))
        vk_list = [v for v in vks if v is not None]
        self._send(*[self._vk_events(v, False) for v in vk_list])
        time.sleep(0.05)
        self._send(*[self._vk_events(v, True) for v in reversed(vk_list)])

    def abort_requested(self) -> bool:
        return bool(self.u32.GetAsyncKeyState(ABORT_VK) & 0x8000)


class Player:
    """Воспроизводит шаги задачи. Любая неясность — остановка с понятной причиной, а не угадывание."""

    def __init__(self, io: Any, task: Dict[str, Any], speed: float = 1.0, sleep=time.sleep, clock=time.monotonic):
        self.io, self.task, self.speed = io, task, max(0.2, min(5.0, speed))
        self.sleep, self.clock = sleep, clock
        self._esc_guard = 0.0
        self.notes: List[str] = []

    # --- ожидание с проверкой прерывания -----------------------------------------
    def _aborted(self) -> bool:
        return self.clock() > self._esc_guard and bool(self.io.abort_requested())

    def _wait(self, seconds: float) -> bool:
        end = self.clock() + seconds
        while True:
            if self._aborted():
                return False
            left = end - self.clock()
            if left <= 0:
                return True
            self.sleep(min(0.05, left))

    # --- окна ------------------------------------------------------------------
    def _find_window(self, ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        proc = str(ref.get("process", "")).lower()
        if not proc:
            return None
        candidates = [w for w in self.io.windows() if str(w.get("process", "")).lower() == proc]
        if not candidates:
            return None
        title = str(ref.get("title", "")).lower()
        fg = self.io.foreground()

        def rank(w: Dict[str, Any]):
            t = str(w.get("title", "")).lower()
            r = w["rect"]
            area = max(0, r[2] - r[0]) * max(0, r[3] - r[1])
            return (t == title and bool(title), bool(title) and (title in t or t in title),
                    bool(fg) and fg.get("hwnd") == w.get("hwnd"), area)
        return max(candidates, key=rank)

    def _ensure_window(self, ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        win = self._find_window(ref)
        if win is None:
            return None
        fg = self.io.foreground()
        if not fg or fg.get("hwnd") != win.get("hwnd"):
            self.io.focus(win)
            self._wait(0.4)
            win = self._find_window(ref) or win
        return win

    def _point(self, step: Dict[str, Any], suffix: str = "") -> Tuple[int, int]:
        """Экранная точка шага: от окна, если оно такое же по размеру, иначе от экрана."""
        ref = step.get("window") or {}
        fx, fy = step.get("x" + suffix), step.get("y" + suffix)
        win = self._find_window(ref) if fx is not None else None
        size = step.get("size")
        if win and fx is not None and fy is not None and size:
            left, top, right, bottom = win["rect"]
            w, h = right - left, bottom - top
            if (w > 0 and h > 0 and abs(w / size[0] - 1) <= SIZE_TOLERANCE and abs(h / size[1] - 1) <= SIZE_TOLERANCE):
                if suffix == "":
                    self._ensure_window(ref)
                    win = self._find_window(ref) or win
                    left, top, right, bottom = win["rect"]
                    w, h = right - left, bottom - top
                return int(left + fx * w), int(top + fy * h)
        sx, sy = step.get("sx" + suffix), step.get("sy" + suffix)
        if sx is None or sy is None:
            raise ToolError("не нашёл окно, в котором нужно щёлкнуть")
        if win is not None or fx is not None:
            self.notes.append("окно изменилось, щелчок по положению на экране")
        sw, sh = self.io.screen_size()
        return int(sx * sw), int(sy * sh)

    # --- выполнение ------------------------------------------------------------
    def run(self) -> Tuple[bool, str]:
        steps = self.task.get("steps", [])
        name = self.task.get("name", "задача")
        if not steps:
            return False, f"В задаче «{name}» нет шагов."
        total = len(steps)
        for n, step in enumerate(steps, 1):
            if not self._wait(float(step.get("delay", 0.5)) / self.speed):
                return False, f"Остановлено по клавише Esc на шаге {n} из {total}."
            try:
                stop = self._do(step)
            except ToolError as exc:
                return False, f"Остановился на шаге {n} из {total}: {exc}."
            if stop:
                return False, f"Остановился на шаге {n} из {total}: {stop}"
            if self._aborted():
                return False, f"Остановлено по клавише Esc на шаге {n} из {total}."
        tail = f" ({'; '.join(sorted(set(self.notes)))})" if self.notes else ""
        return True, f"Задача «{name}» выполнена: {total} шагов.{tail}"

    def _do(self, step: Dict[str, Any]) -> str:
        """Выполняет один шаг. Возвращает причину остановки или пустую строку."""
        k = step.get("kind")
        if k == "window":
            if self._ensure_window(step) is None:
                if not self.io.launch(str(step.get("process", ""))):
                    return f"не вижу программу {step.get('process')}. Откройте её и повторите."
                for _ in range(40):
                    if not self._wait(0.25):
                        return "прервано клавишей Esc."
                    if self._find_window(step):
                        break
                else:
                    return f"программа {step.get('process')} не открылась."
                self._ensure_window(step)
            return ""
        if k == "click":
            x, y = self._point(step)
            self.io.click(x, y, step.get("button", "left"), bool(step.get("double")))
            return ""
        if k == "drag":
            x1, y1 = self._point(step)
            x2, y2 = self._point(step, "2")
            self.io.drag(x1, y1, x2, y2, step.get("button", "left"))
            return ""
        if k == "type":
            if step.get("secret"):
                return "здесь нужно ввести секретный текст (пароль). Введите его сами."
            self.io.type_text(str(step.get("text", "")))
            return ""
        if k == "key":
            if step.get("key") == "esc":
                self._esc_guard = self.clock() + 0.4
            self.io.key(str(step.get("key", "")), int(step.get("count", 1)))
            return ""
        if k == "hotkey":
            if "esc" in step.get("keys", []):
                self._esc_guard = self.clock() + 0.4
            self.io.hotkey(list(step.get("keys", [])))
            return ""
        raise ToolError(f"неизвестный шаг «{k}»")


def make_input() -> Any:
    return Win32Input() if (IS_WIN and not DRY) else DryInput()


def _spawn_player(slug: str, speed: float) -> None:
    exe = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(exe + ["task", "play", slug, "--speed", str(speed)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)


def _save_last_run(task: Dict[str, Any], ok: bool, message: str) -> None:
    try:
        Path(task["_dir"], "last_run.json").write_text(json.dumps(
            {"at": dt.datetime.now().isoformat(timespec="seconds"), "ok": ok, "message": message},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def cmd_task(a) -> str:
    items = load_tasks()
    query = " ".join(a.name).strip()
    if a.action == "list":
        if not items:
            return "Записанных задач пока нет. Скажите: «Джарвис, запомни как я это делаю»."
        lines = [f"{t.get('name')} — {len(t['steps'])} шагов" for t in items]
        return f"Задач: {len(items)}.\n" + "\n".join(lines)
    task = find_task(query, items)
    if task is None:
        have = ", ".join(str(t.get("name")) for t in items[:6]) or "нет ни одной"
        raise ToolError(f"Не нашёл задачу «{query}». Есть: {have}.")
    if a.action == "show":
        lines = [f"{n}. {describe_task_step(s)}" for n, s in enumerate(task["steps"], 1)]
        return f"Задача «{task.get('name')}», {len(lines)} шагов:\n" + "\n".join(lines[:40])
    if a.action == "delete":
        shutil.rmtree(task["_dir"], ignore_errors=True)
        return f"Задача «{task.get('name')}» удалена."
    if a.action == "play" or a.wait:
        ok, message = Player(make_input(), task, a.speed).run()
        _save_last_run(task, ok, message)
        if not ok:
            raise ToolError(message)
        return message
    # run: запускаем отдельным процессом и сразу отвечаем, чтобы Джарвис не молчал всё время выполнения
    if DRY or not IS_WIN:
        print(f"[сухой прогон] запуск задачи {task['slug']}", file=sys.stderr)
    else:
        _spawn_player(task["slug"], a.speed)
    return (f"Запустил задачу «{task.get('name')}», {len(task['steps'])} шагов. "
            "Не трогайте мышь и клавиатуру; чтобы прервать, нажмите Esc.")




# --------------------------------------------------------------------------
# Известные папки, программы меню «Пуск»
# --------------------------------------------------------------------------

KNOWN_FOLDERS = {
    # имя -> (значение в реестре User Shell Folders, папка по умолчанию)
    "рабочий стол": ("Desktop", "Desktop"), "desktop": ("Desktop", "Desktop"),
    "документы": ("Personal", "Documents"), "мои документы": ("Personal", "Documents"), "documents": ("Personal", "Documents"),
    "загрузки": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"), "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "изображения": ("My Pictures", "Pictures"), "картинки": ("My Pictures", "Pictures"), "фото": ("My Pictures", "Pictures"),
    "музыка": ("My Music", "Music"), "видео": ("My Video", "Videos"),
}


def known_folder(name: str) -> Optional[Path]:
    key = (name or "").strip().lower().replace("ё", "е")
    if key not in KNOWN_FOLDERS:
        return None
    reg, default = KNOWN_FOLDERS[key]
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                value, _ = winreg.QueryValueEx(k, reg)
                return Path(os.path.expandvars(value))
        except OSError:
            pass
    return Path.home() / default


def resolve_place(raw: str) -> Path:
    """Путь, известная папка («загрузки») или папка с таким именем на рабочем столе / в профиле."""
    raw = (raw or "").strip().strip('"')
    if not raw:
        raise ToolError("Не сказано, какая папка.")
    kf = known_folder(raw)
    if kf is not None:
        return kf
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if p.is_absolute():
        return p
    for base in [known_folder("рабочий стол"), known_folder("документы"), known_folder("загрузки"), Path.home()]:
        if base is None or not base.is_dir():
            continue
        cand = base / raw
        if cand.exists():
            return cand
        try:
            names = [c for c in base.iterdir() if c.is_dir()]
        except OSError:
            continue
        low = raw.lower().replace("ё", "е")
        for c in names:
            if c.name.lower().replace("ё", "е") == low:
                return c
        best = difflib.get_close_matches(low, [c.name.lower() for c in names], n=1, cutoff=0.8)
        if best:
            return next(c for c in names if c.name.lower() == best[0])
    return (known_folder("рабочий стол") or Path.home()) / raw


def start_menu_dirs() -> List[Path]:
    dirs = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    extra = os.environ.get("JARVIS_START_MENU")
    if extra:
        dirs.append(Path(extra))
    return dirs


def list_apps() -> List[Path]:
    out: List[Path] = []
    for d in start_menu_dirs():
        if d.is_dir():
            for f in d.rglob("*"):
                if f.suffix.lower() in (".lnk", ".url", ".appref-ms") and "uninstall" not in f.name.lower() \
                        and "удал" not in f.name.lower():
                    out.append(f)
    return out


def find_app(query: str) -> Optional[Path]:
    q = query.lower().replace("ё", "е").strip()
    if not q:
        return None
    apps = list_apps()
    names = [a.stem.lower().replace("ё", "е") for a in apps]
    for a, n in zip(apps, names):
        if n == q:
            return a
    starts = [(len(n), a) for a, n in zip(apps, names) if n.startswith(q) or q in n.split()]
    if starts:
        return sorted(starts, key=lambda x: x[0])[0][1]
    best = difflib.get_close_matches(q, names, n=1, cutoff=0.75)
    return apps[names.index(best[0])] if best else None


def cmd_apps(a) -> str:
    q = " ".join(a.query).strip().lower()
    apps = list_apps()
    if q:
        apps = [x for x in apps if q in x.stem.lower()] or ([find_app(q)] if find_app(q) else [])
    if not apps:
        return "Таких программ в меню «Пуск» не нашёл." if q else "Меню «Пуск» пустое или недоступно."
    names = sorted({x.stem for x in apps})
    return f"Программ: {len(names)}. " + "; ".join(names[:40]) + (" …" if len(names) > 40 else "")


# --------------------------------------------------------------------------
# Заметки
# --------------------------------------------------------------------------

def notes_dir() -> Path:
    env = os.environ.get("JARVIS_NOTES")
    if env:
        return Path(env)
    custom = str(load_settings().get("notes", {}).get("dir") or "").strip()
    if custom:
        return Path(os.path.expandvars(os.path.expanduser(custom)))
    return (known_folder("документы") or Path.home() / "Documents") / "Заметки Джарвиса"


def _note_title(text: str) -> str:
    words = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text).split()
    title = " ".join(words[:6]) or "Заметка"
    return title[:60].strip(" .")


def _all_notes() -> List[Path]:
    d = notes_dir()
    if not d.is_dir():
        return []
    return sorted([p for p in d.iterdir() if p.suffix.lower() in (".md", ".txt")],
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _find_note(query: str) -> Optional[Path]:
    notes = _all_notes()
    if not notes:
        return None
    q = query.lower().replace("ё", "е").strip()
    if not q:
        return notes[0]
    stems = [n.stem.lower().replace("ё", "е") for n in notes]
    for n, st in zip(notes, stems):
        if q in st:
            return n
    best = difflib.get_close_matches(q, stems, n=1, cutoff=0.5)
    if best:
        return notes[stems.index(best[0])]
    for n in notes:
        try:
            if q in n.read_text(encoding="utf-8-sig", errors="replace").lower().replace("ё", "е"):
                return n
        except OSError:
            continue
    return None


def cmd_notes(a) -> str:
    text = " ".join(a.text).strip()
    d = notes_dir()
    if a.action == "add":
        if not text:
            raise ToolError("Нечего записать.")
        title = _note_title(a.title or text)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{dt.date.today():%Y-%m-%d} {title}.md"
        stamp = dt.datetime.now().strftime("%H:%M")
        if path.exists():
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n\n{stamp} — {text}\n")
            return f"Дописал в заметку «{title}». Файл: {path}"
        path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
        return f"Заметка «{title}» сохранена. Файл: {path}"
    if a.action == "list":
        notes = _all_notes()
        if not notes:
            return f"Заметок пока нет. Папка: {d}"
        return f"Заметок: {len(notes)}. Последние: " + "; ".join(n.stem for n in notes[:12]) + f". Папка: {d}"
    if a.action == "read":
        note = _find_note(text)
        if note is None:
            raise ToolError("Такой заметки не нашёл.")
        body = note.read_text(encoding="utf-8-sig", errors="replace").strip()
        return f"«{note.stem}»: {body[:2500]}"
    if a.action == "search":
        if not text:
            raise ToolError("Что искать в заметках?")
        q = text.lower().replace("ё", "е")
        hits = []
        for n in _all_notes():
            try:
                for line in n.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                    if q in line.lower().replace("ё", "е"):
                        hits.append(f"{n.stem}: {line.strip()[:120]}")
                        break
            except OSError:
                continue
        return ("Нашёл: " + "; ".join(hits[:10])) if hits else "В заметках этого нет."
    if a.action == "open":
        target = _find_note(text) if text else d
        if target is None:
            raise ToolError("Такой заметки не нашёл.")
        d.mkdir(parents=True, exist_ok=True)
        if DRY or not IS_WIN:
            print(f"[сухой прогон] открыть: {target}", file=sys.stderr)
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
        return f"Открываю {'заметку «' + target.stem + '»' if target != d else 'папку заметок'}."
    raise ToolError("Неизвестное действие с заметками.")


# --------------------------------------------------------------------------
# Папки (окна проводника)
# --------------------------------------------------------------------------

PS_EXPLORER_WINDOWS = (
    "$t = $env:JARVIS_TARGET; $act = $env:JARVIS_ACTION; $n = 0; $sh = New-Object -ComObject Shell.Application;"
    "foreach ($w in @($sh.Windows())) {"
    "  try { $p = $w.Document.Folder.Self.Path } catch { continue };"
    "  if (-not $p) { continue };"
    "  if ($act -eq 'list') { Write-Output $p; continue };"
    "  if ($t -eq '*' -or $p -ieq $t -or (Split-Path $p -Leaf) -ieq $t) { $w.Quit(); $n++ }"
    "};"
    "if ($act -ne 'list') { Write-Output $n }"
)


def cmd_folder(a) -> str:
    raw = " ".join(a.target).strip()
    if a.action == "list":
        if DRY or not IS_WIN:
            return "Открытых папок нет."
        res = run_ps(PS_EXPLORER_WINDOWS, env={"JARVIS_ACTION": "list", "JARVIS_TARGET": ""})
        paths = [l.strip() for l in res.stdout.splitlines() if l.strip()]
        return ("Открыты папки: " + "; ".join(paths)) if paths else "Открытых папок нет."
    if a.action == "close":
        target = "*" if raw.lower() in ("", "все", "всё", "all") else str(resolve_place(raw))
        if DRY or not IS_WIN:
            print(f"[сухой прогон] закрыть папку: {target}", file=sys.stderr)
            return "Закрываю папку."
        res = run_ps(PS_EXPLORER_WINDOWS, env={"JARVIS_ACTION": "close", "JARVIS_TARGET": target})
        lines = res.stdout.strip().splitlines()
        n = int(lines[-1].strip()) if lines and lines[-1].strip().isdigit() else 0
        if n == 0:
            raise ToolError("Такая папка не открыта.")
        return f"Закрыл {n} {plural(n, 'окно', 'окна', 'окон')} проводника."
    path = resolve_place(raw)
    if a.action == "create":
        if DRY:
            print(f"[сухой прогон] создать папку: {path}", file=sys.stderr)
            return f"Папка создана: {path}"
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        return f"Папка уже есть: {path}" if existed else f"Папка создана: {path}"
    if a.action == "open":
        if not path.exists():
            raise ToolError(f"Папки нет: {path}")
        if DRY or not IS_WIN:
            print(f"[сухой прогон] открыть папку: {path}", file=sys.stderr)
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
        return f"Открываю папку {path.name or path}."
    raise ToolError("Неизвестное действие с папкой.")


# --------------------------------------------------------------------------
# Окна программ
# --------------------------------------------------------------------------

SKIP_WINDOWS = {"program manager", "microsoft text input application", "windows input experience", "settings"}
SECRET_TITLE = re.compile(r"парол|password|пин-код|\bpin\b|вход в|sign.?in|log.?in|авторизац|учетн|учётн", re.I)


def _windows(io: Any) -> List[Dict[str, Any]]:
    return [w for w in io.windows() if w.get("title", "").strip().lower() not in SKIP_WINDOWS]


def _find_window(io: Any, query: str) -> Dict[str, Any]:
    wins = _windows(io)
    q = query.lower().replace("ё", "е").strip()
    if not q:
        raise ToolError("Не сказано, какое окно.")
    for w in wins:
        proc = os.path.splitext(w.get("process", ""))[0].lower()
        if q == proc or q in w.get("title", "").lower().replace("ё", "е"):
            return w
    titles = [w.get("title", "").lower() for w in wins]
    best = difflib.get_close_matches(q, titles + [os.path.splitext(w.get("process", ""))[0].lower() for w in wins],
                                     n=1, cutoff=0.6)
    if best:
        for w in wins:
            if best[0] in (w.get("title", "").lower(), os.path.splitext(w.get("process", ""))[0].lower()):
                return w
    raise ToolError(f"Окно «{query}» не найдено.")


def cmd_windows(_a) -> str:
    io = make_input()
    wins = _windows(io)
    if not wins:
        return "Открытых окон нет."
    lines = [f"{w['title'][:70]} ({w.get('process') or '?'})" for w in wins[:25]]
    return f"Открыто окон: {len(wins)}. " + "; ".join(lines)


def cmd_window(a) -> str:
    io = make_input()
    w = _find_window(io, " ".join(a.target))
    title = w.get("title", "")
    if a.action == "focus":
        io.focus(w)
        return f"Переключился на «{title[:60]}»."
    if DRY or not IS_WIN:
        print(f"[сухой прогон] окно {a.action}: {title}", file=sys.stderr)
        return f"Готово: {title[:60]}."
    u32 = ctypes.windll.user32
    if a.action == "close":
        u32.PostMessageW(w["hwnd"], 0x0010, 0, 0)          # WM_CLOSE: программа сама спросит про несохранённое
        return f"Закрываю «{title[:60]}»."
    if a.action == "minimize":
        u32.ShowWindow(w["hwnd"], 6)
        return f"Свернул «{title[:60]}»."
    if a.action == "maximize":
        u32.ShowWindow(w["hwnd"], 3)
        return f"Развернул «{title[:60]}»."
    raise ToolError("Неизвестное действие с окном.")


# --------------------------------------------------------------------------
# Клавиатура
# --------------------------------------------------------------------------

KEY_ALIASES = {"control": "ctrl", "контрол": "ctrl", "enter": "enter", "ввод": "enter", "return": "enter",
               "escape": "esc", "пробел": "space", "del": "delete", "windows": "win", "cmd": "win"}


def cmd_keys(a) -> str:
    io = make_input()
    fg = io.foreground() or {}
    if SECRET_TITLE.search(fg.get("title", "") or ""):
        raise ToolError("Сейчас открыто окно входа или пароля: туда я ничего не набираю.")
    text = " ".join(a.text)
    if a.action == "type":
        if not text:
            raise ToolError("Нечего набирать.")
        io.type_text(text)
        return f"Набрал текст ({len(text)} симв.)."
    combo = [KEY_ALIASES.get(k.strip().lower(), k.strip().lower()) for k in re.split(r"[+\s]+", text) if k.strip()]
    if not combo:
        raise ToolError("Не сказано, какие клавиши.")
    unknown = [k for k in combo if k not in KEY_VK]
    if unknown:
        raise ToolError("Не знаю клавиши: " + ", ".join(unknown))
    if len(combo) == 1:
        io.key(combo[0])
    else:
        io.hotkey(combo)
    return "Нажал " + "+".join(combo) + "."


# --------------------------------------------------------------------------
# Интернет: поиск и чтение страниц
# --------------------------------------------------------------------------

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
TAG_RE = re.compile(r"<[^>]+>")


def _fetch(url: str, timeout: float = 12.0, limit: int = 2_000_000) -> Tuple[str, str]:
    """(текст ответа, итоговый адрес). Кодировка — из заголовка или meta."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "ru,en;q=0.8",
                                               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(limit)
            charset = resp.headers.get_content_charset() or ""
            final = resp.geturl()
    except urllib.error.HTTPError as exc:
        raise ToolError(f"Сайт ответил ошибкой {exc.code}.") from None
    except (urllib.error.URLError, OSError) as exc:
        raise ToolError("Нет связи с сайтом. Проверьте интернет.") from exc
    if not charset:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4000], re.I)
        charset = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        return raw.decode(charset, "replace"), final
    except LookupError:
        return raw.decode("utf-8", "replace"), final


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", fragment))).strip()


def parse_ddg(page: str) -> List[Dict[str, str]]:
    out = []
    starts = list(re.finditer(r'<div[^>]+class="([^"]*\bresult\b[^"]*)"', page))
    for i, m0 in enumerate(starts):
        if "result--ad" in m0.group(1):
            continue                                  # реклама
        block = page[m0.end():starts[i + 1].start() if i + 1 < len(starts) else len(page)]
        m = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        href = html.unescape(m.group(1))
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        if href.startswith("//"):
            href = "https:" + href
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', block, re.S)
        out.append({"title": _clean(m.group(2)), "url": href, "snippet": _clean(sn.group(1)) if sn else ""})
    return out


def parse_bing(page: str) -> List[Dict[str, str]]:
    out = []
    for block in page.split('<li class="b_algo"')[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        sn = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append({"title": _clean(m.group(2)), "url": html.unescape(m.group(1)), "snippet": _clean(sn.group(1)) if sn else ""})
    return out


def web_search(query: str) -> List[Dict[str, str]]:
    q = urllib.parse.quote_plus(query)
    errors = []
    for url, parse in ((f"https://html.duckduckgo.com/html/?q={q}&kl=ru-ru", parse_ddg),
                       (f"https://www.bing.com/search?q={q}&setlang=ru", parse_bing)):
        try:
            page, _ = _fetch(url)
            found = parse(page)
            if found:
                return found
        except ToolError as exc:
            errors.append(str(exc))
    if errors and len(errors) == 2:
        raise ToolError(errors[0])
    return []


def page_text(page: str) -> Tuple[str, str]:
    title_m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    title = _clean(title_m.group(1)) if title_m else ""
    body = re.sub(r"<(script|style|noscript|svg|nav|footer|header|form)\b.*?</\1>", " ", page, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"</(p|div|li|h\d|tr|br)>", "\n", body, flags=re.I)
    text = html.unescape(TAG_RE.sub(" ", body))
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if len(l) > 30]
    return title, "\n".join(lines)


def cmd_web(a) -> str:
    text = " ".join(a.query).strip()
    if not text:
        raise ToolError("Не сказано, что искать.")
    if a.action == "search":
        if DRY:
            return f"[сухой прогон] поиск: {text}"
        results = web_search(text)
        if not results:
            return "Поиск ничего не нашёл."
        return "\n".join(f"{i}. {r['title']} — {r['url']}\n   {r['snippet'][:220]}" for i, r in enumerate(results[:6], 1))
    if a.action == "read":
        url = text if re.match(r"https?://", text) else "https://" + text
        if DRY:
            return f"[сухой прогон] чтение: {url}"
        page, final = _fetch(url)
        title, body = page_text(page)
        if not body:
            return f"«{title or final}»: на странице нет текста (возможно, она строится скриптами)."
        return f"«{title or final}» ({final}):\n{body[:3500]}"
    if a.action == "open":
        url = text if re.match(r"https?://", text) else "https://www.google.com/search?q=" + urllib.parse.quote_plus(text)
        if DRY or not IS_WIN:
            print(f"[сухой прогон] открыть: {url}", file=sys.stderr)
        else:
            os.startfile(url)  # type: ignore[attr-defined]
        return "Открываю в браузере."
    raise ToolError("Неизвестное действие.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pc.py", description="Инструменты ПК для Джарвиса")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn in [("minimize-all", cmd_minimize_all), ("restore-all", cmd_restore_all),
                     ("maximize", cmd_maximize), ("minimize", cmd_minimize),
                     ("timer-cancel", cmd_timer_cancel), ("lock", cmd_lock), ("now", cmd_now),
                     ("sysinfo", cmd_sysinfo), ("network", cmd_network)]:
        sub.add_parser(name).set_defaults(func=fn)

    t = sub.add_parser("timer"); t.add_argument("minutes", type=int); t.set_defaults(func=cmd_timer)

    v = sub.add_parser("volume")
    v.add_argument("action", choices=["up", "down", "mute", "set"])
    v.add_argument("value", nargs="?", type=int)
    v.set_defaults(func=cmd_volume)

    o = sub.add_parser("open"); o.add_argument("target", nargs="+"); o.set_defaults(func=cmd_open)

    g = sub.add_parser("game-mode"); g.add_argument("state", choices=["on", "off"]); g.set_defaults(func=cmd_game_mode)

    w = sub.add_parser("weather"); w.add_argument("city", nargs="*"); w.set_defaults(func=cmd_weather)

    r = sub.add_parser("rate"); r.add_argument("currency", nargs="*"); r.set_defaults(func=cmd_rate)

    # Файлы
    f = sub.add_parser("files")
    f.add_argument("action", choices=["list", "search", "size", "move", "copy", "delete", "create-folder", "read"])
    f.add_argument("path", nargs="*")
    f.add_argument("--to", dest="extra", nargs="*", default=[])
    f.set_defaults(func=cmd_files)

    # Процессы
    ps = sub.add_parser("processes"); ps.add_argument("query", nargs="*"); ps.set_defaults(func=cmd_processes)
    pk = sub.add_parser("process-kill"); pk.add_argument("target", nargs="+"); pk.set_defaults(func=cmd_process_kill)

    # Буфер обмена
    cb = sub.add_parser("clipboard")
    cb.add_argument("action", choices=["get", "set"])
    cb.add_argument("text", nargs="*")
    cb.set_defaults(func=cmd_clipboard)

    # Скриншот
    ss = sub.add_parser("screenshot"); ss.add_argument("path", nargs="*"); ss.set_defaults(func=cmd_screenshot)

    # Выполнение команд
    rn = sub.add_parser("run"); rn.add_argument("command", nargs="+"); rn.set_defaults(func=cmd_run)

    # Записанные задачи
    tk = sub.add_parser("task")
    tk.add_argument("action", choices=["list", "show", "run", "play", "delete"])
    tk.add_argument("name", nargs="*")
    tk.add_argument("--wait", action="store_true")
    tk.add_argument("--speed", type=float, default=1.0)
    tk.set_defaults(func=cmd_task)

    # Уведомления
    nt = sub.add_parser("notify"); nt.add_argument("text", nargs="+"); nt.set_defaults(func=cmd_notify)

    # Заметки
    no = sub.add_parser("notes")
    no.add_argument("action", choices=["add", "list", "read", "search", "open"])
    no.add_argument("text", nargs="*")
    no.add_argument("--title", default="")
    no.set_defaults(func=cmd_notes)

    # Папки
    fo = sub.add_parser("folder")
    fo.add_argument("action", choices=["open", "close", "create", "list"])
    fo.add_argument("target", nargs="*")
    fo.set_defaults(func=cmd_folder)

    # Окна
    sub.add_parser("windows").set_defaults(func=cmd_windows)
    wi = sub.add_parser("window")
    wi.add_argument("action", choices=["close", "focus", "minimize", "maximize"])
    wi.add_argument("target", nargs="+")
    wi.set_defaults(func=cmd_window)

    # Клавиатура
    ky = sub.add_parser("keys")
    ky.add_argument("action", choices=["type", "press"])
    ky.add_argument("text", nargs="+")
    ky.set_defaults(func=cmd_keys)

    # Интернет
    wb = sub.add_parser("web")
    wb.add_argument("action", choices=["search", "read", "open"])
    wb.add_argument("query", nargs="+")
    wb.set_defaults(func=cmd_web)

    # Программы из меню «Пуск»
    ap = sub.add_parser("apps"); ap.add_argument("query", nargs="*"); ap.set_defaults(func=cmd_apps)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        print(args.func(args))
        return 0
    except ToolError as exc:
        print(f"Не получилось: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — любая неожиданность читается вслух
        print(f"Не получилось: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
