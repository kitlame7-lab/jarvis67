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
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def run_cmd(argv: List[str]) -> subprocess.CompletedProcess:
    if DRY:
        print(f"[сухой прогон] {' '.join(argv)}", file=sys.stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=flags)


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
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pc.py", description="Инструменты ПК для Джарвиса")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn in [("minimize-all", cmd_minimize_all), ("restore-all", cmd_restore_all),
                     ("maximize", cmd_maximize), ("minimize", cmd_minimize),
                     ("timer-cancel", cmd_timer_cancel), ("lock", cmd_lock), ("now", cmd_now)]:
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
