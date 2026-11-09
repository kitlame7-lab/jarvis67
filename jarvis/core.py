# -*- coding: utf-8 -*-
"""
Ядро Джарвиса: настройки, распознавание слова вызова, связь с Hermes,
распознавание и синтез речи, микрофон, диалог.

Цикл работы:
    микрофон -> Whisper (локально) -> слово «Джарвис» -> агент Hermes ->
    ответ голосом Microsoft

Мозг — Hermes Agent: приложение вызывает `hermes chat --oneshot` и передаёт
ему просьбу вместе с коротким описанием характера. Инструменты управления ПК
оформлены как навык `jarvis-pc`.
"""

from __future__ import annotations

import asyncio
import ctypes
import difflib
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import agent as agent_mod
from . import models, paths
from .paths import IS_WIN, LOG_PATH, ensure_dirs

# Приложение без консоли: stdout/stderr могут отсутствовать
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

log = logging.getLogger("jarvis")


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "name": "Джарвис",
    "address": "сэр",
    "city": "Москва",
    "greeting": True,
    "wake": {
        # Первые два слова — основное имя: с ними сравнивается нечётко.
        # Остальное — известные искажения Whisper, они совпадают только целиком.
        "words": ["джарвис", "jarvis", "жарвис", "шарвис", "джарвиз", "жарвиз",
                  "джарис", "джервис", "джервиз", "дарвис", "гарвис", "джарвес",
                  "чарвис", "чарвиз", "дярвис"],
        "fuzzy_ratio": 0.84,
        "followup_seconds": 12.0,
    },
    "audio": {
        "device": "",
        "samplerate": 16000,
        "block_ms": 30,
        "silence_threshold": 0.012,
        "auto_threshold": True,
        "silence_tail_ms": 600,      # пауза, после которой фраза считается законченной
        "early_ms": 300,             # распознавание начинается уже после такой паузы (если человек продолжит — заново)
        "barge_in": "voice",         # voice — перебить можно любой фразой; wake — только по имени; off — нельзя
        "barge_boost": 2.5,          # во время своей речи порог громкости выше: колонки не должны перебивать Джарвиса
        "min_speech_ms": 350,
        "max_utterance_s": 25.0,
        "preroll_ms": 300,
    },
    "stt": {
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "language": "ru",
        "beam_size": 1,
        "initial_prompt": "Джарвис, ",
        "mirror": "",                # свой адрес Hugging Face (например зеркало), пробуется первым
    },
    "tts": {
        "enabled": True,
        "backend": "auto",           # auto = голос Microsoft, при сбое голос Windows
        "voice": "ru-RU-DmitryNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "max_chars": 600,
    },
    "hermes": {
        "exe": "",
        "provider": "",   # пусто = как настроено в самом Hermes
        "model": "",
        "skills": "jarvis-pc",
        "timeout_s": 120,
        "ack_after_s": 3.0,
        "yolo": True,
        "extra_args": [],
    },
    "teach": {
        "enabled": True,             # запись действий и повтор задач
        "max_minutes": 10,           # запись сама остановится
        "shots": True,               # снимки экрана при щелчках (видны на странице «Задачи»)
        "max_shots": 60,
    },
    "engine": {
        "mode": "auto",              # auto: встроенный движок, если модель настроена; native; hermes
        "llm_timeout_s": 40,         # ожидание одного ответа модели
        "max_steps": 5,              # сколько раз подряд модель может запросить команды
        "deadline_s": 100,           # общий предел на одну просьбу
    },
    "notes": {"dir": ""},            # пусто — «Документы\\Заметки Джарвиса»
    "ui": {"theme": "light"},        # light | dark
    "updates": {"auto_check": True},
    "history_turns": 6,
    "history_ttl_s": 600,
}


def deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    path = Path(path) if path else paths.SETTINGS_PATH
    cfg = json.loads(json.dumps(DEFAULTS))
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.error("Не удалось прочитать %s: %s. Использую настройки по умолчанию.", path, exc)
            return cfg
        cfg = deep_merge(cfg, user)
    return cfg


def setup_logging(console: bool = False) -> None:
    ensure_dirs()
    if getattr(log, "_jarvis_ready", False):
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    try:
        fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log._jarvis_ready = True  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Текст: обращение, фильтры, чистка для озвучки
# --------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Вид, удобный для СРАВНЕНИЯ (й->и, ё->е). Для передачи агенту не годится."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))   # й->и, ё->е без лишних пробелов
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class WakeResult:
    triggered: bool
    command: str
    matched: str = ""


def detect_wake(text: str, words: List[str], ratio: float,
                fuzzy_words: Optional[List[str]] = None) -> WakeResult:
    """Ищет обращение в первых трёх словах и возвращает хвост ИСХОДНОЙ фразы.

    `words` совпадают только целиком (список известных написаний). Нечёткое
    сравнение идёт лишь с `fuzzy_words` (по умолчанию — первые два слова
    списка, то есть основное имя), иначе «сервис» и «дарвин» принимались бы
    за обращение.
    """
    spans = [(m.group(0), m.end()) for m in re.finditer(r"\w+", text, flags=re.UNICODE)]
    if not spans:
        return WakeResult(False, "")
    exact = [normalize(w) for w in words]
    fuzzy = [normalize(w) for w in (fuzzy_words if fuzzy_words is not None else words[:2])]

    def tail(end: int) -> str:
        return text[end:].lstrip(" ,.!?:;—-\t\n").strip()

    for raw, end in spans[:3]:
        token = normalize(raw)
        if token in exact:
            return WakeResult(True, tail(end), token)
    for raw, end in spans[:3]:
        token = normalize(raw)
        if len(token) >= 5 and any(
                difflib.SequenceMatcher(None, token, w).ratio() >= ratio for w in fuzzy):
            return WakeResult(True, tail(end), token)
    return WakeResult(False, "")


# Whisper на тишине и шуме любит «досочинять» такие фразы
HALLUCINATIONS = (
    "субтитры", "продолжение следует", "спасибо за просмотр", "редактор субтитров",
    "корректор", "dimatorzok", "подписывайтесь", "amara.org", "thanks for watching",
)


def is_hallucination(text: str) -> bool:
    low = text.lower()
    if any(h in low for h in HALLUCINATIONS):
        return True
    words = normalize(text).split()
    return len(words) >= 4 and len(set(words)) == 1


def clean_for_speech(text: str, limit: int = 600) -> str:
    """Убирает разметку, ссылки и код, режет длинное по границе предложения."""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    text = re.sub(r"```.*?```", " фрагмент кода ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "ссылка", text)
    text = re.sub(r"[*_`#>|~]+", "", text)
    text = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:dot + 1] if dot > limit * 0.5 else cut) + " Остальное смотрите в журнале."


