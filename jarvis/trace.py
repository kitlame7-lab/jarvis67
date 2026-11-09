"""Превращение сырых событий записи (клики, клавиши, смена окон) в аккуратные шаги задачи.

Это чистая логика без Windows и без Qt: на вход список событий, на выход список шагов. Поэтому её можно полностью
проверить тестами. Нейросеть шаги не переписывает: она только называет задачу и помечает лишнее (см. teach.py).

Событие — словарь с полем `t` (секунды от начала записи) и `type`:
    {"t": 1.2, "type": "mouse", "action": "down"|"up", "button": "left", "x": 640, "y": 360, "win": WIN}
    {"t": 2.0, "type": "key", "name": "a", "char": "а", "mods": ["ctrl"], "win": WIN}
    {"t": 0.0, "type": "window", "win": WIN}
где WIN = {"title": str, "process": "EXCEL.EXE", "rect": [left, top, right, bottom]}.
Размер экрана передаётся отдельно: `screen=(ширина, высота)`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

MAX_STEPS = 400
CLICK_MAX_SECONDS = 0.6          # дольше — это уже не щелчок
CLICK_MAX_MOVE_PX = 8            # дальше — это перетаскивание
DOUBLE_CLICK_SECONDS = 0.5
TYPING_GAP_SECONDS = 5.0         # пауза, после которой набор считается новым шагом
MAX_DELAY = 3.0                  # пауза между шагами при повторе
MIN_DELAY = 0.3

SPECIAL_KEYS = {"enter", "tab", "esc", "backspace", "delete", "up", "down", "left", "right", "home", "end",
                "pageup", "pagedown", "insert", "win", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9",
                "f10", "f11", "f12"}
MOD_ORDER = ["win", "ctrl", "alt", "shift"]
OWN_PROCESSES = {"jarvis.exe"}
OWN_TITLES = ("джарвис",)

# Окна, в которых набирают пароли и другие секреты: текст оттуда не сохраняется
SECRET_TITLE = re.compile(
    r"парол|password|passwd|пин[- ]?код|\bpin\b|sign[ -]?in|log[ -]?in|\bвход\b|авториз|учётн|учетн|credential|"
    r"секрет|token|токен|\bcvv\b|\bcvc\b|банковск|карт[аы]", re.I)


def is_secret_window(title: str) -> bool:
    return bool(SECRET_TITLE.search(title or ""))


def _is_own(win: Optional[Dict[str, Any]]) -> bool:
    if not win:
        return False
    if str(win.get("process", "")).lower() in OWN_PROCESSES:
        return True
    return str(win.get("title", "")).lower().startswith(OWN_TITLES)


def _fractions(x: float, y: float, rect: Optional[List[int]]) -> Optional[Tuple[float, float]]:
    if not rect or len(rect) != 4:
        return None
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    return (round(min(1.0, max(0.0, (x - left) / w)), 4), round(min(1.0, max(0.0, (y - top) / h)), 4))


def _size(rect: Optional[List[int]]) -> Optional[List[int]]:
    if not rect or len(rect) != 4 or rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
        return None
    return [rect[2] - rect[0], rect[3] - rect[1]]


def _win_ref(win: Optional[Dict[str, Any]]) -> Dict[str, str]:
    win = win or {}
    return {"process": str(win.get("process", "")), "title": str(win.get("title", ""))[:120]}


def _key_label(name: str) -> str:
    return {"esc": "Esc", "enter": "Enter", "pageup": "PageUp", "pagedown": "PageDown"}.get(name, name.capitalize() if len(name) > 1 else name.upper())


def compile_steps(events: List[Dict[str, Any]], screen: Tuple[int, int] = (1920, 1080)) -> List[Dict[str, Any]]:
    """События → шаги. Шаги: window, click, drag, type, key, hotkey. У каждого `delay` (пауза перед шагом)."""
    steps: List[Dict[str, Any]] = []
    sw, sh = max(1, screen[0]), max(1, screen[1])
    state: Dict[str, Any] = {"process": None, "typing": None, "down": {}, "last_end": 0.0}

    def push(step: Dict[str, Any], t_start: float, t_end: Optional[float] = None) -> Dict[str, Any]:
        gap = t_start - state["last_end"]
        step["delay"] = round(min(MAX_DELAY, max(MIN_DELAY, gap)), 1) if steps else 0.5
        state["last_end"] = t_end if t_end is not None else t_start
        steps.append(step)
        return step

    def enter_window(win: Optional[Dict[str, Any]], t: float) -> None:
        proc = str((win or {}).get("process", ""))
        if proc and proc != state["process"]:
            flush_typing()
            state["process"] = proc
            push({"kind": "window", **_win_ref(win)}, t)

    def flush_typing() -> None:
        typing = state["typing"]
        state["typing"] = None
        if not typing or not typing["text"]:
            return
        secret = typing.get("secret") or is_secret_window(typing["win"].get("title", ""))
        step: Dict[str, Any] = {"kind": "type", "length": len(typing["text"]), "window": _win_ref(typing["win"])}
        if secret:
            step["secret"] = True
        else:
            step["text"] = typing["text"]
        push(step, typing["t0"], typing["t1"])

    for ev in sorted(events, key=lambda e: e.get("t", 0.0)):
        t = float(ev.get("t", 0.0))
        win = ev.get("win")
        if _is_own(win):
            continue
        kind = ev.get("type")

        if kind == "window":
            enter_window(win, t)
            continue

        if kind == "mouse":
            enter_window(win, t)
            button = ev.get("button", "left")
            if ev.get("action") == "down":
                state["down"][button] = ev
                continue
            start = state["down"].pop(button, None)
            if start is None:
                continue
            flush_typing()
            moved = max(abs(ev["x"] - start["x"]), abs(ev["y"] - start["y"]))
            rect = (start.get("win") or {}).get("rect")
            fr = _fractions(start["x"], start["y"], rect)
            ref = _win_ref(start.get("win"))
            if moved > CLICK_MAX_MOVE_PX and t - start["t"] > 0.05:
                end_fr = _fractions(ev["x"], ev["y"], rect)
                if fr and end_fr:
                    push({"kind": "drag", "button": button, "window": ref, "size": _size(rect), "x": fr[0], "y": fr[1],
                          "x2": end_fr[0], "y2": end_fr[1], "sx": round(start["x"] / sw, 4), "sy": round(start["y"] / sh, 4),
                          "sx2": round(ev["x"] / sw, 4), "sy2": round(ev["y"] / sh, 4),
                          "shot": start.get("shot") or ev.get("shot")}, start["t"], t)
                continue
            if t - start["t"] > CLICK_MAX_SECONDS and moved <= CLICK_MAX_MOVE_PX:
                pass            # долгое нажатие считаем обычным щелчком
            prev = steps[-1] if steps else None
            if (prev and prev.get("kind") == "click" and not prev.get("double") and prev.get("button") == button
                    and start["t"] - state["last_end"] < DOUBLE_CLICK_SECONDS
                    and abs(prev.get("px", -999) - start["x"]) <= CLICK_MAX_MOVE_PX
                    and abs(prev.get("py", -999) - start["y"]) <= CLICK_MAX_MOVE_PX):
                prev["double"] = True
                state["last_end"] = t
                continue
            step = {"kind": "click", "button": button, "double": False, "window": ref, "size": _size(rect), "px": start["x"], "py": start["y"],
                    "sx": round(start["x"] / sw, 4), "sy": round(start["y"] / sh, 4), "shot": start.get("shot") or ev.get("shot")}
            if fr:
                step["x"], step["y"] = fr
            push(step, start["t"], t)
            continue

        if kind == "key":
            enter_window(win, t)
            name = str(ev.get("name", ""))
            char = ev.get("char")
            mods = [m for m in MOD_ORDER if m in (ev.get("mods") or [])]
            plain = not [m for m in mods if m in ("ctrl", "alt", "win")] or (ev.get("altgr") and char)
            if char and plain:
                typing = state["typing"]
                if typing and (t - typing["t1"] > TYPING_GAP_SECONDS or typing["win"].get("process") != (win or {}).get("process")):
                    flush_typing()
                    typing = None
                if not typing:
                    typing = state["typing"] = {"text": "", "t0": t, "t1": t, "win": win or {}}
                typing["text"] += char
                typing["secret"] = typing.get("secret") or bool(ev.get("secret"))
                typing["t1"] = t
                continue
            if name == "backspace" and not mods and state["typing"] and state["typing"]["text"]:
                state["typing"]["text"] = state["typing"]["text"][:-1]
                state["typing"]["t1"] = t
                continue
            flush_typing()
            if mods and name not in ("ctrl", "alt", "shift", "win"):
                push({"kind": "hotkey", "keys": mods + [name]}, t)
            elif name in SPECIAL_KEYS or name:
                prev = steps[-1] if steps else None
                if prev and prev.get("kind") == "key" and prev.get("key") == name and t - state["last_end"] < 1.0:
                    prev["count"] = prev.get("count", 1) + 1
                    state["last_end"] = t
                else:
                    push({"kind": "key", "key": name, "count": 1}, t)

    flush_typing()
    steps = steps[:MAX_STEPS]
    for i, s in enumerate(steps, 1):
        s["i"] = i
    return steps


def describe_step(step: Dict[str, Any], mask_text: bool = True) -> str:
    """Короткое описание шага по-русски. Набранный текст по умолчанию скрыт (показана только длина)."""
    k = step.get("kind")
    title = (step.get("window") or {}).get("title") or (step.get("window") or {}).get("process") or ""
    where = f" в окне «{title[:50]}»" if title else ""
    if k == "window":
        return f"Переключился на программу {step.get('process') or '?'}" + (f" («{step['title'][:50]}»)" if step.get("title") else "")
    if k == "click":
        btn = {"left": "левой", "right": "правой", "middle": "средней"}.get(step.get("button", "left"), "левой")
        dbl = "Двойной щелчок" if step.get("double") else "Щелчок"
        pos = f" ({round(step['x'] * 100)}% по ширине, {round(step['y'] * 100)}% по высоте)" if "x" in step else ""
        return f"{dbl} {btn} кнопкой{where}{pos}"
    if k == "drag":
        return f"Перетаскивание{where}"
    if k == "type":
        if step.get("secret"):
            return f"Ввод секретного текста ({step.get('length', 0)} симв., не сохранён){where}"
        text = step.get("text", "")
        return f"Ввод текста ({len(text)} симв.){where}" if mask_text else f"Ввод текста «{text[:80]}»{where}"
    if k == "key":
        n = step.get("count", 1)
        return f"Клавиша {_key_label(step.get('key', '?'))}" + (f" ×{n}" if n > 1 else "")
    if k == "hotkey":
        return "Сочетание " + "+".join(_key_label(x) for x in step.get("keys", []))
    return str(k)
