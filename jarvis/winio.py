"""Чтение действий пользователя в Windows: где мышь, какие кнопки и клавиши нажаты, какое окно активно.

Хуков нет: состояние опрашивается ~120 раз в секунду (GetAsyncKeyState, GetCursorPos). Такой способ не требует прав
администратора, не вмешивается в ввод и не тормозит систему, а антивирусы не принимают его за кейлоггер-хук.
Цена: клавиша, нажатая и отпущенная быстрее ~8 мс, может потеряться, а зажатая клавиша даёт одно нажатие.

Слой разделён на две части, чтобы логику можно было проверять без Windows:
  * `WinApi` — тонкая обёртка над ctypes (работает только в Windows);
  * `InputSampler` — превращает снимки состояния в события для `trace.compile_steps`; принимает любой объект с теми же
    методами (в тестах это `FakeApi`).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger("jarvis")

MOUSE_BUTTONS = {"left": 0x01, "right": 0x02, "middle": 0x04}
MODIFIER_VKS = {0x10: "shift", 0xA0: "shift", 0xA1: "shift", 0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
                0x12: "alt", 0xA4: "alt", 0xA5: "alt", 0x5B: "win", 0x5C: "win"}
NAMED_KEYS = {0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "esc", 0x20: "space", 0x21: "pageup",
              0x22: "pagedown", 0x23: "end", 0x24: "home", 0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
              0x2D: "insert", 0x2E: "delete", 0x14: "capslock", 0x91: "scrolllock", 0x13: "pause", 0x2C: "printscreen"}
PUNCT_NAMES = {0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
               0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'"}


def _watched_vks() -> List[int]:
    vks = set(NAMED_KEYS) | set(MODIFIER_VKS) | set(PUNCT_NAMES) | {0xE2}
    vks |= set(range(0x30, 0x3A)) | set(range(0x41, 0x5B)) | set(range(0x60, 0x70)) | set(range(0x70, 0x7C))
    return sorted(vks)


WATCHED_VKS = _watched_vks()


def vk_name(vk: int) -> str:
    """Имя клавиши, не зависящее от раскладки: так же его понимает проигрыватель задач в pc.py."""
    if vk in NAMED_KEYS:
        return NAMED_KEYS[vk]
    if vk in MODIFIER_VKS:
        return MODIFIER_VKS[vk]
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x60 <= vk <= 0x69:
        return str(vk - 0x60)
    if 0x70 <= vk <= 0x7B:
        return f"f{vk - 0x6F}"
    return PUNCT_NAMES.get(vk, f"vk{vk}")


def modifiers_from(down: Set[int]) -> List[str]:
    names = {MODIFIER_VKS[v] for v in down if v in MODIFIER_VKS}
    return [m for m in ("win", "ctrl", "alt", "shift") if m in names]


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

class WinApi:
    """Вызовы Windows через ctypes. Создаётся только в Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Запись действий работает только в Windows")
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u32.GetAsyncKeyState.restype = ctypes.c_short
        self.u32.WindowFromPoint.argtypes = [wintypes.POINT]
        self.u32.WindowFromPoint.restype = wintypes.HWND
        self.u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.u32.GetAncestor.restype = wintypes.HWND
        self.u32.GetForegroundWindow.restype = wintypes.HWND
        self.u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.u32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        self.u32.GetKeyboardLayout.restype = wintypes.HKL
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            self.u32.SetProcessDPIAware()
        except OSError:
            pass
        self._proc_cache: Dict[int, str] = {}

    # --- мышь -------------------------------------------------------------
    def cursor(self) -> Tuple[int, int]:
        pt = self.wt.POINT()
        self.u32.GetCursorPos(self.ct.byref(pt))
        return pt.x, pt.y

    def buttons(self) -> Dict[str, bool]:
        return {name: bool(self.u32.GetAsyncKeyState(vk) & 0x8000) for name, vk in MOUSE_BUTTONS.items()}

    def screen_size(self) -> Tuple[int, int]:
        return self.u32.GetSystemMetrics(0), self.u32.GetSystemMetrics(1)

    # --- клавиатура ---------------------------------------------------------
    def keys(self) -> Set[int]:
        return {vk for vk in WATCHED_VKS if self.u32.GetAsyncKeyState(vk) & 0x8000}

    def char_for(self, vk: int, mods: List[str]) -> Optional[str]:
        """Символ, который даёт клавиша в текущей раскладке активного окна (None — не печатаемая)."""
        if vk in MODIFIER_VKS or (vk in NAMED_KEYS and vk != 0x20) or 0x70 <= vk <= 0x7B:
            return None
        if vk == 0x20:
            return " "
        hwnd = self.u32.GetForegroundWindow()
        tid = self.u32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
        hkl = self.u32.GetKeyboardLayout(tid)
        state = (self.ct.c_ubyte * 256)()
        if "shift" in mods:
            state[0x10] = 0x80
        if self.u32.GetKeyState(0x14) & 1:                    # Caps Lock
            state[0x14] = 0x01
        scan = self.u32.MapVirtualKeyExW(vk, 0, hkl)
        buf = self.ct.create_unicode_buffer(8)
        n = self.u32.ToUnicodeEx(vk, scan, state, buf, 8, 0x4, hkl)   # 0x4: не менять состояние «мёртвых» клавиш
        if n >= 1 and buf.value and buf.value[0] >= " ":
            return buf.value[0]
        return None

    def focus_is_password(self) -> bool:
        """Фокус в поле ввода пароля (стиль ES_PASSWORD у обычных полей Windows; в браузерах определить нельзя)."""
        class GUITHREADINFO(self.ct.Structure):
            _fields_ = [("cbSize", self.wt.DWORD), ("flags", self.wt.DWORD), ("hwndActive", self.wt.HWND),
                        ("hwndFocus", self.wt.HWND), ("hwndCapture", self.wt.HWND), ("hwndMenuOwner", self.wt.HWND),
                        ("hwndMoveSize", self.wt.HWND), ("hwndCaret", self.wt.HWND), ("rcCaret", self.wt.RECT)]
        info = GUITHREADINFO()
        info.cbSize = self.ct.sizeof(info)
        if not self.u32.GetGUIThreadInfo(0, self.ct.byref(info)) or not info.hwndFocus:
            return False
        style = self.u32.GetWindowLongW(info.hwndFocus, -16)
        cls = self.ct.create_unicode_buffer(64)
        self.u32.GetClassNameW(info.hwndFocus, cls, 64)
        return cls.value.lower().startswith("edit") and bool(style & 0x20)

    # --- окна ---------------------------------------------------------------
    def _describe(self, hwnd) -> Optional[Dict[str, Any]]:
        if not hwnd:
            return None
        title = self.ct.create_unicode_buffer(256)
        self.u32.GetWindowTextW(hwnd, title, 256)
        rect = self.wt.RECT()
        self.u32.GetWindowRect(hwnd, self.ct.byref(rect))
        pid = self.wt.DWORD()
        self.u32.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        return {"title": title.value, "process": self._process_name(pid.value),
                "rect": [rect.left, rect.top, rect.right, rect.bottom]}

    def _process_name(self, pid: int) -> str:
        if pid in self._proc_cache:
            return self._proc_cache[pid]
        name = ""
        handle = self.k32.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                buf = self.ct.create_unicode_buffer(520)
                size = self.wt.DWORD(520)
                if self.k32.QueryFullProcessImageNameW(handle, 0, buf, self.ct.byref(size)):
                    name = os.path.basename(buf.value)
            finally:
                self.k32.CloseHandle(handle)
        if len(self._proc_cache) > 256:
            self._proc_cache.clear()
        self._proc_cache[pid] = name
        return name

    def foreground(self) -> Optional[Dict[str, Any]]:
        return self._describe(self.u32.GetForegroundWindow())

    def window_at(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        hwnd = self.u32.WindowFromPoint(self.wt.POINT(x, y))
        return self._describe(self.u32.GetAncestor(hwnd, 2) if hwnd else None)      # GA_ROOT

    # --- снимок экрана --------------------------------------------------------
    def grab_jpeg(self, path: str, max_width: int = 960) -> None:
        """Снимок всего экрана в JPEG уменьшенного размера (GDI + Qt для сжатия)."""
        from PySide6.QtGui import QImage
        ct, u32 = self.ct, self.u32
        gdi = ct.WinDLL("gdi32", use_last_error=True)
        w, h = self.screen_size()
        hdc = u32.GetDC(0)
        mem = gdi.CreateCompatibleDC(hdc)
        bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
        old = gdi.SelectObject(mem, bmp)
        try:
            gdi.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)       # SRCCOPY

            class BMI(ct.Structure):
                _fields_ = [("biSize", self.wt.DWORD), ("biWidth", self.wt.LONG), ("biHeight", self.wt.LONG),
                            ("biPlanes", self.wt.WORD), ("biBitCount", self.wt.WORD), ("biCompression", self.wt.DWORD),
                            ("biSizeImage", self.wt.DWORD), ("x", self.wt.LONG), ("y", self.wt.LONG),
                            ("u", self.wt.DWORD), ("i", self.wt.DWORD)]
            bmi = BMI(ct.sizeof(BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = (ct.c_ubyte * (w * h * 4))()
            gdi.GetDIBits(mem, bmp, 0, h, buf, ct.byref(bmi), 0)
            image = QImage(bytes(buf), w, h, w * 4, QImage.Format.Format_RGB32)
            if w > max_width:
                image = image.scaledToWidth(max_width)
            image.save(path, "JPG", 70)
        finally:
            gdi.SelectObject(mem, old)
            gdi.DeleteObject(bmp)
            gdi.DeleteDC(mem)
            u32.ReleaseDC(0, hdc)


# --------------------------------------------------------------------------
# Опрос состояния
# --------------------------------------------------------------------------

class InputSampler:
    """Следит за мышью и клавиатурой и копит события в формате `trace.compile_steps`."""

    def __init__(self, api: Any, clock: Callable[[], float] = time.monotonic, interval: float = 0.008,
                 max_events: int = 20000):
        self.api, self.clock, self.interval, self.max_events = api, clock, interval, max_events
        self.events: List[Dict[str, Any]] = []
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._t0 = clock()
        self._buttons: Dict[str, bool] = {n: False for n in MOUSE_BUTTONS}
        self._keys: Set[int] = set()
        self._last_win: Optional[str] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.errors = 0

    def _emit(self, ev: Dict[str, Any]) -> None:
        with self._lock:
            if len(self.events) < self.max_events:
                self.events.append(ev)
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 — сбой подписчика не должен останавливать запись
                log.exception("Сбой обработчика записи")

    def poll(self) -> None:
        """Один опрос: сравнить текущее состояние с прошлым и породить события."""
        t = round(self.clock() - self._t0, 3)
        api = self.api
        x, y = api.cursor()
        buttons = api.buttons()
        for name, pressed in buttons.items():
            if pressed != self._buttons[name]:
                self._buttons[name] = pressed
                self._emit({"t": t, "type": "mouse", "action": "down" if pressed else "up",
                            "button": name, "x": x, "y": y, "win": api.window_at(x, y)})
        down = set(api.keys())
        fresh = down - self._keys
        self._keys = down
        if not fresh:
            return
        mods = modifiers_from(down)
        win = api.foreground()
        secret = None
        for vk in sorted(fresh):
            if vk in MODIFIER_VKS:
                continue
            char = None if set(mods) & {"ctrl", "alt", "win"} else api.char_for(vk, mods)
            ev = {"t": t, "type": "key", "name": vk_name(vk), "char": char, "mods": mods, "win": win}
            if char:
                if secret is None:
                    secret = bool(api.focus_is_password())
                if secret:
                    ev["secret"] = True
            self._emit(ev)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._t0 = self.clock()
        self._thread = threading.Thread(target=self._loop, name="jarvis-input", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001 — одно неудачное чтение не должно обрывать запись
                self.errors += 1
                if self.errors in (1, 50, 500):
                    log.exception("Сбой чтения ввода (%s)", self.errors)
            self._stop.wait(self.interval)

    def stop(self) -> List[Dict[str, Any]]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        with self._lock:
            return list(self.events)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def elapsed(self) -> float:
        return self.clock() - self._t0