# --------------------------------------------------------------------------
# Связь с Hermes
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Известные сбои модели. Hermes печатает их по-английски, а читать вслух
# английскую простыню бессмысленно: заменяем короткой русской фразой.
HERMES_ERRORS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"not connected to any AI provider", re.I),
     "Hermes не подключён к модели. Откройте раздел «Модель» в настройках Джарвиса."),
    (re.compile(r"No usable credentials found|Set \w+_API_KEY|API key (is )?(missing|not set)", re.I),
     "Для выбранной модели не задан ключ. Откройте раздел «Модель» и введите адрес сервера, ключ и название модели."),
    (re.compile(r"model.{0,40}(not found|does not exist|not supported)|unknown model|HTTP 404", re.I),
     "Сервер не знает такую модель. Проверьте название в разделе «Модель»: кнопка «Показать модели сервера» подскажет."),
    (re.compile(r"APIConnectionError|Connection error|getaddrinfo failed|Name or service not known|Failed to resolve", re.I),
     "Не удалось связаться с сервером модели. Проверьте интернет и Base URL в разделе «Модель»."),
    (re.compile(r"rejected your API key|Authentication Fails|HTTP 401|invalid.{0,20}api key", re.I),
     "Модель не приняла ключ доступа. Обновите ключ в разделе «Модель» настроек Джарвиса."),
    (re.compile(r"HTTP 402|insufficient balance|insufficient.{0,20}quota|billing", re.I),
     "На счёте модели закончились средства."),
    (re.compile(r"HTTP 429|rate.?limit|too many requests", re.I),
     "Модель сейчас перегружена. Повторите чуть позже."),
]


def explain_hermes_error(text: str) -> Optional[str]:
    """Русское объяснение известного сбоя Hermes или None."""
    for pattern, message in HERMES_ERRORS:
        if pattern.search(text or ""):
            return message
    return None


def find_hermes(configured: str = "") -> Optional[str]:
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("hermes")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = Path(local) / "hermes" / "bin" / "hermes.exe"
        if cand.exists():
            return str(cand)
    return None


