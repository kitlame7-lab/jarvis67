"""Диагностика: хронология запуска, журнал ошибок, проверка связи, выводы человеческим языком и отчёт.

Задача модуля: чтобы на вопрос «почему не работает» можно было ответить одним нажатием, без пересылки журналов
и скриншотов. Поэтому всё собирается само (`gather`), превращается в выводы (`analyze`, чистая функция без сети и
файлов, её легко проверить тестом) и оформляется в отчёт без секретов (`render_text`), который можно сразу
отправить в Issues репозитория (`issue_url`).
"""
from __future__ import annotations

import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import REPO, __version__, paths
from .core import log

ERRORS_PATH = paths.DATA_DIR / "errors.jsonl"
MAX_ERRORS = 200
LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2, "ok": 3}


# --------------------------------------------------------------------------
# Хронология: на каком этапе сейчас запуск и сколько он длится
# --------------------------------------------------------------------------

class Timeline:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: List[Dict[str, Any]] = []

    def begin(self, stage: str, note: str = "") -> None:
        now = time.time()
        with self._lock:
            if self._items and self._items[-1]["ended"] is None:
                self._items[-1]["ended"] = now
            self._items.append({"stage": stage, "note": note, "started": now, "ended": None})
            del self._items[:-60]

    def end(self) -> None:
        with self._lock:
            if self._items and self._items[-1]["ended"] is None:
                self._items[-1]["ended"] = time.time()

    def current(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._items and self._items[-1]["ended"] is None:
                it = dict(self._items[-1])
                it["elapsed"] = time.time() - it["started"]
                return it
        return None

    def snapshot(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [{"stage": i["stage"], "note": i["note"], "seconds": round((i["ended"] or now) - i["started"], 1),
                     "running": i["ended"] is None} for i in self._items]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


TIMELINE = Timeline()


# --------------------------------------------------------------------------
# Секреты никогда не попадают в отчёт
# --------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (re.compile(r"\b(sk|pk|rk|key|tok)[-_][A-Za-z0-9_\-]{8,}\b", re.I), "[ключ скрыт]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"), "[токен бота скрыт]"),
    (re.compile(r"(?i)\b(bearer|authorization:?)\s+[A-Za-z0-9._\-]{8,}"), r"\1 [скрыто]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\s*[=:]\s*\S+"), r"\1=[скрыто]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "[длинная строка скрыта]"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[скрыто]@"),
]


def scrub(text: str) -> str:
    """Вырезает ключи, токены и пароли, а имя пользователя Windows заменяет на %USERNAME%."""
    out = text or ""
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    home = str(Path.home())
    for variant in {home, home.replace("\\", "/")}:
        if variant and len(variant) > 3:
            out = out.replace(variant, "~")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if len(user) >= 3:
        out = re.sub(rf"(?i)(?<![A-Za-z0-9_]){re.escape(user)}(?![A-Za-z0-9_])", "%USERNAME%", out)
    return out


def safe_url(url: str) -> str:
    """Адрес без логина, пароля и параметров запроса."""
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        return f"{u.scheme}://{host}{':' + str(u.port) if u.port else ''}{u.path}".rstrip("/")
    except ValueError:
        return ""


# --------------------------------------------------------------------------
# Журнал необработанных ошибок
# --------------------------------------------------------------------------

_err_lock = threading.Lock()


def _load_errors() -> List[Dict[str, Any]]:
    try:
        lines = ERRORS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def record_error(source: str, message: str, tb: str = "") -> None:
    """Сохраняет ошибку в errors.jsonl; одинаковые подряд считаются, а не дублируются."""
    message = scrub(message)[:500]
    tb = scrub(tb)[-3000:]
    log.error("[%s] %s", source, message)
    with _err_lock:
        items = _load_errors()
        key = (source, message.splitlines()[0] if message else "")
        for it in items:
            if (it.get("source"), (it.get("message") or "").splitlines()[0] if it.get("message") else "") == key:
                it["count"] = int(it.get("count", 1)) + 1
                it["last"] = datetime.now().isoformat(timespec="seconds")
                if tb:
                    it["tb"] = tb
                break
        else:
            now = datetime.now().isoformat(timespec="seconds")
            items.append({"first": now, "last": now, "source": source, "message": message, "tb": tb, "count": 1})
        items = items[-MAX_ERRORS:]
        try:
            paths.ensure_dirs()
            ERRORS_PATH.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n", encoding="utf-8")
        except OSError:
            log.debug("Не удалось записать errors.jsonl", exc_info=True)


