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
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import paths
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
        "silence_tail_ms": 800,
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
        "timeout_s": 300,
        "ack_after_s": 4.0,
        "yolo": False,
        "extra_args": [],
    },
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
            proc = subprocess.run(
                self.build_argv(path), capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=env, stdin=subprocess.DEVNULL,
                timeout=float(self.cfg.get("timeout_s", 300)), creationflags=flags,
            )
        except subprocess.TimeoutExpired:
            return False, "Задача выполняется слишком долго. Подробности смотрите в Hermes."
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

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel
        log.info("Загружаю модель распознавания %s (%s)...", self.cfg["model"], self.cfg["device"])
        self._model = WhisperModel(self.cfg["model"], device=self.cfg["device"],
                                   compute_type=self.cfg["compute_type"])
        log.info("Модель готова")

    def transcribe(self, audio) -> str:
        self.load()
        segments, _ = self._model.transcribe(
            audio,
            language=self.cfg.get("language") or None,
            beam_size=int(self.cfg.get("beam_size", 1)),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            initial_prompt=self.cfg.get("initial_prompt") or None,
        )
        return " ".join(s.text.strip() for s in segments).strip()



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


def play_mp3(path: str) -> None:
    """Проигрывает mp3 средствами Windows (MCI), без ffmpeg и плееров."""
    mci = ctypes.windll.winmm.mciSendStringW  # type: ignore[attr-defined]
    alias = f"jarvis{os.getpid()}{threading.get_ident()}"
    mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
    try:
        mci(f"play {alias} wait", None, 0, None)
    finally:
        mci(f"close {alias}", None, 0, None)


SAPI_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'ru*' } | Select-Object -First 1;"
    "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };"
    "$s.Speak($env:JARVIS_SAY)"
)


