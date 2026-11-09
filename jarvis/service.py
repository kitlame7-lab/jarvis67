"""Жизненный цикл ассистента внутри приложения: загрузка модели речи, микрофон, поток прослушивания.
Не зависит от Qt: окно подписывается на обратные вызовы и ничего больше не знает.

Запуск разбит на этапы (`diag.TIMELINE`), у каждого есть предел терпения. Если этап не двигается, сторож пишет об этом
в журнал и сообщает окну, а не оставляет человека смотреть на «Загружаюсь…» без объяснений."""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import backend, diag, models, teach, winio
from .core import (Assistant, Hermes, NullRecorder, Recorder, Speaker, Transcriber, load_settings, log)

# Состояния, которые видит окно
STOPPED, LOADING, LISTENING, PAUSED, ERROR = "stopped", "loading", "listening", "paused", "error"

STATE_TEXT = {
    STOPPED: "Остановлен",
    LOADING: "Загружаюсь…",
    LISTENING: "Слушаю",
    PAUSED: "На паузе",
    ERROR: "Ошибка",
}

# Этапы запуска и сколько секунд без движения считается зависанием
STAGE_DOWNLOAD, STAGE_MEMORY, STAGE_MIC = "скачивание модели", "загрузка модели в память", "микрофон"
PATIENCE_S = {STAGE_DOWNLOAD: 75.0, STAGE_MEMORY: 120.0, STAGE_MIC: 30.0}
WATCH_EVERY_S = 2.0