def recent_errors(limit: int = 30) -> List[Dict[str, Any]]:
    return sorted(_load_errors(), key=lambda i: i.get("last", ""), reverse=True)[:limit]


def clear_errors() -> None:
    with _err_lock:
        try:
            ERRORS_PATH.unlink()
        except OSError:
            pass


_hooks_installed = False


def install_hooks() -> None:
    """Любое необработанное исключение (в любом потоке) попадает в журнал ошибок вместо тихого исчезновения."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    prev_hook = sys.excepthook
    prev_thread = threading.excepthook

    def on_main(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            record_error("исключение", f"{exc_type.__name__}: {exc}", "".join(traceback.format_exception(exc_type, exc, tb)))
        prev_hook(exc_type, exc, tb)

    def on_thread(args):
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread else "?"
            record_error(f"поток {name}", f"{args.exc_type.__name__}: {args.exc_value}",
                         "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        prev_thread(args)

    sys.excepthook = on_main
    threading.excepthook = on_thread


# --------------------------------------------------------------------------
# Проверка связи
# --------------------------------------------------------------------------

DEFAULT_TARGETS = {
    "Hugging Face": "https://huggingface.co",
    "Зеркало Hugging Face": "https://hf-mirror.com",
    "Голос Microsoft": "https://speech.platform.bing.com",
    "GitHub": "https://api.github.com",
    "Погода": "https://api.open-meteo.com",
}


def probe(url: str, timeout: float = 6.0) -> Dict[str, Any]:
    """Достучаться до адреса. Любой ответ сервера, даже 404, означает «связь есть»."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": f"Jarvis-Assistant/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001 — любая причина недоступности важна для вывода
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "ms": int((time.monotonic() - t0) * 1000), "error": f"{type(reason).__name__}: {reason}"[:160]}
    return {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "code": code}


def check_network(targets: Optional[Dict[str, str]] = None, timeout: float = 6.0) -> Dict[str, Dict[str, Any]]:
    targets = targets or DEFAULT_TARGETS
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(targets)))) as pool:
        futures = {name: pool.submit(probe, url, timeout) for name, url in targets.items()}
        return {name: dict(fut.result(timeout=timeout + 5), url=safe_url(targets[name])) for name, fut in futures.items()}


# --------------------------------------------------------------------------
# Сбор данных
# --------------------------------------------------------------------------

def _hermes_version(exe: str) -> str:
    try:
        res = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=25, stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return ((res.stdout or res.stderr).strip().splitlines() or [""])[0][:120]
    except Exception as exc:  # noqa: BLE001
        return f"не удалось узнать: {type(exc).__name__}"


def _log_tail(lines: int = 120) -> List[str]:
    try:
        with open(paths.LOG_PATH, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 60_000))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return raw.splitlines()[-lines:]