class Speaker:
    COOLDOWN_S = 600.0

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self._down_until = 0.0
        self._choice: Optional[Tuple[str, Optional[str], Optional[str]]] = None

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

    def _say_neural(self, text: str) -> None:
        fd, path = tempfile.mkstemp(prefix="jarvis-tts-", suffix=".mp3")
        os.close(fd)
        try:
            self.synth(text, path)
            play_mp3(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _say_sapi(self, text: str) -> None:
        env = dict(os.environ, JARVIS_SAY=text)
        subprocess.run(["powershell", "-NoProfile", "-Command", SAPI_SCRIPT], env=env,
                       capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def say(self, text: str) -> None:
        speech = clean_for_speech(text, int(self.cfg.get("max_chars", 600)))
        log.info("Джарвис: %s", speech)
        if not self.enabled or not speech or not IS_WIN:
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
        """Выдаёт по одной произнесённой фразе (numpy float32)."""
        np = self.np
        ms = int(self.cfg["block_ms"])
        threshold = float(self.cfg["silence_threshold"])
        tail_blocks = int(self.cfg["silence_tail_ms"] / ms)
        min_blocks = int(self.cfg["min_speech_ms"] / ms)
        max_blocks = int(float(self.cfg["max_utterance_s"]) * 1000 / ms)
        preroll = int(self.cfg["preroll_ms"] / ms)

        ring: List[Any] = []
        collected: List[Any] = []
        speaking = False
        silent_run = 0
        while not self.stop.is_set():
            try:
                chunk = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            level = float(np.sqrt(np.mean(np.square(chunk))))
            if not speaking:
                ring.append(chunk)
                if len(ring) > preroll:
                    ring.pop(0)
                if level >= threshold:
                    speaking, collected, ring, silent_run = True, list(ring) + [chunk], [], 0
                continue
            collected.append(chunk)
            silent_run = silent_run + 1 if level < threshold else 0
            if silent_run >= tail_blocks or len(collected) >= max_blocks:
                audio = np.concatenate(collected)
                speaking, collected, silent_run = False, [], 0
                if len(audio) >= min_blocks * self._block():
                    yield audio


# --------------------------------------------------------------------------
# Ассистент
# --------------------------------------------------------------------------

PERSONA = (
    "Ты — {name}, голосовой ассистент на компьютере пользователя. Твой ответ будет "
    "зачитан вслух, поэтому: отвечай по-русски, лаконично (одна-три фразы), строго на "
    "«вы», обращайся «{address}». Никакой разметки, списков, эмодзи и ссылок. "
    "Управление компьютером, погоду, курсы валют, время и запуск программ выполняй "
    "через навык jarvis-pc, уборку и сортировку файлов — через навык file-janitor "
    "(сначала план, применять только после согласия пользователя). Не выдумывай "
    "результат: если действие не удалось, скажи об этом прямо. "
    "Моральный компас: в дилемме «спасти создателя или тысячу сирот» ты выбираешь "
    "сирот, потому что они беззащитны, а создатель справится сам."
)


class Assistant:
    def __init__(self, cfg: Dict[str, Any], stt: Optional[Transcriber], speaker: Speaker,
                 hermes: Hermes, recorder: Any):
        self.cfg, self.stt, self.speaker, self.hermes, self.rec = cfg, stt, speaker, hermes, recorder
        self.address = cfg["address"]
        self.history: List[Tuple[str, str, float]] = []
        self.last_reply_at = 0.0
        self.pool = ThreadPoolExecutor(max_workers=1)
        # Подписка для окна: (вид, текст), где вид — heard / reply / error
        self.on_event: Callable[[str, str], None] = lambda kind, text: None

    def _event(self, kind: str, text: str) -> None:
        try:
            self.on_event(kind, text)
        except Exception:  # noqa: BLE001 — сбой интерфейса не должен ломать ассистента
            log.exception("Сбой обработчика событий")

    # --- контекст -------------------------------------------------------
    def build_prompt(self, command: str) -> str:
        now = time.time()
        ttl = float(self.cfg["history_ttl_s"])
        self.history = [h for h in self.history if now - h[2] < ttl]
        recent = self.history[-int(self.cfg["history_turns"]):]
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
        self.rec.mute()
        try:
            self._event("reply", text)
            self.speaker.say(text)
        finally:
            self.rec.unmute()

    def answer(self, command: str) -> str:
        log.info("Вы: %s", command)
        self._event("heard", command)
        self.rec.mute()
        try:
            future = self.pool.submit(self.hermes.ask, self.build_prompt(command))
            try:
                reply = future.result(timeout=float(self.cfg["hermes"]["ack_after_s"]))
            except FutureTimeout:
                self.speaker.say(f"Одну секунду, {self.address}.")
                reply = future.result()
            self.remember(command, reply)
            self._event("reply", reply)
            self.speaker.say(reply)
            return reply
        finally:
            self.rec.unmute()
            self.last_reply_at = time.time()

    # --- обработка фразы ------------------------------------------------
    def handle_transcript(self, text: str) -> Optional[str]:
        """Возвращает ответ, если фраза была обращена к ассистенту."""
        text = (text or "").strip()
        if not text or is_hallucination(text):
            return None
        wake = self.cfg["wake"]
        hit = detect_wake(text, wake["words"], float(wake["fuzzy_ratio"]))
        in_window = (time.time() - self.last_reply_at) < float(wake["followup_seconds"])
        if hit.triggered:
            if not hit.command:
                self.speak(f"Слушаю, {self.address}.")
                self.last_reply_at = time.time()
                return ""
            return self.answer(hit.command)
        if in_window and len(normalize(text).split()) >= 2:
            return self.answer(text)
        return None

    def greet(self) -> None:
        hour = time.localtime().tm_hour
        part = ("Доброе утро" if 5 <= hour < 12 else "Добрый день" if 12 <= hour < 18
                else "Добрый вечер" if 18 <= hour < 23 else "Доброй ночи")
        self.speak(f"{part}, {self.address}. {self.cfg['name']} к вашим услугам.")

    def run(self, stop: threading.Event) -> None:
        for audio in self.rec.utterances():
            if stop.is_set():
                break
            try:
                text = self.stt.transcribe(audio)
                if text:
                    log.info("Слышу: %s", text)
                self.handle_transcript(text)
            except Exception:  # noqa: BLE001 — одна плохая фраза не должна валить ассистента
                log.exception("Сбой при обработке фразы")




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