class AssistantService:
    def __init__(self, on_state: Callable[[str, str], None] = lambda s, d: None,
                 on_event: Callable[[str, str], None] = lambda k, t: None,
                 on_progress: Callable[[float, str], None] = lambda f, t: None,
                 on_recording: Callable[[bool], None] = lambda r: None):
        self.on_state, self.on_event, self.on_progress = on_state, on_event, on_progress
        self.on_recording = on_recording
        self._teacher: Optional[teach.Teacher] = None
        self._winapi: Optional[winio.WinApi] = None
        self.state = STOPPED
        self.detail = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._start_again = False
        self._recorder: Optional[Recorder] = None
        self._assistant: Optional[Assistant] = None
        self._lock = threading.RLock()
        self._stt: Optional[Transcriber] = None
        self._stt_key: Any = None
        self._base_detail = ""
        self._last_motion = time.monotonic()
        self._last_bytes = -1

    # --- состояние ---------------------------------------------------------
    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        self._base_detail = detail
        try:
            self.on_state(state, detail)
        except Exception:  # noqa: BLE001
            log.exception("Сбой обработчика состояния")

    def _progress(self, fraction: float, text: str) -> None:
        try:
            self.on_progress(fraction, text)
        except Exception:  # noqa: BLE001
            log.exception("Сбой обработчика прогресса")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- запуск и остановка ------------------------------------------------
    def _spawn(self) -> None:
        """Запускает новый поток ассистента. Вызывать под блокировкой."""
        self._stop = threading.Event()
        stop = self._stop
        self._last_motion = time.monotonic()
        self._last_bytes = -1
        diag.TIMELINE.clear()
        self._thread = threading.Thread(target=self._thread_main, args=(stop,), name="jarvis-service", daemon=True)
        self._thread.start()
        threading.Thread(target=self._watch, args=(stop, self._thread), name="jarvis-watchdog", daemon=True).start()

    def start(self) -> None:
        with self._lock:
            if self.running:
                if self._stop.is_set():
                    # Остановка ещё идёт (например, модель грузится в память и прервать её нельзя):
                    # запомним просьбу, поток сам запустит новый, как только закончит
                    self._start_again = True
                return
            self._spawn()

    def stop(self, wait: float = 5.0) -> None:
        with self._lock:
            self._start_again = False
            self._stop.set()
            rec = self._recorder
            if rec is not None:
                rec.stop.set()
            th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(wait)
        self._progress(-1.0, "")
        self._set(STOPPED)

    def restart(self) -> None:
        """Применить новые настройки: микрофон и модель читаются заново."""
        self.stop()
        self.start()

    def set_paused(self, paused: bool) -> None:
        rec = self._recorder
        if rec is None:
            return
        rec.paused = paused
        self._set(PAUSED if paused else LISTENING)

    # --- сторож ------------------------------------------------------------
    def _watch(self, stop: threading.Event, main: threading.Thread) -> None:
        """Раз в пару секунд проверяет, что запуск не завис, и показывает, сколько он уже идёт."""
        warned = set()
        while main.is_alive() and not stop.wait(WATCH_EVERY_S):
            if self.state != LOADING:
                continue
            cur = diag.TIMELINE.current()
            if not cur:
                continue
            stage, elapsed = cur["stage"], cur["elapsed"]
            if stage != STAGE_DOWNLOAD and self._base_detail:
                # без этой подписи при загрузке в память окно выглядит замёрзшим
                self.detail = f"{self._base_detail.split(' · ')[0]} · {int(elapsed)} с"
                try:
                    self.on_state(LOADING, self.detail)
                except Exception:  # noqa: BLE001
                    log.debug("Сбой обработчика состояния в сторожe", exc_info=True)
            idle = time.monotonic() - self._last_motion if stage == STAGE_DOWNLOAD else elapsed
            if idle > PATIENCE_S.get(stage, 120.0) and (stage, "stuck") not in warned:
                warned.add((stage, "stuck"))
                msg = (f"Запуск не двигается {int(idle)} с на этапе «{stage}». "
                       "Откройте «Диагностика»: там указана причина и что делать.")
                log.error(msg)
                diag.record_error("запуск", msg)
                self.on_event("error", msg)

    # --- основной цикл -----------------------------------------------------
    def _transcriber(self, cfg: Dict[str, Any]) -> Transcriber:
        key = tuple(sorted((k, str(v)) for k, v in cfg["stt"].items()))
        if self._stt is None or self._stt_key != key:
            self._stt = Transcriber(cfg["stt"])
            self._stt_key = key
        return self._stt

    def _load_stt(self, stt: Transcriber, stop: threading.Event) -> None:
        stage = {"name": ""}

        def on_prog(p: models.Progress) -> None:
            name = STAGE_MEMORY if p.phase == "loading" else STAGE_DOWNLOAD
            if stage["name"] != name:
                stage["name"] = name
                diag.TIMELINE.begin(name, p.text())
            if p.done != self._last_bytes or p.phase != "download":
                self._last_bytes = p.done
                self._last_motion = time.monotonic()
            self._progress(p.fraction if p.phase == "download" and p.total else -1.0, p.text())
            if p.text() != self.detail:
                self._set(LOADING, p.text())

        diag.TIMELINE.begin(STAGE_DOWNLOAD, "проверка локальной копии")
        self._set(LOADING, "Проверяю, есть ли модель на компьютере")
        stt.load(on_progress=on_prog, cancel=stop)
        self._progress(-1.0, "")

    def _thread_main(self, stop: threading.Event) -> None:
        try:
            self._run(stop)
        finally:
            diag.TIMELINE.end()
            with self._lock:
                again = self._start_again and self._stop is stop
                self._start_again = False
                if again:
                    self._spawn()

    def _run(self, stop: threading.Event) -> None:
        try:
            cfg = load_settings()
            try:
                self._load_stt(self._transcriber(cfg), stop)
            except models.Cancelled:
                log.info("Загрузка модели прервана")
                return
            except models.DownloadFailed as exc:
                log.error("%s", exc)
                diag.record_error("загрузка модели", str(exc))
                self._progress(-1.0, "")
                self._set(ERROR, str(exc).splitlines()[0])
                self.on_event("error", str(exc))
                return
            speaker = Speaker(cfg["tts"])
            hermes = Hermes(cfg["hermes"])
            if not hermes.exe:
                self.on_event("error", "Hermes не найден. Откройте раздел «Диагностика».")
            greeted = False
            while not stop.is_set():
                recorder: Optional[Recorder] = None
                try:
                    diag.TIMELINE.begin(STAGE_MIC)
                    recorder = Recorder(cfg["audio"])
                    self._set(LOADING, "Открываю микрофон")
                    recorder.open()
                    self._recorder = recorder
                    if cfg["audio"].get("auto_threshold", True):
                        self._set(LOADING, "Замеряю шум в комнате")
                        recorder.cfg["silence_threshold"] = recorder.calibrate()
                        log.info("Порог тишины: %.4f", recorder.cfg["silence_threshold"])
                    assistant = Assistant(cfg, self._stt, speaker, hermes, recorder)
                    assistant.on_event = self.on_event
                    self.attach(assistant)
                    self._assistant = assistant
                    diag.TIMELINE.end()
                    self._set(LISTENING)
                    if not greeted and cfg.get("greeting", True):
                        assistant.greet()
                    greeted = True
                    log.info("Слушаю. Скажите «%s».", cfg["name"])
                    assistant.run(stop)
                except Exception as exc:  # noqa: BLE001 — микрофон отключили, пропал звук: пробуем снова
                    log.exception("Сбой основного цикла, повтор через 5 секунд")
                    diag.TIMELINE.end()
                    diag.record_error("микрофон или звук", f"{type(exc).__name__}: {exc}")
                    self._set(ERROR, f"{type(exc).__name__}: {exc}")
                    self.on_event("error", f"Микрофон или звук: {exc}")
                    stop.wait(5)
                finally:
                    if recorder is not None:
                        try:
                            recorder.close()
                        except Exception:  # noqa: BLE001
                            log.debug("Не удалось закрыть микрофон", exc_info=True)
                    self._recorder = None
        except Exception as exc:  # noqa: BLE001
            log.exception("Ассистент не запустился")
            diag.record_error("запуск", f"{type(exc).__name__}: {exc}")
            self._progress(-1.0, "")
            self._set(ERROR, f"{type(exc).__name__}: {exc}")
            self.on_event("error", f"Не удалось запуститься: {exc}")
            return
        self._set(STOPPED)

    # --- запись действий и задачи ---------------------------------------------
    @property
    def teacher(self) -> Optional[teach.Teacher]:
        """Рекордер создаётся один раз и живёт, пока работает приложение. None, если запись выключена в настройках."""
        cfg = load_settings()
        tc = cfg.get("teach", {})
        if not tc.get("enabled", True):
            return None
        if self._teacher is None:
            hermes_cfg = dict(cfg["hermes"], skills="", yolo=False, timeout_s=90)

            def sampler() -> winio.InputSampler:
                self._winapi = winio.WinApi()
                return winio.InputSampler(self._winapi)

            def grab(path: str) -> None:
                if self._winapi is not None:
                    self._winapi.grab_jpeg(path)

            self._teacher = teach.Teacher(
                sampler, ask=Hermes(hermes_cfg).run, grab=grab if tc.get("shots", True) else None,
                on_done=self._teach_done, on_state=self._teach_state,
                max_minutes=float(tc.get("max_minutes", 10)), max_shots=int(tc.get("max_shots", 60)),
                screen=lambda: self._winapi.screen_size() if self._winapi else (1920, 1080))
        return self._teacher

    def _teach_state(self, recording: bool) -> None:
        try:
            self.on_recording(recording)
        except Exception:  # noqa: BLE001
            log.exception("Сбой обработчика записи")

    def _teach_done(self, task: Any, message: str) -> None:
        if task is None and self._teacher is not None and self._teacher.last_error:
            diag.record_error("запись действий", self._teacher.last_error)
        assistant = self._assistant
        if assistant is not None:
            assistant.notify(message)
        else:
            self.on_event("reply", message)

    def run_task(self, task: Any) -> str:
        """Запускает записанную задачу через jarvis-pc и возвращает, что сказать пользователю."""
        try:
            proc = subprocess.run(backend.pc_argv() + ["task", "run", task.slug], capture_output=True, timeout=20,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            diag.record_error("запуск задачи", f"{type(exc).__name__}: {exc}")
            return "Не получилось запустить задачу. Подробности в диагностике."
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            return out or "Не получилось запустить задачу."
        return out

    def attach(self, assistant: Assistant) -> None:
        """Подключает к ассистенту запись действий и запуск задач."""
        assistant.teach = self.teacher
        assistant.task_runner = self.run_task

    # --- текстовый режим (поле на главной) -------------------------------
    def ask_text(self, text: str, speak: bool = False) -> None:
        """Отправляет просьбу агенту без микрофона. Ответ придёт событием reply."""
        def work() -> None:
            cfg = load_settings()
            hermes = Hermes(cfg["hermes"])
            speaker = Speaker(cfg["tts"])
            assistant = self._assistant or Assistant(cfg, None, speaker, hermes, NullRecorder())
            assistant.on_event = self.on_event
            if assistant.teach is None:
                self.attach(assistant)
            self.on_event("heard", text)
            from . import intents
            intent = intents.parse(text, recording=bool(assistant.teach and assistant.teach.recording))
            if intent:                       # запись действий и запуск задач обрабатываются без нейросети
                reply = assistant.handle_intent(intent)
                self.on_event("reply", reply)
                if speak:
                    assistant.rec.mute()
                    try:
                        speaker.say(reply)
                    finally:
                        assistant.rec.unmute()
                return
            ok, reply = hermes.run(assistant.build_prompt(text))
            if ok:
                assistant.remember(text, reply)
                self.on_event("reply", reply)
                if speak:
                    assistant.rec.mute()
                    try:
                        speaker.say(reply)
                    finally:
                        assistant.rec.unmute()
            else:
                self.on_event("error", reply)

        threading.Thread(target=work, name="jarvis-chat", daemon=True).start()


def prepare_background() -> None:
    """Привести окружение в порядок в фоне при старте приложения."""
    threading.Thread(target=backend.first_run_setup, name="jarvis-prepare", daemon=True).start()
