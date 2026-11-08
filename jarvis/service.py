"""Жизненный цикл ассистента внутри приложения: загрузка модели речи, микрофон, поток прослушивания.
Не зависит от Qt: окно подписывается на два обратных вызова и ничего больше не знает."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from . import backend
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


class AssistantService:
    def __init__(self, on_state: Callable[[str, str], None] = lambda s, d: None,
                 on_event: Callable[[str, str], None] = lambda k, t: None):
        self.on_state, self.on_event = on_state, on_event
        self.state = STOPPED
        self.detail = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._recorder: Optional[Recorder] = None
        self._assistant: Optional[Assistant] = None
        self._lock = threading.RLock()
        self._stt: Optional[Transcriber] = None
        self._stt_key: Any = None

    # --- состояние ---------------------------------------------------------
    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        try:
            self.on_state(state, detail)
        except Exception:  # noqa: BLE001
            log.exception("Сбой обработчика состояния")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- запуск и остановка ------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, name="jarvis-service", daemon=True)
            self._thread.start()

    def stop(self, wait: float = 5.0) -> None:
        with self._lock:
            self._stop.set()
            rec = self._recorder
            if rec is not None:
                rec.stop.set()
            th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(wait)
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

    # --- основной цикл -----------------------------------------------------
    def _transcriber(self, cfg: Dict[str, Any]) -> Transcriber:
        key = tuple(sorted((k, str(v)) for k, v in cfg["stt"].items()))
        if self._stt is None or self._stt_key != key:
            self._stt = Transcriber(cfg["stt"])
            self._stt_key = key
        return self._stt

    def _run(self) -> None:
        stop = self._stop
        try:
            cfg = load_settings()
            self._set(LOADING, "Загружаю распознавание речи")
            stt = self._transcriber(cfg)
            stt.load()
            speaker = Speaker(cfg["tts"])
            hermes = Hermes(cfg["hermes"])
            if not hermes.exe:
                self.on_event("error", "Hermes не найден. Откройте раздел «Система».")
            greeted = False
            while not stop.is_set():
                recorder: Optional[Recorder] = None
                try:
                    recorder = Recorder(cfg["audio"])
                    recorder.open()
                    self._recorder = recorder
                    if cfg["audio"].get("auto_threshold", True):
                        self._set(LOADING, "Замеряю шум в комнате")
                        recorder.cfg["silence_threshold"] = recorder.calibrate()
                        log.info("Порог тишины: %.4f", recorder.cfg["silence_threshold"])
                    assistant = Assistant(cfg, stt, speaker, hermes, recorder)
                    assistant.on_event = self.on_event
                    self._assistant = assistant
                    self._set(LISTENING)
                    if not greeted and cfg.get("greeting", True):
                        assistant.greet()
                    greeted = True
                    log.info("Слушаю. Скажите «%s».", cfg["name"])
                    assistant.run(stop)
                except Exception as exc:  # noqa: BLE001 — микрофон отключили, пропал звук: пробуем снова
                    log.exception("Сбой основного цикла, повтор через 5 секунд")
                    self._set(ERROR, f"{type(exc).__name__}: {exc}")
                    self.on_event("error", f"Микрофон или звук: {exc}")
                    stop.wait(5)
                finally:
                    if recorder is not None:
                        try:
                            recorder.close()
                        except Exception:  # noqa: BLE001
                            pass
                    self._recorder = None
        except Exception as exc:  # noqa: BLE001
            log.exception("Ассистент не запустился")
            self._set(ERROR, f"{type(exc).__name__}: {exc}")
            self.on_event("error", f"Не удалось запуститься: {exc}")
            return
        self._set(STOPPED)

    # --- текстовый режим (вкладка «Чат») ----------------------------------
    def ask_text(self, text: str, speak: bool = False) -> None:
        """Отправляет просьбу агенту без микрофона. Ответ придёт событием reply."""
        def work() -> None:
            cfg = load_settings()
            hermes = Hermes(cfg["hermes"])
            speaker = Speaker(cfg["tts"])
            assistant = self._assistant or Assistant(cfg, None, speaker, hermes, NullRecorder())
            assistant.on_event = self.on_event
            self.on_event("heard", text)
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