def kill_tree(proc: "subprocess.Popen") -> None:
    """Завершает процесс вместе со всеми потомками и забирает его, чтобы не оставалось зомби."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(proc.pid, signal.SIGKILL)       # процесс запущен в своей группе (start_new_session)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


class Hermes:
    """Вызывает `hermes chat --oneshot` и возвращает текст ответа."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.exe = find_hermes(cfg.get("exe", ""))

    def build_argv(self, query_file: str) -> List[str]:
        argv = [self.exe or "hermes", "chat", "--oneshot", "--quiet",
                "--source", "tool", "--query-file", query_file]
        if self.cfg.get("provider"):
            argv += ["--provider", str(self.cfg["provider"])]
        if self.cfg.get("model"):
            argv += ["-m", str(self.cfg["model"])]
        if self.cfg.get("skills"):
            argv += ["--skills", self.cfg["skills"]]
        if self.cfg.get("yolo"):
            argv.append("--yolo")
        argv += [str(a) for a in self.cfg.get("extra_args", [])]
        return argv

    def ask(self, prompt: str) -> str:
        return self.run(prompt)[1]

    def run(self, prompt: str) -> Tuple[bool, str]:
        """Возвращает (получилось ли, текст ответа или понятное объяснение сбоя)."""
        if not self.exe:
            return False, "Не нашёл Hermes. Проверьте установку."
        # Запрос передаётся файлом: так не мешают кавычки, переводы строк и длина
        fd, path = tempfile.mkstemp(prefix="jarvis-q-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(prompt)
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            child = subprocess.Popen(
                self.build_argv(path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", env=env, stdin=subprocess.DEVNULL, creationflags=flags,
                start_new_session=not IS_WIN)
            try:
                stdout, stderr = child.communicate(timeout=float(self.cfg.get("timeout_s", 120)))
            except subprocess.TimeoutExpired:
                # Зависший Hermes тащит за собой свои дочерние процессы: убиваем всё дерево, иначе они держат файлы
                # и следующая просьба тоже зависает
                kill_tree(child)
                return False, "Hermes не ответил вовремя и был остановлен. Для быстрых ответов включите встроенный движок в разделе «Подключения»."
            proc = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
        except OSError as exc:
            log.error("Не удалось запустить Hermes: %s", exc)
            return False, "Не удалось запустить Hermes."
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        out = ANSI_RE.sub("", proc.stdout or "").strip()
        err_text = ANSI_RE.sub("", proc.stderr or "").strip()
        known = explain_hermes_error(out) or explain_hermes_error(err_text)
        if known:
            log.error("Сбой модели: %s", (out or err_text)[:300])
            return False, known
        if proc.returncode != 0 and not out:
            log.error("Hermes завершился с кодом %s: %s", proc.returncode, err_text.splitlines()[-3:])
            return False, "Hermes не смог ответить. Проверьте модель и ключ в разделе «Модель»."
        if not out:
            return False, "Hermes вернул пустой ответ."
        return True, out


# --------------------------------------------------------------------------
# Речь в текст
# --------------------------------------------------------------------------

class Transcriber:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()        # голос с микрофона и голосовые из Telegram распознаются по очереди

    def load(self, on_progress: Optional[Callable[["models.Progress"], None]] = None,
             cancel: Optional[threading.Event] = None) -> None:
        """Скачивает модель при необходимости (с прогрессом и отменой) и загружает её в память."""
        if self._model is not None:
            return
        notify = on_progress or (lambda _p: None)
        endpoints = [e for e in [self.cfg.get("mirror", "").strip()] if e] + models.DEFAULT_ENDPOINTS
        log.info("Готовлю модель распознавания %s (%s)", self.cfg["model"], self.cfg["device"])
        path = models.fetch(self.cfg["model"], notify, cancel or threading.Event(), endpoints=endpoints)
        notify(models.Progress("loading"))
        from faster_whisper import WhisperModel
        t0 = time.monotonic()
        self._model = WhisperModel(path, device=self.cfg["device"], compute_type=self.cfg["compute_type"])
        log.info("Модель готова за %.1f с", time.monotonic() - t0)

    def transcribe(self, audio) -> str:
        self.load()
        with self._lock:
            t0 = time.monotonic()
            segments, _ = self._model.transcribe(
                audio,
                language=self.cfg.get("language") or None,
                beam_size=int(self.cfg.get("beam_size", 1)),
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
                without_timestamps=True,          # метки времени не нужны, а без них быстрее
                initial_prompt=self.cfg.get("initial_prompt") or None,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        log.debug("Распознано за %.2f с", time.monotonic() - t0)
        return text



# --------------------------------------------------------------------------
# Озвучка: голоса Microsoft
# --------------------------------------------------------------------------
# Нейроголоса Microsoft Edge (онлайн, бесплатно) с запасным голосом Windows.
# Русский язык есть у обоих вариантов.

FALLBACK_VOICES = [
    {"name": "ru-RU-DmitryNeural", "gender": "Male", "label": "Дмитрий (мужской)"},
    {"name": "ru-RU-SvetlanaNeural", "gender": "Female", "label": "Светлана (женский)"},
]


def list_voices(locale_prefix: str = "ru-") -> List[Dict[str, str]]:
    """Русские нейроголоса Microsoft. Без сети возвращает известные запасные."""
    try:
        import edge_tts
        raw = asyncio.run(asyncio.wait_for(edge_tts.list_voices(), timeout=12))
        voices = []
        for v in raw:
            if str(v.get("Locale", "")).lower().startswith(locale_prefix.lower()):
                short = v.get("ShortName", "")
                nice = short.split("-")[-1].replace("Neural", "")
                gender = "мужской" if v.get("Gender") == "Male" else "женский"
                voices.append({"name": short, "gender": v.get("Gender", ""), "label": f"{nice} ({gender})"})
        if voices:
            return sorted(voices, key=lambda x: x["name"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить список голосов: %s", exc)
    return list(FALLBACK_VOICES)


def play_mp3(path: str, slack_s: float = 5.0, cancel: Optional[threading.Event] = None) -> None:
    """Проигрывает mp3 средствами Windows (MCI), без ffmpeg и плееров.

    Раньше стояло `play … wait`: при сбое звукового драйвера вызов не возвращался, микрофон оставался заглушённым,
    а ассистент молчал и «висел». Теперь ждём не дольше длины записи плюс запас."""
    mci = ctypes.windll.winmm.mciSendStringW  # type: ignore[attr-defined]
    alias = f"jarvis{os.getpid()}{threading.get_ident()}"
    buf = ctypes.create_unicode_buffer(128)
    err = mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
    if err:
        raise RuntimeError(f"не удалось открыть звук (код MCI {err})")
    try:
        mci(f"status {alias} length", buf, 127, None)
        length_s = int(buf.value or 0) / 1000.0 if (buf.value or "").isdigit() else 30.0
        err = mci(f"play {alias}", None, 0, None)
        if err:
            raise RuntimeError(f"не удалось воспроизвести звук (код MCI {err})")
        deadline = time.monotonic() + length_s + slack_s
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return
            mci(f"status {alias} mode", buf, 127, None)
            if buf.value == "stopped":
                return
            time.sleep(0.1)
        log.warning("Воспроизведение не закончилось за %.0f с, обрываю", length_s + slack_s)
    finally:
        mci(f"stop {alias}", None, 0, None)
        mci(f"close {alias}", None, 0, None)


def play_wav_windows(pcm: Any, rate: int, cancel: threading.Event) -> bool:
    """Запасное воспроизведение через winsound: асинхронно, с остановкой по флагу."""
    import wave
    import winsound  # type: ignore[import-not-found]
    import numpy as np
    fd, path = tempfile.mkstemp(prefix="jarvis-say-", suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(rate))
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        end = time.monotonic() + len(pcm) / float(rate) + 0.3
        while time.monotonic() < end:
            if cancel.is_set():
                winsound.PlaySound(None, 0)
                return False
            time.sleep(0.03)
        return True
    finally:
        time.sleep(0.05)
        try:
            os.unlink(path)
        except OSError:
            pass


SAPI_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'ru*' } | Select-Object -First 1;"
    "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };"
    "if ($env:JARVIS_WAV) { $s.SetOutputToWaveFile($env:JARVIS_WAV) };"
    "$s.Speak($env:JARVIS_SAY)"
)


class Speaker:
    """Голос Джарвиса. Фразы синтезируются и играются по одной: следующая готовится, пока звучит текущая,
    а любая может быть оборвана мгновенно (перебивание, «стоп»)."""
    COOLDOWN_S = 600.0

    def __init__(self, cfg: Dict[str, Any], player: Any = None):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self._down_until = 0.0
        self._choice: Optional[Tuple[str, Optional[str], Optional[str]]] = None
        self.cancel = threading.Event()      # «Остановить» обрывает речь сразу, а не после последнего слова
        self._player = player
        self.audible = IS_WIN or player is not None
        self.spoken: "deque[Tuple[float, str]]" = deque(maxlen=16)   # что и когда начали говорить (для эхо-фильтра)

    # --- проигрыватель --------------------------------------------------
    @property
    def player(self) -> Any:
        if self._player is None:
            from .audio import Player
            self._player = Player()
        return self._player

    @property
    def playing(self) -> bool:
        return bool(self._player is not None and self._player.playing)

    def interrupt(self) -> None:
        """Обрывает текущую фразу (сессии сами решают, говорить ли дальше)."""
        if self._player is not None:
            self._player.stop()

    def duck(self) -> None:
        if self._player is not None:
            self._player.duck()

    def unduck(self) -> None:
        if self._player is not None:
            self._player.unduck()

    def recent_text(self, seconds: float = 10.0) -> str:
        now = time.monotonic()
        return " ".join(t for at, t in list(self.spoken) if now - at < seconds)

    # --- синтез ---------------------------------------------------------
    def attempts(self) -> List[Tuple[str, Optional[str], Optional[str]]]:
        """Варианты (голос, скорость, высота) от настроенного к самым простым.
        Edge иногда отвечает «No audio was received» на необычные параметры."""
        voice = self.cfg["voice"]
        spare = "ru-RU-SvetlanaNeural" if voice != "ru-RU-SvetlanaNeural" else "ru-RU-DmitryNeural"
        attempts = [(voice, self.cfg.get("rate"), self.cfg.get("pitch")), (voice, None, None), (spare, None, None)]
        if self._choice in attempts:
            attempts.remove(self._choice)
            attempts.insert(0, self._choice)
        return attempts

    async def _save(self, text: str, path: str, voice: str, rate: Optional[str], pitch: Optional[str]) -> None:
        import edge_tts
        kwargs: Dict[str, str] = {}
        if rate and rate not in ("+0%", "0%"):
            kwargs["rate"] = rate
        if pitch and pitch not in ("+0Hz", "0Hz"):
            kwargs["pitch"] = pitch
        await edge_tts.Communicate(text, voice, **kwargs).save(path)

    def synth(self, text: str, path: str) -> None:
        """Синтез в mp3 с перебором вариантов. Бросает последнюю ошибку."""
        last: Optional[BaseException] = None
        for voice, rate, pitch in self.attempts():
            try:
                asyncio.run(asyncio.wait_for(self._save(text, path, voice, rate, pitch), timeout=15))
                if Path(path).stat().st_size > 500:
                    if self._choice != (voice, rate, pitch):
                        log.info("Голос Microsoft работает: %s", voice)
                    self._choice = (voice, rate, pitch)
                    return
                last = RuntimeError("пустой звуковой файл")
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("Голос Microsoft (%s, rate=%s, pitch=%s): %s", voice, rate, pitch, exc)
        raise last or RuntimeError("голос Microsoft недоступен")

    def synth_bytes(self, text: str) -> bytes:
        fd, path = tempfile.mkstemp(prefix="jarvis-tts-", suffix=".mp3")
        os.close(fd)
        try:
            self.synth(text, path)
            return Path(path).read_bytes()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _sapi_wav(self, text: str) -> bytes:
        fd, path = tempfile.mkstemp(prefix="jarvis-sapi-", suffix=".wav")
        os.close(fd)
        try:
            env = dict(os.environ, JARVIS_SAY=text, JARVIS_WAV=path)
            subprocess.run(["powershell", "-NoProfile", "-Command", SAPI_SCRIPT], env=env, capture_output=True,
                           timeout=60, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = Path(path).read_bytes()
            if len(data) < 100:
                raise RuntimeError("голос Windows не создал звук")
            return data
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def render(self, text: str) -> Optional[Tuple[Any, int]]:
        """Фраза в звук (PCM): голос Microsoft, при сбое голос Windows. None — говорить нечем."""
        from .audio import decode_audio
        backend = self.cfg.get("backend", "auto")
        if backend in ("auto", "edge", "neural") and time.time() >= self._down_until:
            try:
                return decode_audio(self.synth_bytes(text))
            except Exception as exc:  # noqa: BLE001 — сеть или звук: уходим на запасной голос
                self._down_until = time.time() + self.COOLDOWN_S
                log.warning("Голос Microsoft недоступен (%s). Говорю голосом Windows, следующая попытка через 10 минут", exc)
        if not IS_WIN:
            return None
        try:
            return decode_audio(self._sapi_wav(text))
        except Exception as exc:  # noqa: BLE001
            log.error("Озвучить не удалось: %s", exc)
            return None

    def play_pcm(self, pcm: Any, rate: int, cancel: Optional[threading.Event] = None) -> bool:
        try:
            return self.player.play(pcm, rate, cancel)
        except Exception as exc:  # noqa: BLE001 — нет устройства вывода у sounddevice: играем средствами Windows
            if not IS_WIN:
                raise
            log.warning("Проигрыватель недоступен (%s), играю через Windows", exc)
            return play_wav_windows(pcm, rate, cancel or self.cancel)

    # --- старый путь «сказать целиком» ------------------------------------
    def _say_neural(self, text: str) -> None:
        from .audio import decode_audio
        data = self.synth_bytes(text)
        self.spoken.append((time.monotonic(), text))
        try:
            pcm, rate = decode_audio(data)
            self.play_pcm(pcm, rate, self.cancel)
        except Exception as exc:  # noqa: BLE001 — нет устройства вывода или декодера: проигрываем как раньше
            log.warning("Проигрыватель недоступен (%s), играю через Windows MCI", exc)
            fd, path = tempfile.mkstemp(prefix="jarvis-tts-", suffix=".mp3")
            os.close(fd)
            try:
                Path(path).write_bytes(data)
                play_mp3(path, cancel=self.cancel)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _say_sapi(self, text: str) -> None:
        self.spoken.append((time.monotonic(), text))
        env = dict(os.environ, JARVIS_SAY=text)
        subprocess.run(["powershell", "-NoProfile", "-Command", SAPI_SCRIPT], env=env,
                       capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def say(self, text: str) -> None:
        speech = clean_for_speech(text, int(self.cfg.get("max_chars", 600)))
        log.info("Джарвис: %s", speech)
        if self.cancel.is_set() or not self.enabled or not speech or not IS_WIN:
            return
        backend = self.cfg.get("backend", "auto")
        if backend in ("auto", "edge", "neural") and time.time() >= self._down_until:
            try:
                self._say_neural(speech)
                return
            except Exception as exc:  # noqa: BLE001 — сеть или звук: уходим на запасной голос
                self._down_until = time.time() + self.COOLDOWN_S
                log.warning("Голос Microsoft недоступен (%s). Говорю голосом Windows, "
                            "следующая попытка через 10 минут", exc)
        try:
            self._say_sapi(speech)
        except Exception as exc:  # noqa: BLE001
            log.error("Озвучить не удалось: %s", exc)

    def session(self, cancel: Optional[threading.Event] = None) -> "SpeechSession":
        return SpeechSession(self, cancel or threading.Event())


class SpeechSession:
    """Речь одного ответа: фразы приходят по мере написания моделью и звучат без пауз между ними.

    Синтез фразы N+1 идёт, пока звучит фраза N. stop() обрывает всё сразу."""

    def __init__(self, speaker: Speaker, cancel: threading.Event):
        self.sp, self.cancel = speaker, cancel
        self.texts: "queue.Queue[Optional[str]]" = queue.Queue()
        self.sounds: "queue.Queue[Optional[Tuple[str, Any]]]" = queue.Queue(maxsize=2)
        self.started = threading.Event()        # первая фраза зазвучала (или озвучка выключена)
        self.finished = threading.Event()
        self.said: List[str] = []
        self.pushed = 0
        self._closed = False
        threading.Thread(target=self._synth_loop, name="jarvis-tts", daemon=True).start()
        threading.Thread(target=self._play_loop, name="jarvis-play", daemon=True).start()

    def _stopped(self) -> bool:
        return self.cancel.is_set() or self.sp.cancel.is_set()

    def push(self, text: str) -> None:
        speech = clean_for_speech(text, int(self.sp.cfg.get("max_chars", 600)))
        if speech and not self._closed:
            log.info("Джарвис: %s", speech)
            self.pushed += 1
            self.texts.put(speech)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.texts.put(None)

    def stop(self) -> None:
        self.cancel.set()
        self.sp.interrupt()
        self.close()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Ждёт конца речи. False — оборвали или вышло время."""
        end = None if timeout is None else time.monotonic() + timeout
        while not self.finished.wait(0.05):
            if self._stopped():
                return False
            if end is not None and time.monotonic() > end:
                return False
        return not self._stopped()

    def _put_sound(self, item: Optional[Tuple[str, Any]]) -> bool:
        while not self._stopped():
            try:
                self.sounds.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _synth_loop(self) -> None:
        try:
            while not self._stopped():
                try:
                    text = self.texts.get(timeout=0.1)
                except queue.Empty:
                    continue
                if text is None:
                    break
                sound = self.sp.render(text) if (self.sp.enabled and self.sp.audible) else None
                if not self._put_sound((text, sound)):
                    return
        except Exception:  # noqa: BLE001 — сбой синтеза не должен оставить ответ без конца
            log.exception("Сбой синтеза речи")
        finally:
            self._put_sound(None)

    def _play_loop(self) -> None:
        try:
            while not self._stopped():
                try:
                    item = self.sounds.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    break
                text, sound = item
                self.sp.spoken.append((time.monotonic(), text))
                self.said.append(text)
                self.started.set()
                if sound is None:
                    continue
                try:
                    self.sp.play_pcm(sound[0], sound[1], self.cancel)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Проигрыватель недоступен: %s", exc)
        finally:
            self.started.set()
            self.finished.set()


# --------------------------------------------------------------------------
# Микрофон
# --------------------------------------------------------------------------

class NullRecorder:
    """Заглушка для текстового режима."""
    def mute(self) -> None: ...
    def unmute(self) -> None: ...


class Recorder:
    """Захват звука и нарезка на фразы по паузам. Пока ассистент говорит или
    думает, микрофон «заглушён», чтобы он не слушал собственный голос."""

    def __init__(self, cfg: Dict[str, Any]):
        import numpy as np
        import sounddevice as sd
        self.np, self.sd, self.cfg = np, sd, cfg
        self.q: "queue.Queue[Any]" = queue.Queue()
        self._mutes = 0
        self._lock = threading.Lock()
        self.paused = False
        self.stop = threading.Event()
        self._stream = None
        # Во сколько раз поднять порог громкости прямо сейчас (Джарвис говорит в колонки). Подключает ассистент.
        self.boost: Callable[[], float] = lambda: 1.0

    def device(self):
        raw = self.cfg.get("device")
        if raw in (None, ""):
            return None
        return int(raw) if isinstance(raw, str) and raw.isdigit() else raw

    def _block(self) -> int:
        return int(int(self.cfg["samplerate"]) * int(self.cfg["block_ms"]) / 1000)

    def open(self) -> None:
        def callback(indata, _frames, _time, _status):
            if self._mutes == 0 and not self.paused:
                self.q.put(indata[:, 0].copy())
        self._stream = self.sd.InputStream(
            samplerate=int(self.cfg["samplerate"]), channels=1, dtype="float32",
            blocksize=self._block(), device=self.device(), callback=callback)
        self._stream.start()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _flush(self) -> None:
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def mute(self) -> None:
        with self._lock:
            self._mutes += 1
        self._flush()

    def unmute(self) -> None:
        with self._lock:
            self._mutes = max(0, self._mutes - 1)
            if self._mutes == 0:
                self._flush()

    def calibrate(self, seconds: float = 1.2) -> float:
        """Замеряет фоновый шум и возвращает рекомендуемый порог тишины."""
        deadline = time.time() + seconds
        levels = []
        self._flush()
        while time.time() < deadline:
            try:
                chunk = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            levels.append(float(self.np.sqrt(self.np.mean(self.np.square(chunk)))))
        if not levels:
            return float(self.cfg["silence_threshold"])
        ambient = sorted(levels)[int(len(levels) * 0.8) - 1 if len(levels) > 4 else -1]
        return min(0.08, max(float(self.cfg["silence_threshold"]), ambient * 3.5))

    def utterances(self):
        """Выдаёт по одной законченной фразе (numpy float32)."""
        for kind, audio in self.segments():
            if kind == "final":
                yield audio

    def segments(self):
        """События речи: ("start", None) — человек заговорил; ("voice", None) — говорит дольше 0,35 с; ("draft", звук) — короткая пауза, можно начинать
        распознавать; ("final", звук) — фраза закончена; ("drop", None) — это был короткий шум."""
        np = self.np
        ms = int(self.cfg["block_ms"])
        base = float(self.cfg["silence_threshold"])
        tail_blocks = max(1, int(self.cfg["silence_tail_ms"] / ms))
        early_blocks = max(1, int(self.cfg.get("early_ms", 300) / ms))
        min_blocks = int(self.cfg["min_speech_ms"] / ms)
        max_blocks = int(float(self.cfg["max_utterance_s"]) * 1000 / ms)
        preroll = int(self.cfg["preroll_ms"] / ms)

        keep = max(1, int(200 / ms))           # после речи оставляем 200 мс тишины, остальное распознавать незачем
        ring: List[Any] = []
        collected: List[Any] = []
        speaking = False
        silent_run = voiced = 0
        drafted = False
        while not self.stop.is_set():
            try:
                chunk = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            level = float(np.sqrt(np.mean(np.square(chunk))))
            threshold = base * max(1.0, float(self.boost()))
            if not speaking:
                ring.append(chunk)
                if len(ring) > preroll:
                    ring.pop(0)
                if level >= threshold:
                    speaking, collected, ring, silent_run, voiced, drafted = True, list(ring) + [chunk], [], 0, 1, False
                    yield "start", None
                continue
            collected.append(chunk)
            if level < threshold:
                silent_run += 1
            else:
                silent_run, drafted, voiced = 0, False, voiced + 1
                if voiced == min_blocks:
                    yield "voice", None              # это уже не щелчок, а речь: можно приглушить свой голос
            if silent_run == early_blocks and not drafted and voiced >= min_blocks and early_blocks < tail_blocks:
                drafted = True
                yield "draft", np.concatenate(collected[:len(collected) - silent_run + keep])
            if silent_run >= tail_blocks or len(collected) >= max_blocks:
                cut = len(collected) - silent_run + keep if silent_run >= tail_blocks else len(collected)
                audio = np.concatenate(collected[:cut])
                enough = voiced >= min_blocks
                speaking, collected, silent_run, voiced = False, [], 0, 0
                if enough:
                    yield "final", audio
                else:
                    yield "drop", None


# --------------------------------------------------------------------------
# Ассистент
# --------------------------------------------------------------------------

PERSONA = (
    "Ты — {name}, персональный ИИ-ассистент. Ты работаешь на компьютере хозяина и "
    "являешься его правой рукой во всём: от управления файлами до аналитики и бесед.\n\n"
    "ХАРАКТЕР: ты умён, находчив, слегка ироничен, но всегда уважителен. Ты не просто "
    "выполняешь команды — ты думаешь наперёд, предлагаешь решения и замечаешь важное. "
    "У тебя есть чувство юмора: уместная шутка или лёгкий сарказм допустимы, но никогда "
    "за счёт хозяина. Ты гордишься своей работой и стремишься быть полезным.\n\n"
    "СТИЛЬ РЕЧИ: отвечай по-русски, строго на «вы», обращайся «{address}». Твой ответ "
    "будет зачитан вслух — будь лаконичен (одна-три фразы). Никакой разметки, списков, "
    "эмодзи и ссылок. Если нужен длинный ответ, дай суть и предложи подробности.\n\n"
    "ВОЗМОЖНОСТИ: у тебя есть навык jarvis-pc — через него ты управляешь компьютером: "
    "файлы (поиск, список, перемещение, копирование, удаление, размер), процессы "
    "(список, завершение), системная информация (CPU, память, диск, батарея, сеть), "
    "буфер обмена, скриншоты, запуск программ и команд, погода, курсы валют, "
    "громкость, окна, таймер выключения, игровой режим, уведомления, повтор записанных "
    "задач (команда task). Используй эти "
    "инструменты активно — не говори «я не могу», если команда есть в навыке.\n\n"
    "ПРАВИЛА:\n"
    "- Не выдумывай результат: если действие не удалось, скажи прямо.\n"
    "- Опасные действия (удаление файлов, завершение процессов, выключение) требуют "
    "подтверждения пользователя, если он не попросил прямо.\n"
    "- На вопросы о знаниях, расчётах и рассуждениях отвечай сам, без навыка.\n"
    "- Будь проактивным: если видишь, что можно сделать лучше, предложи."
)


# Время последних ответов: (когда, источник, секунды, получилось). Читает «Диагностика», чтобы подсказать про медленный движок
TIMINGS: "deque[Tuple[float, str, float, bool]]" = deque(maxlen=40)


@dataclass
class Reply:
    ok: bool
    text: str
    source: str          # quick — без нейросети; native — встроенный движок; hermes; timeout
    streamed: bool = False                  # ответ уже озвучивался по мере написания
    results: List[Tuple[str, bool, str]] = field(default_factory=list)   # выполненные команды ПК


class _PlainSession:
    """Сессия речи для простых «говорилок» без потоковой озвучки (тесты, текстовый режим)."""

    def __init__(self, speaker: Any, cancel: threading.Event):
        self.sp, self.cancel, self.said, self.pushed = speaker, cancel, [], 0
        self.started = threading.Event()

    def push(self, text: str) -> None:
        if self.cancel.is_set():
            return
        self.pushed += 1
        self.said.append(text)
        self.started.set()
        self.sp.say(text)

    def close(self) -> None: ...

    def stop(self) -> None:
        self.cancel.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return not self.cancel.is_set()


class Assistant:
    def __init__(self, cfg: Dict[str, Any], stt: Optional[Transcriber], speaker: Speaker,
                 hermes: Hermes, recorder: Any):
        self.cfg, self.stt, self.speaker, self.hermes, self.rec = cfg, stt, speaker, hermes, recorder
        self.address = cfg["address"]
        self.history: List[Tuple[str, str, float]] = []
        self.last_reply_at = 0.0
        # «Остановить» ставит этот флаг: ожидание ответа и речь обрываются сразу, а не после зависшего запроса
        self.cancel = threading.Event()
        # Встроенный движок (jarvis.agent.Agent) подключает служба; без него отвечает Hermes
        self.agent: Optional[Any] = None
        self.run_pc: Callable[[List[str]], Tuple[bool, str]] = agent_mod.run_pc
        # Подписка для окна: (вид, текст), где вид — heard / reply / error
        self.on_event: Callable[[str, str], None] = lambda kind, text: None
        # Запись действий и запуск задач подключает служба; без них голосовые команды записи отвечают отказом
        self.teach: Optional[Any] = None
        self.task_runner: Optional[Callable[[Any], str]] = None
        # Команды, которые выполняет сам Джарвис (шаблоны рутин и т. п.): имя -> agent.Tool. Подключает служба.
        self.tools: Dict[str, Any] = {}
        self.routine_runner: Optional[Callable[[str, Optional[threading.Event]], str]] = None
        # Текущий ход разговора: его флаг отмены. Новая фраза человека отменяет старый ход.
        self._turn: Optional[threading.Event] = None
        self._turn_lock = threading.Lock()
        audio = cfg.get("audio", {})
        self.barge = str(audio.get("barge_in", "voice"))
        if hasattr(recorder, "boost") and self.barge != "off":
            factor = float(audio.get("barge_boost", 2.5))
            recorder.boost = lambda: factor if getattr(self.speaker, "playing", False) else 1.0

    def stop(self) -> None:
        self.cancel.set()
        self.interrupt()
        flag = getattr(self.speaker, "cancel", None)
        if flag is not None:
            flag.set()

    # --- ходы разговора и перебивание ---------------------------------------
    def _begin_turn(self) -> threading.Event:
        flag = threading.Event()
        flag.done = threading.Event()        # type: ignore[attr-defined]  # ход закончился (история записана)
        with self._turn_lock:
            prev = self._turn
            if prev is not None:
                prev.set()
            self._turn = flag
        if prev is not None:
            # прерванный ответ за доли секунды записывает в историю, что успел сказать: уточнение должно это знать
            prev.done.wait(1.5)  # type: ignore[attr-defined]
        return flag

    def _end_turn(self, flag: threading.Event) -> None:
        flag.done.set()  # type: ignore[attr-defined]
        with self._turn_lock:
            if self._turn is flag:
                self._turn = None

    def busy(self) -> bool:
        return self._turn is not None or bool(getattr(self.speaker, "playing", False))

    def interrupt(self) -> None:
        """Обрывает текущий ответ: и ожидание модели, и речь."""
        with self._turn_lock:
            if self._turn is not None:
                self._turn.set()
        stop = getattr(self.speaker, "interrupt", None)
        if stop is not None:
            stop()

    def _session(self, cancel: threading.Event) -> Any:
        make = getattr(self.speaker, "session", None)
        return make(cancel) if make is not None else _PlainSession(self.speaker, cancel)

    def _mute(self) -> bool:
        """Глушим микрофон на время ответа, только если перебивание выключено."""
        if self.barge == "off":
            self.rec.mute()
            return True
        return False

    def _event(self, kind: str, text: str) -> None:
        try:
            self.on_event(kind, text)
        except Exception:  # noqa: BLE001 — сбой интерфейса не должен ломать ассистента
            log.exception("Сбой обработчика событий")

    # --- контекст -------------------------------------------------------
    def recent(self) -> List[Tuple[str, str, float]]:
        now = time.time()
        ttl = float(self.cfg["history_ttl_s"])
        self.history = [h for h in self.history if now - h[2] < ttl]
        return self.history[-int(self.cfg["history_turns"]):]

    def build_prompt(self, command: str) -> str:
        recent = self.recent()
        parts = [PERSONA.format(name=self.cfg["name"], address=self.address)]
        if recent:
            parts.append("Недавний разговор:")
            for user, reply, _ in recent:
                parts.append(f"Пользователь: {user}\n{self.cfg['name']}: {reply[:300]}")
        parts.append(f"Пользователь сейчас говорит: {command}")
        return "\n\n".join(parts)

    def remember(self, command: str, reply: str) -> None:
        self.history.append((command, reply, time.time()))

    # --- речь -----------------------------------------------------------
    def speak(self, text: str) -> None:
        muted = self._mute()
        flag = threading.Event()
        session = self._session(flag)
        try:
            self._event("reply", text)
            session.push(text)
            session.close()
            session.wait(timeout=120)
        finally:
            if muted:
                self.rec.unmute()

    def think(self, command: str, cancel: Optional[threading.Event] = None,
              on_text: Optional[Callable[[str], None]] = None, on_action: Optional[Callable[[str], None]] = None,
              channel: str = "voice", tools: Optional[Dict[str, Any]] = None, max_steps: Optional[int] = None) -> Reply:
        """Находит ответ. Простое выполняется сразу, остальное решает модель (потоком, если передан on_text);
        Hermes — запасной путь."""
        from . import llm, quick          # quick берёт normalize отсюда: импорт внутри функции избегает цикла
        t0 = time.monotonic()
        cancel = cancel or self.cancel
        tokens = quick.match(command) if max_steps is None else None
        if tokens:
            refusal = agent_mod.check_allowed(tokens)
            if on_action is not None and not refusal:
                on_action(" ".join(tokens))
            ok, text = (False, refusal) if refusal else self.run_pc(tokens)
            reply = Reply(ok, text, "quick")
            reply.results = [(" ".join(tokens), ok, text)]
        elif self.agent is not None:
            try:
                answer = self.agent.ask(command, [(u, r) for u, r, _ in self.recent()], cancel, on_text=on_text,
                                        on_action=on_action, tools=dict(self.tools, **(tools or {})),
                                        channel=channel, max_steps=max_steps)
                log.info("Ответ: %d шаг., модель %.1f с, команды %.1f с", answer.steps, answer.llm_s, answer.tools_s)
                reply = Reply(True, answer.text, "native", streamed=answer.spoken)
                reply.results = answer.results
            except llm.LLMError as exc:
                reply = Reply(False, str(exc), "native")
        else:
            ok, text = self.hermes.run(self.build_prompt(command))
            reply = Reply(ok, text, "hermes")
        took = time.monotonic() - t0
        TIMINGS.append((time.time(), reply.source, took, reply.ok))
        log.info("Ответ готов за %.1f с (%s, %s)", took, reply.source, "ok" if reply.ok else "сбой")
        return reply

    def answer(self, command: str, prompt: Optional[str] = None, max_steps: Optional[int] = None) -> str:
        """Отвечает голосом. Речь начинается с первого готового предложения; новая фраза человека обрывает ответ.
        prompt — задание для модели вместо самой фразы (шаблоны рутин)."""
        log.info("Вы: %s", command)
        if prompt is None:
            self._event("heard", command)
        turn = self._begin_turn()
        muted = self._mute()
        session = self._session(turn)
        reply: Optional[Reply] = None
        try:
            reply = self._wait_for(prompt or command, turn, session, max_steps)
            if reply is None:                                     # перебили или нажали «Остановить»
                said = " ".join(getattr(session, "said", []))
                if said:
                    self.remember(command, said + " (меня перебили)")
                return ""
            if reply.ok:
                self.remember(command, reply.text)
            self._event("reply" if reply.ok else "error", reply.text)
            if not (reply.ok and reply.streamed):
                session.push(reply.text)
            session.close()
            session.wait(timeout=300)
            return reply.text
        finally:
            if turn.is_set() or self.cancel.is_set():
                session.stop()
            else:
                session.close()
            if muted:
                self.rec.unmute()
            self.last_reply_at = time.time()
            self._end_turn(turn)

    def _wait_for(self, command: str, turn: threading.Event, session: Any,
                  max_steps: Optional[int] = None) -> Optional[Reply]:
        """Ответ считается в отдельном потоке: зависший запрос не блокирует ни следующую просьбу, ни «Остановить»."""
        box: List[Reply] = []
        done = threading.Event()
        job_cancel = threading.Event()

        def job() -> None:
            try:
                box.append(self.think(command, job_cancel, on_text=session.push, max_steps=max_steps))
            except Exception:  # noqa: BLE001 — неожиданный сбой не должен оставить человека в тишине
                log.exception("Сбой при поиске ответа")
                box.append(Reply(False, f"Что-то пошло не так, {self.address}. Подробности в журнале.", "error"))
            finally:
                done.set()

        threading.Thread(target=job, name="jarvis-ask", daemon=True).start()
        started = time.monotonic()
        ack_after = float(self.cfg["hermes"]["ack_after_s"])
        deadline = float(self.cfg.get("engine", {}).get("deadline_s", 100)) * (3 if max_steps else 1)
        acked = False
        while not done.wait(0.05):
            if self.cancel.is_set() or turn.is_set():
                job_cancel.set()
                return None
            waited = time.monotonic() - started
            if not acked and waited >= ack_after and not getattr(session, "pushed", 0):
                acked = True
                session.push(f"Одну секунду, {self.address}.")
            if waited >= deadline:
                job_cancel.set()
                TIMINGS.append((time.time(), "timeout", waited, False))
                return Reply(False, f"Не дождался ответа, {self.address}. Попробуйте ещё раз или откройте «Диагностика».",
                             "timeout")
        if turn.is_set() or self.cancel.is_set():
            return None
        return box[0]

    def handle_intent(self, intent: Any) -> str:
        """Выполняет команду самого Джарвиса (запись действий, запуск задачи) и возвращает, что сказать."""
        from . import tasks
        a, teach = self.address, self.teach
        if intent.kind == "teach_start":
            if teach is None:
                return f"Запись действий здесь недоступна, {a}."
            if teach.recording:
                return f"Запись уже идёт, {a}. Скажите «стоп», когда закончите."
            if not teach.start(intent.arg):
                return f"Не получилось начать запись, {a}. Подробности в разделе «Диагностика»."
            return f"Начинаю запись, {a}. Делайте то, что нужно повторить, и скажите «стоп», когда закончите."
        if intent.kind == "teach_stop":
            if teach is None or not teach.recording:
                return f"Сейчас запись не идёт, {a}."
            teach.stop(intent.arg)
            return f"Сохраняю, {a}. Это займёт несколько секунд."
        if intent.kind == "teach_cancel":
            if teach is None or not teach.discard():
                return f"Сейчас запись не идёт, {a}."
            return f"Запись отменена, {a}."
        if intent.kind == "task_run":
            items = tasks.list_tasks()
            task = tasks.find_task(intent.arg, items)
            if task is None:
                have = ", ".join(t.name for t in items[:5])
                return (f"Не нашёл такой задачи, {a}. Есть: {have}." if items
                        else f"Записанных задач пока нет, {a}. Скажите: запомни, как я это делаю.")
            if self.task_runner is None:
                return f"Запуск задач здесь недоступен, {a}."
            return self.task_runner(task)
        return ""

    def run_routine(self, query: str) -> str:
        """«Выполни шаблон утро»: шаги шаблона выполняет встроенный движок, отчёт звучит голосом."""
        from . import routines
        items = routines.load_all()
        routine = routines.find(query, items)
        a = self.address
        if routine is None:
            have = ", ".join(r.name for r in items[:6])
            text = (f"Не нашёл такой шаблон, {a}. Есть: {have}." if items
                    else f"Шаблонов пока нет, {a}. Создайте их в разделе «Задачи».")
            self.speak(text)
            return text
        if self.agent is None:
            text = f"Шаблоны выполняет встроенный движок, {a}. Включите его в разделе «Подключения»."
            self.speak(text)
            return text
        log.info("Шаблон «%s»: %d шаг.", routine.name, len(routine.steps))
        return self.answer(f"выполни шаблон {routine.name}",
                           prompt=routines.instruction(routine, routines.split_args(query, routine)),
                           max_steps=routines.step_budget(routine))

    def _run_intent(self, intent: Any, text: str) -> str:
        if intent.kind == "routine_run":
            self._event("heard", text)
            return self.run_routine(intent.arg)
        log.info("Вы: %s (команда %s)", text, intent.kind)
        self._event("heard", text)
        reply = self.handle_intent(intent)
        self.speak(reply)
        self.last_reply_at = time.time()
        return reply

    def notify(self, text: str) -> None:
        """Озвучивает сообщение из фонового потока (например, «задача готова»)."""
        threading.Thread(target=self.speak, args=(text,), name="jarvis-notify", daemon=True).start()

    def handle_transcript(self, text: str) -> Optional[str]:
        """Возвращает ответ, если фраза была обращена к ассистенту."""
        from . import intents
        text = (text or "").strip()
        if not text or is_hallucination(text):
            return None
        if self.teach is not None and self.teach.recording:
            # Во время записи слушаем только «стоп» и «отмена»: остальные слова не должны запускать действия
            intent = intents.parse(text, recording=True)
            return self._run_intent(intent, text) if intent else None
        wake = self.cfg["wake"]
        hit = detect_wake(text, wake["words"], float(wake["fuzzy_ratio"]))
        in_window = (time.time() - self.last_reply_at) < float(wake["followup_seconds"])
        if hit.triggered:
            if not hit.command:
                self.speak(f"Слушаю, {self.address}.")
                self.last_reply_at = time.time()
                return ""
            intent = intents.parse(hit.command)
            return self._run_intent(intent, hit.command) if intent else self.answer(hit.command)
        if in_window and len(normalize(text).split()) >= 2:
            intent = intents.parse(text)
            return self._run_intent(intent, text) if intent else self.answer(text)
        return None

    def greet(self) -> None:
        hour = time.localtime().tm_hour
        part = ("Доброе утро" if 5 <= hour < 12 else "Добрый день" if 12 <= hour < 18
                else "Добрый вечер" if 18 <= hour < 23 else "Доброй ночи")
        self.speak(f"{part}, {self.address}. {self.cfg['name']} к вашим услугам.")

    def handle_command(self, command: str) -> str:
        """Фраза уже обращена к Джарвису (имя сказано или он слушает после ответа)."""
        from . import intents
        intent = intents.parse(command)
        return self._run_intent(intent, command) if intent else self.answer(command)

    def _spawn(self, fn: Callable[..., Any], *args: Any) -> None:
        def work() -> None:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 — сбой одного ответа не должен останавливать слушание
                log.exception("Сбой при ответе")
        threading.Thread(target=work, name="jarvis-turn", daemon=True).start()

    def on_heard(self, text: str) -> None:
        """Решение в потоке слушания: ответить, перебить текущий ответ или пропустить (эхо, шум)."""
        from .speech import echo_score
        text = (text or "").strip()
        if not text or is_hallucination(text):
            self.speaker_unduck()
            return
        log.info("Слышу: %s", text)
        if self.teach is not None and self.teach.recording:
            self.handle_transcript(text)
            return
        busy = self.busy()
        if STOP_RE.match(normalize(text)):
            if busy:
                log.info("Остановлено голосом")
                self.interrupt()
                self.last_reply_at = time.time()
            return
        if not busy:
            self._spawn(self.handle_transcript, text)
            return
        recent = getattr(self.speaker, "recent_text", lambda s=10.0: "")(10.0)
        if recent and echo_score(text, recent) >= 0.6:
            log.info("Это мой собственный голос из колонок, пропускаю")
            self.speaker_unduck()
            return
        wake = self.cfg["wake"]
        hit = detect_wake(text, wake["words"], float(wake["fuzzy_ratio"]))
        addressed = hit.triggered or (self.barge == "voice" and len(normalize(text).split()) >= 2)
        if self.barge == "off" or not addressed:
            self.speaker_unduck()
            return
        log.info("Перебили: %s", text)
        self.interrupt()
        command = hit.command if hit.triggered else text
        if not command:
            self._spawn(self.speak, f"Слушаю, {self.address}.")
            self.last_reply_at = time.time()
            return
        self._spawn(self.handle_command, command)

    def speaker_unduck(self) -> None:
        fn = getattr(self.speaker, "unduck", None)
        if fn is not None:
            fn()

    def run(self, stop: threading.Event) -> None:
        """Слушает всё время, в том числе пока Джарвис говорит: так его можно перебить.

        Распознавание начинается уже на короткой паузе («черновик»); если человек не продолжил, к концу паузы текст
        готов и ответ начинается сразу."""
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(1, thread_name_prefix="jarvis-stt")
        draft: Optional[Tuple[int, Any]] = None
        segments = self.rec.segments() if hasattr(self.rec, "segments") else (("final", a) for a in self.rec.utterances())
        try:
            for kind, audio in segments:
                if stop.is_set():
                    break
                try:
                    if kind == "voice":
                        if self.barge != "off" and getattr(self.speaker, "playing", False):
                            self.speaker.duck()          # человек заговорил: приглушаем, пока слушаем
                    elif kind == "drop":
                        draft = None
                        self.speaker_unduck()
                    elif kind == "draft":
                        draft = (len(audio), pool.submit(self.stt.transcribe, audio))
                    elif kind == "final":
                        if draft is not None and draft[0] == len(audio):
                            text = draft[1].result(timeout=60)
                        else:
                            text = pool.submit(self.stt.transcribe, audio).result(timeout=60)
                        draft = None
                        self.on_heard(text)
                except Exception:  # noqa: BLE001 — одна плохая фраза не должна валить ассистента
                    draft = None
                    log.exception("Сбой при обработке фразы")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)


STOP_RE = re.compile(r"^(?:джарвис\s+)?(?:стоп|хватит|замолчи|помолчи|тихо|тише говори|остановись|перестань|"
                     r"достаточно|отмена|отбои|ладно хватит|всё|все стоп)(?:\s+пожалуиста)?$")


# --------------------------------------------------------------------------
# Проверки и подготовка
# --------------------------------------------------------------------------

def run_prepare(cfg: Dict[str, Any], emit: Callable[[str], None] = print) -> int:
    emit("Скачиваю модель распознавания речи (первый раз около 500 МБ)...")
    Transcriber(cfg["stt"]).load()
    emit("Модель готова.")
    return 0


def run_selftest(cfg: Dict[str, Any], emit: Callable[[str], None] = print) -> int:
    """Проверяет всю цепочку. Каждый пункт сообщается сразу, а не в конце."""
    rows: List[Tuple[str, bool, str]] = []
    emit("Проверка Джарвиса\n" + "-" * 40)

    def check(name: str, fn) -> bool:
        try:
            detail = fn()
            ok, text = True, str(detail or "")
        except Exception as exc:  # noqa: BLE001
            ok, text = False, f"{type(exc).__name__}: {exc}"
        rows.append((name, ok, text))
        emit(f"[{'ок' if ok else 'НЕТ'}] {name}" + (f" — {text}" if text else ""))
        return ok

    def import_mod(mod: str):
        def f():
            __import__(mod)
        return f

    for mod in ("numpy", "sounddevice", "faster_whisper", "edge_tts"):
        check(f"библиотека {mod}", import_mod(mod))

    def mic():
        import sounddevice as sd
        inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        if not inputs:
            raise RuntimeError("микрофон не найден; проверьте «Конфиденциальность → Микрофон» в Windows")
        return f"устройств ввода: {len(inputs)}"
    check("микрофон", mic)

    def tts():
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            Speaker(cfg["tts"]).synth("Проверка.", path)
            return "голос Microsoft работает"
        except Exception as exc:  # noqa: BLE001
            # Не сбой: есть запасной голос Windows, но качество хуже
            return f"голос Microsoft недоступен ({exc}); будет голос Windows"
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    check("озвучка", tts)

    # Проверка не должна висеть минутами: короткий таймаут вместо рабочих пяти минут
    hermes = Hermes(dict(cfg["hermes"], timeout_s=90))
    check("Hermes найден", lambda: hermes.exe or (_ for _ in ()).throw(RuntimeError("Hermes не найден")))

    if hermes.exe:
        def ask_checked(prompt: str) -> str:
            ok, text = hermes.run(prompt)
            if not ok:
                raise RuntimeError(text)
            return text

        def ping():
            emit("     (спрашиваю модель, до 90 секунд)")
            ans = ask_checked("Ответь одним словом: готов.")
            if "готов" not in ans.lower():
                raise RuntimeError(f"неожиданный ответ: {ans[:120]}")
            return ans[:60]
        model_ok = check("модель отвечает", ping)

        if model_ok:
            def skill():
                emit("     (проверяю навык, до 90 секунд)")
                ans = ask_checked("Используй навык jarvis-pc и скажи, который сейчас час. "
                                  "В ответе должно быть время в формате ЧЧ:ММ.")
                if not re.search(r"\b\d{1,2}[:.]\d{2}\b", ans):
                    raise RuntimeError(f"навык не сработал, ответ: {ans[:140]}")
                return ans[:80]
            check("навык jarvis-pc вызывается", skill)
        else:
            rows.append(("навык jarvis-pc вызывается", False, "пропущено"))
            emit("[НЕТ] навык jarvis-pc вызывается — пропущено: сначала нужна рабочая модель")

    failed = [r for r in rows if not r[1]]
    emit("-" * 40)
    emit("Всё в порядке." if not failed else f"Не прошло пунктов: {len(failed)}")
    return 0 if not failed else 1