def gather(service_state: tuple = ("", ""), net: bool = True, hermes_probe: bool = True,
           network: Optional[Callable[[Dict[str, str]], Dict[str, Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Собирает всё, что нужно для выводов. Тяжёлое (сеть, Hermes) запускайте не в потоке окна."""
    from . import backend, llm, models
    from .core import TIMINGS, find_hermes, load_settings

    cfg = load_settings()
    home = backend.hermes_home()
    now = backend.read_hermes_model(home)
    exe = find_hermes(cfg["hermes"].get("exe", "")) or ""
    stt_name = str(cfg["stt"].get("model", "small"))
    cached = models.local_path(stt_name)
    try:
        free_gb = round(shutil.disk_usage(paths.DATA_DIR if paths.DATA_DIR.exists() else Path.home()).free / 1024 ** 3, 1)
    except OSError:
        free_gb = -1.0
    try:
        import sounddevice as sd
        mics = sum(1 for d in sd.query_devices() if d["max_input_channels"] > 0)
        mic_error = ""
    except Exception as exc:  # noqa: BLE001 — нет PortAudio или драйвера
        mics, mic_error = -1, f"{type(exc).__name__}: {exc}"[:160]

    data: Dict[str, Any] = {
        "app": {"version": __version__, "frozen": paths.FROZEN, "python": platform.python_version(),
                "platform": platform.platform(), "encoding": locale.getpreferredencoding(),
                "time": datetime.now().isoformat(timespec="seconds")},
        "paths": {"data": str(paths.DATA_DIR), "log": str(paths.LOG_PATH), "free_gb": free_gb},
        "settings": {"stt": {k: cfg["stt"].get(k) for k in ("model", "device", "compute_type", "mirror")},
                     "tts": {k: cfg["tts"].get(k) for k in ("backend", "voice", "enabled")},
                     "audio_device": cfg["audio"].get("device"),
                     "hermes": {k: cfg["hermes"].get(k) for k in ("provider", "model", "yolo", "timeout_s")}},
        "hermes": {"exe": exe, "version": _hermes_version(exe) if (exe and hermes_probe) else "",
                   "provider": now.get("provider", ""), "model": now.get("model", ""),
                   "base_url": safe_url(now.get("base_url", ""))},
        "engine": {"mode": str(cfg.get("engine", {}).get("mode", "auto")),
                   "native": llm.resolve_endpoint(cfg) is not None,
                   "timings": [{"source": src, "seconds": round(sec, 1), "ok": ok} for _, src, sec, ok in list(TIMINGS)[-20:]]},
        "model_cache": {"name": stt_name, "found": bool(cached), "path": cached or str(models.model_dir(stt_name))},
        "mic": {"count": mics, "error": mic_error},
        "service": {"state": service_state[0], "detail": service_state[1]},
        "timeline": TIMELINE.snapshot(),
        "current_stage": TIMELINE.current(),
        "errors": recent_errors(15),
        "log_tail": _log_tail(),
        "network": {},
    }
    if net:
        targets = dict(DEFAULT_TARGETS)
        if data["hermes"]["base_url"]:
            targets["Сервер модели"] = data["hermes"]["base_url"]
        data["network"] = (network or check_network)(targets)
    return data


# --------------------------------------------------------------------------
# Выводы
# --------------------------------------------------------------------------

@dataclass
class Finding:
    level: str          # error | warn | info | ok
    title: str
    detail: str = ""
    fix: str = ""


def analyze(data: Dict[str, Any]) -> List[Finding]:
    """Превращает собранные данные в выводы. Чистая функция: ни сети, ни файлов."""
    from .core import explain_hermes_error
    out: List[Finding] = []
    net = data.get("network") or {}
    cache = data.get("model_cache") or {}
    hf, mirror = net.get("Hugging Face"), net.get("Зеркало Hugging Face")
    state = (data.get("service") or {}).get("state", "")
    cur = data.get("current_stage")

    # --- вечная загрузка
    if state == "loading" and cur and cur.get("elapsed", 0) > 90:
        stage = cur.get("stage", "?")
        tips = {
            "скачивание модели": "Модель скачивается медленно или не качается. Смотрите строку прогресса на главной; "
                                 "если скорость мала, в разделе «Слух» выберите модель base (около 145 МБ) или укажите зеркало.",
            "загрузка модели в память": "Модель уже скачана, но не загружается в память. Выберите меньшую модель (base) в разделе «Слух».",
            "микрофон": "Не удаётся открыть микрофон. Закройте программы, которые его занимают, и проверьте "
                        "«Параметры → Конфиденциальность → Микрофон».",
        }
        out.append(Finding("error", f"Запуск застрял на этапе «{stage}» уже {int(cur['elapsed'])} с",
                           cur.get("note", ""), tips.get(stage, "Нажмите «Перезапустить». Если повторится, отправьте отчёт.")))
    if state == "error":
        out.append(Finding("error", "Ассистент остановился с ошибкой", (data.get("service") or {}).get("detail", ""),
                           "Нажмите «Перезапустить»; причина обычно указана в строке выше."))

    # --- модель распознавания
    free = (data.get("paths") or {}).get("free_gb", -1)
    if not cache.get("found"):
        size = {"tiny": 75, "base": 145, "small": 484, "medium": 1500}.get(str(cache.get("name")), 500)
        if 0 <= free < size / 1024 * 2:
            out.append(Finding("error", "Мало места на диске для модели речи", f"Свободно {free} ГБ, нужно около {size} МБ.",
                               "Освободите место или выберите модель меньше (base) в разделе «Слух»."))
        if net and hf is not None:
            if not hf["ok"] and mirror and mirror["ok"]:
                out.append(Finding("warn", "Hugging Face недоступен, но зеркало отвечает",
                                   f"Модель «{cache.get('name')}» ещё не скачана. {hf.get('error', '')}",
                                   "Ничего делать не нужно: Джарвис сам переключится на зеркало."))
            elif not hf["ok"] and (not mirror or not mirror["ok"]):
                out.append(Finding("error", "Нет доступа к серверам модели речи",
                                   f"Hugging Face: {hf.get('error', '')}; зеркало: {(mirror or {}).get('error', 'не проверялось')}",
                                   "Проверьте интернет или включите VPN. Либо скачайте папку модели вручную и положите её "
                                   f"сюда: {cache.get('path')}"))
            else:
                out.append(Finding("info", "Модель речи ещё не скачана", f"Будет скачана при запуске (около {size} МБ).",
                                   "Дождитесь окончания загрузки на главной странице."))
        else:
            out.append(Finding("info", "Модель речи ещё не скачана", "", "Она скачается при первом запуске ассистента."))

    # --- Hermes и модель ответов
    h = data.get("hermes") or {}
    if not h.get("exe"):
        out.append(Finding("error", "Hermes не найден", "Без него Джарвис слышит, но не может думать и действовать.",
                           "Установите Hermes Agent или укажите путь к hermes.exe в разделе «Система»."))
    else:
        if str(h.get("version", "")).startswith("не удалось"):
            out.append(Finding("warn", "Hermes не отвечает на проверку версии", h.get("version", ""),
                               "Откройте Hermes отдельно и убедитесь, что он запускается."))
        if not (h.get("model") or (data.get("settings") or {}).get("hermes", {}).get("model")):
            out.append(Finding("warn", "В Hermes не указана модель", "",
                               "Откройте раздел «Подключения» и введите адрес сервера, ключ и название модели."))
    srv = net.get("Сервер модели")
    if srv is not None and not srv["ok"]:
        out.append(Finding("error", "Сервер модели ответов недоступен", f"{srv.get('url', '')}: {srv.get('error', '')}",
                           "Проверьте адрес (Base URL) и интернет. Сайт-поставщик ключа может быть временно недоступен."))

    # --- скорость ответов
    eng = data.get("engine") or {}
    if eng and eng.get("mode") != "hermes" and not eng.get("native") and h.get("exe"):
        out.append(Finding("info", "Отвечает Hermes, а не быстрый встроенный движок",
                           "Для вашего провайдера встроенный движок не настроен (нет адреса, ключа или модели).",
                           "Заполните адрес, ключ и модель в разделе «Подключения»: ответы станут заметно быстрее."))
    slow = sorted(t["seconds"] for t in eng.get("timings", []) if t["source"] in ("hermes", "native") and t["ok"])
    if len(slow) >= 3 and slow[len(slow) // 2] > 15:
        via = "Hermes" if not eng.get("native") else "модель"
        out.append(Finding("warn", f"Ответы медленные: обычно {slow[len(slow) // 2]:.0f} с", f"Отвечает {via}.",
                           "Выберите «Встроенный» в «Подключения → Движок ответов» или модель побыстрее."
                           if eng.get("mode") != "native" else "Выберите модель побыстрее (без режима рассуждений)."))
    lost = sum(1 for t in eng.get("timings", []) if t["source"] == "timeout")
    if lost >= 2:
        out.append(Finding("error", f"Ответ не пришёл вовремя {lost} раз", "Запросы к модели зависают.",
                           "Проверьте баланс и адрес в «Подключения»; при необходимости переключите «Движок ответов»."))

    # --- микрофон и голос
    mic = data.get("mic") or {}
    if mic.get("count") == 0:
        out.append(Finding("error", "Микрофон не найден", "", "Подключите микрофон и проверьте «Конфиденциальность → Микрофон»."))
    elif mic.get("count", 0) < 0 and mic.get("error"):
        out.append(Finding("warn", "Звуковая библиотека недоступна", mic["error"], "Переустановите Джарвиса."))
    tts = net.get("Голос Microsoft")
    if tts is not None and not tts["ok"]:
        out.append(Finding("warn", "Голос Microsoft недоступен", tts.get("error", ""),
                           "Джарвис говорит запасным голосом Windows, пока нет связи."))
    gh = net.get("GitHub")
    if gh is not None and not gh["ok"]:
        out.append(Finding("info", "GitHub недоступен", gh.get("error", ""), "Проверка обновлений не сработает."))

    # --- накопленные ошибки
    for err in (data.get("errors") or [])[:5]:
        msg = (err.get("message") or "").splitlines()[0] if err.get("message") else ""
        known = explain_hermes_error(err.get("message", ""))
        out.append(Finding("warn", f"Ошибка ×{err.get('count', 1)}: {msg[:110]}", f"{err.get('source', '')}; последний раз {err.get('last', '')}",
                           known or "Если повторяется, отправьте отчёт."))

    if not any(f.level in ("error", "warn") for f in out):
        out.append(Finding("ok", "Проблем не найдено", "Все проверенные части работают."))
    return sorted(out, key=lambda f: LEVEL_ORDER[f.level])


# --------------------------------------------------------------------------
# Отчёт
# --------------------------------------------------------------------------

def render_text(data: Dict[str, Any], findings: List[Finding]) -> str:
    """Отчёт для человека и для разработчика. Секреты вырезаны."""
    a, p, h = data["app"], data["paths"], data["hermes"]
    mark = {"error": "ОШИБКА", "warn": "ВНИМАНИЕ", "info": "ИНФО", "ok": "ОК"}
    lines = [f"Джарвис {a['version']} ({'установленный' if a['frozen'] else 'из исходников'}), {a['platform']}, "
             f"Python {a['python']}, кодировка {a['encoding']}, {a['time']}", ""]
    lines.append("ВЫВОДЫ")
    for f in findings:
        lines.append(f"[{mark[f.level]}] {f.title}")
        if f.detail:
            lines.append(f"    {f.detail}")
        if f.fix:
            lines.append(f"    → {f.fix}")
    lines += ["", "СОСТОЯНИЕ",
              f"  ассистент: {data['service']['state'] or '—'} {data['service']['detail']}",
              f"  Hermes: {h['exe'] or 'не найден'} {h['version']}; провайдер={h['provider'] or '—'}, модель={h['model'] or '—'}, "
              f"адрес={h['base_url'] or '—'}",
              f"  движок ответов: {(data.get('engine') or {}).get('mode', '—')}, встроенный готов: "
              f"{'да' if (data.get('engine') or {}).get('native') else 'нет'}; последние ответы, с: "
              f"{[(t['source'], t['seconds']) for t in (data.get('engine') or {}).get('timings', [])[-8:]]}",
              f"  модель речи: {data['model_cache']['name']}, на компьютере: {'да' if data['model_cache']['found'] else 'нет'}",
              f"  микрофонов: {data['mic']['count']} {data['mic']['error']}",
              f"  свободно на диске: {p['free_gb']} ГБ; данные: {p['data']}",
              f"  настройки: {json.dumps(data['settings'], ensure_ascii=False)}"]
    if data["network"]:
        lines += ["", "СВЯЗЬ"]
        for name, r in data["network"].items():
            lines.append(f"  {name}: {'есть, ' + str(r['ms']) + ' мс' if r['ok'] else 'НЕТ — ' + r.get('error', '')}")
    if data["timeline"]:
        lines += ["", "ХРОНОЛОГИЯ ЗАПУСКА"]
        for t in data["timeline"][-15:]:
            lines.append(f"  {t['stage']}: {t['seconds']} с{' (идёт)' if t['running'] else ''} {t['note']}")
    if data["errors"]:
        lines += ["", "ОШИБКИ"]
        for e in data["errors"][:8]:
            lines.append(f"  ×{e.get('count', 1)} [{e.get('source', '')}] {e.get('message', '')[:200]}")
            if e.get("tb"):
                lines += ["      " + ln for ln in e["tb"].splitlines()[-6:]]
    if data["log_tail"]:
        lines += ["", "ЖУРНАЛ (последние строки)"] + ["  " + ln for ln in data["log_tail"][-60:]]
    return scrub("\n".join(lines))


def issue_url(findings: List[Finding], report: str) -> str:
    """Адрес новой задачи в репозитории с готовым заголовком и коротким описанием.

    Полный отчёт слишком велик для адреса, поэтому он копируется в буфер обмена, а в задачу вставляется целиком."""
    worst = next((f for f in findings if f.level in ("error", "warn")), findings[0] if findings else None)
    title = f"Отчёт: {worst.title}" if worst else "Отчёт Джарвиса"
    short = "\n".join(f"- {f.title}" for f in findings[:6])
    body = (f"Версия: {__version__}\n\nВыводы диагностики:\n{short}\n\n"
            "Полный отчёт: вставьте сюда из буфера обмена (Ctrl+V).\n")
    return f"https://github.com/{REPO}/issues/new?" + urllib.parse.urlencode({"title": title[:120], "body": body})
