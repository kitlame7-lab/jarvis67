"""Загрузка модели распознавания речи.

Раньше модель (около 480 МБ) скачивалась молча: окно годами висело на «Загружаюсь…», а перезапуск и остановка не
работали, пока скачивание не закончится. Здесь:

* прогресс в байтах и скорость, которые видит окно;
* мгновенная отмена (по событию `cancel`);
* обрыв и переход на запасной адрес (зеркало), если байты перестали идти;
* понятное сообщение, что именно не получилось и что делать.

Модуль не импортирует тяжёлые библиотеки при загрузке и не знает про Qt.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Таймауты huggingface_hub читаются при его импорте, поэтому выставляем до первого обращения.
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "20")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")

DEFAULT_ENDPOINTS = ["https://huggingface.co", "https://hf-mirror.com"]
MODEL_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
# Примерный размер для подписи «из N МБ», пока сервер не сообщил точный
APPROX_MB = {"tiny": 75, "tiny.en": 75, "base": 145, "base.en": 145, "small": 484, "small.en": 484,
             "medium": 1500, "medium.en": 1500, "large-v3": 3000, "large-v2": 3000, "large-v1": 3000}
STALL_SECONDS = 40.0


class Cancelled(Exception):
    """Загрузку остановил пользователь."""


class DownloadFailed(Exception):
    """Не удалось скачать ни с одного адреса. В тексте — причины и что делать."""


@dataclass
class Progress:
    """Снимок состояния загрузки для окна."""
    phase: str = "check"            # check | download | switch | loading | done
    done: int = 0
    total: int = 0
    speed: float = 0.0              # байт/с, сглаженная
    endpoint: str = ""
    attempt: int = 0

    @property
    def fraction(self) -> float:
        return min(1.0, self.done / self.total) if self.total > 0 else 0.0

    @property
    def eta_s(self) -> Optional[float]:
        if self.speed > 1024 and self.total > self.done:
            return (self.total - self.done) / self.speed
        return None

    def text(self) -> str:
        if self.phase == "check":
            return "Проверяю, есть ли модель на компьютере"
        if self.phase == "loading":
            return "Загружаю модель в память (несколько секунд)"
        if self.phase == "switch":
            return "Адрес не отвечает, пробую запасной"
        mb = 1024 * 1024
        line = f"Скачиваю модель распознавания речи: {self.done / mb:.0f} из {self.total / mb:.0f} МБ"
        if self.speed > 1024:
            line += f", {self.speed / mb:.1f} МБ/с"
        eta = self.eta_s
        if eta is not None:
            line += f", осталось {_human_eta(eta)}"
        host = self.endpoint.replace("https://", "").replace("http://", "")
        return line + (f" ({host})" if host else "")


def _human_eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{max(seconds, 1)} с"
    return f"{round(seconds / 60)} мин"


@dataclass
class _Tracker:
    """Складывает байты со всех файлов загрузки; потокобезопасен."""
    estimate: int = 0
    bars: Dict[int, List[int]] = field(default_factory=dict)       # id -> [готово, всего]
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_change: float = field(default_factory=time.monotonic)
    _speed: float = 0.0
    _last_sample: Optional[tuple] = None

    def update(self, bar_id: int, n: int, total: Optional[int] = None) -> None:
        with self.lock:
            bar = self.bars.setdefault(bar_id, [0, 0])
            bar[0] += n
            if total:
                bar[1] = total
            if n:
                self.last_change = time.monotonic()

    def snapshot(self, endpoint: str, attempt: int) -> Progress:
        with self.lock:
            done = sum(b[0] for b in self.bars.values())
            total = max(sum(b[1] for b in self.bars.values()), self.estimate)
        now = time.monotonic()
        if self._last_sample is not None:
            t0, d0 = self._last_sample
            if now - t0 >= 0.5:
                inst = max(0.0, (done - d0) / (now - t0))
                self._speed = inst if self._speed == 0 else 0.7 * self._speed + 0.3 * inst
                self._last_sample = (now, done)
        else:
            self._last_sample = (now, done)
        return Progress("download", done, total, self._speed, endpoint, attempt)


def repo_for(name: str) -> str:
    """Имя модели → репозиторий Hugging Face (small → Systran/faster-whisper-small)."""
    if "/" in name:
        return name
    from faster_whisper.utils import _MODELS      # единственный источник правды о таблице имён
    repo = _MODELS.get(name)
    if repo is None:
        raise ValueError(f"Неизвестная модель «{name}». Доступны: {', '.join(sorted(_MODELS))}")
    return repo


def default_snapshot(repo: str, **kwargs):
    from huggingface_hub import snapshot_download
    return snapshot_download(repo, **kwargs)


REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")


def models_root() -> Path:
    from . import paths
    return paths.DATA_DIR / "models"


def model_dir(name: str, root: Optional[Path] = None) -> Path:
    """Постоянное место модели. Сюда же можно положить папку с моделью вручную."""
    return (root or models_root()) / name.replace("/", "--")


def _is_complete(folder: Path) -> bool:
    return all((folder / f).is_file() for f in REQUIRED_FILES) and any(folder.glob("vocabulary.*"))


def local_path(name: str, root: Optional[Path] = None, snapshot: Callable = default_snapshot) -> Optional[str]:
    """Папка модели, если она уже на компьютере; иначе None. Сеть не используется."""
    if os.path.isdir(name):
        return name
    mine = model_dir(name, root)
    if _is_complete(mine):
        return str(mine)
    try:    # модель, скачанная старой версией приложения, лежит в общем кэше Hugging Face
        return snapshot(repo_for(name), local_files_only=True, allow_patterns=MODEL_PATTERNS)
    except Exception:       # noqa: BLE001 — «нет в кэше» у huggingface_hub выражается несколькими типами ошибок
        return None


def _tqdm_factory(tracker: _Tracker, abort: threading.Event):
    from tqdm.auto import tqdm as base

    class HubBar(base):                      # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            if abort.is_set():
                raise Cancelled()
            kwargs["disable"] = True         # рисовать нечего: прогресс забираем сами
            self._is_bytes = kwargs.get("unit") == "B"
            self._total_hint = kwargs.get("total") or 0
            self._initial_hint = kwargs.get("initial") or 0
            super().__init__(*args, **kwargs)
            if self._is_bytes:
                tracker.update(id(self), self._initial_hint, self._total_hint or None)

        def update(self, n=1):
            if abort.is_set():
                raise Cancelled()
            if self._is_bytes and n:
                tracker.update(id(self), int(n))
            return None

    return HubBar


def fetch(name: str, on_progress: Callable[[Progress], None], cancel: threading.Event,
          endpoints: Optional[List[str]] = None, root: Optional[Path] = None,
          snapshot: Callable = default_snapshot, stall_s: float = STALL_SECONDS,
          poll_s: float = 0.25) -> str:
    """Возвращает папку модели; при необходимости скачивает. Бросает Cancelled или DownloadFailed.

    Каждая попытка качает в собственную папку `<модель>.part<N>-<время>`: брошенная зависшая загрузка не может
    помешать следующей, а в постоянном месте всегда лежит только полная модель."""
    on_progress(Progress("check"))
    found = local_path(name, root, snapshot)
    if found:
        on_progress(Progress("done"))
        return found

    repo = repo_for(name)
    final = model_dir(name, root)
    final.parent.mkdir(parents=True, exist_ok=True)
    estimate = int(APPROX_MB.get(name, 500) * 1024 * 1024)
    reasons: List[str] = []
    for attempt, endpoint in enumerate(endpoints or DEFAULT_ENDPOINTS, 1):
        if cancel.is_set():
            raise Cancelled()
        part = final.parent / f"{final.name}.part{attempt}-{int(time.time())}"
        tracker = _Tracker(estimate=estimate)
        abort = threading.Event()
        box: Dict[str, object] = {}

        def work(endpoint=endpoint, tracker=tracker, abort=abort, box=box, part=part) -> None:
            try:
                box["path"] = snapshot(repo, endpoint=endpoint, allow_patterns=MODEL_PATTERNS, local_dir=str(part),
                                       tqdm_class=_tqdm_factory(tracker, abort), max_workers=4)
            except BaseException as exc:    # noqa: BLE001 — результат разбирает вызывающий поток
                box["error"] = exc

        th = threading.Thread(target=work, name="jarvis-model-download", daemon=True)
        th.start()
        started = time.monotonic()
        stalled = False
        while th.is_alive():
            th.join(poll_s)
            on_progress(tracker.snapshot(endpoint, attempt))
            if cancel.is_set():
                abort.set()
                raise Cancelled()
            if time.monotonic() - max(tracker.last_change, started) > stall_s:
                abort.set()                  # поток снимется при следующем обновлении; ждать его не нужно
                stalled = True
                break
        if stalled:
            reasons.append(f"{endpoint}: данные не поступали {int(stall_s)} секунд")
            on_progress(Progress("switch", endpoint=endpoint, attempt=attempt))
            continue
        err = box.get("error")
        if isinstance(err, Cancelled):
            raise Cancelled()
        if err is None and _is_complete(part):
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            os.replace(part, final)
            _cleanup_parts(final)
            on_progress(Progress("done", 1, 1, 0, endpoint, attempt))
            return str(final)
        reasons.append(f"{endpoint}: {type(err).__name__}: {str(err)[:160]}" if err else f"{endpoint}: неполный набор файлов")

    raise DownloadFailed(
        "Не удалось скачать модель распознавания речи.\n  " + "\n  ".join(reasons)
        + "\nПроверьте интернет или VPN и нажмите «Перезапустить». Можно выбрать меньшую модель "
          "(base, около 145 МБ) в разделе «Слух».")


def _cleanup_parts(final: Path, older_than_s: float = 3600.0) -> None:
    """Убирает недокачанные папки прошлых попыток (но не свежие: их может ещё писать брошенный поток)."""
    now = time.time()
    for old in final.parent.glob(f"{final.name}.part*"):
        try:
            if now - old.stat().st_mtime > older_than_s:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            continue
