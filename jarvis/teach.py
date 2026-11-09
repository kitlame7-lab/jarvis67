"""Запись действий пользователя и превращение её в задачу.

Порядок: «Джарвис, запомни как я это делаю» → идёт запись (мышь, клавиши, окна, снимки экрана при щелчках) →
«стоп» → шаги собираются кодом (`trace.compile_steps`), нейросеть только придумывает название и фразы вызова и
отмечает случайные лишние шаги → задача сохраняется в `tasks`. Повторяет её `pc.py task play`.

Нейросеть получает описание шагов без набранного текста (только длину) и не может добавлять или менять шаги.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import tasks, trace

log = logging.getLogger("jarvis")

MAX_DROP_SHARE = 0.4        # нейросеть не может выбросить больше 40% шагов


class TeachError(Exception):
    pass


# --------------------------------------------------------------------------
# Разметка шагов нейросетью
# --------------------------------------------------------------------------

def build_prompt(steps: List[Dict[str, Any]], hint_name: str = "") -> str:
    lines = [f"{s['i']}. {trace.describe_step(s)}" for s in steps]
    hint = f"Пользователь назвал задачу: «{hint_name}». Используй это название." if hint_name else ""
    return (
        "Ты помогаешь оформить задачу, записанную по действиям пользователя на компьютере. "
        "Ниже шаги, которые код уже собрал из записи. Набранный текст скрыт намеренно.\n\n"
        + "\n".join(lines) + "\n\n" + hint +
        "\nОтветь ТОЛЬКО одним JSON-объектом без пояснений и без markdown:\n"
        '{"name": "короткое название задачи, до 6 слов",'
        ' "description": "одно предложение: что делает задача",'
        ' "phrases": ["2-4 фразы, которыми пользователь может её вызвать"],'
        ' "notes": {"номер шага": "короткая пометка, зачем он нужен"},'
        ' "drop": [номера шагов, которые похожи на случайные щелчки или лишние движения]}\n'
        "Не выдумывай шаги и не добавляй новые. Если сомневаешься, шаг не выбрасывай."
    )


def parse_annotation(text: str, count: int) -> Dict[str, Any]:
    """Достаёт и проверяет JSON из ответа. При любой странности бросает TeachError."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise TeachError("в ответе нет JSON")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise TeachError(f"JSON не разобран: {exc}") from exc
    if not isinstance(data, dict):
        raise TeachError("ответ не объект")
    name = re.sub(r"\s+", " ", str(data.get("name", ""))).strip(" .\"«»")[:60]
    if not name:
        raise TeachError("нет названия")
    phrases = [re.sub(r"\s+", " ", str(p)).strip()[:80] for p in (data.get("phrases") or []) if str(p).strip()][:5]
    notes: Dict[int, str] = {}
    raw_notes = data.get("notes") or {}
    if isinstance(raw_notes, dict):
        for k, v in raw_notes.items():
            if str(k).isdigit() and 1 <= int(k) <= count and str(v).strip():
                notes[int(k)] = str(v).strip()[:120]
    drop: List[int] = []
    for d in data.get("drop") or []:
        if isinstance(d, (int, str)) and str(d).isdigit() and 1 <= int(d) <= count and int(d) not in drop:
            drop.append(int(d))
    return {"name": name, "description": str(data.get("description", "")).strip()[:200], "phrases": phrases,
            "notes": notes, "drop": drop}


def apply_annotation(steps: List[Dict[str, Any]], ann: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Накладывает пометки и убирает лишнее. Выбросить можно только щелчки, не больше MAX_DROP_SHARE шагов."""
    droppable = {s["i"] for s in steps if s.get("kind") in ("click", "drag")}
    drop = [i for i in ann.get("drop", []) if i in droppable]
    drop = drop[:int(len(steps) * MAX_DROP_SHARE)]
    out = []
    for s in steps:
        if s["i"] in drop:
            continue
        note = ann.get("notes", {}).get(s["i"])
        if note:
            s["note"] = note
        out.append(s)
    for n, s in enumerate(out, 1):
        s["i"] = n
    return out


def fallback_name(now: Optional[datetime] = None) -> str:
    return "Задача от " + (now or datetime.now()).strftime("%d.%m %H:%M")


# --------------------------------------------------------------------------
# Рекордер
# --------------------------------------------------------------------------

class Teacher:
    """Запускает запись, по команде останавливает и собирает задачу в фоне."""

    def __init__(self, sampler_factory: Callable[[], Any], ask: Optional[Callable[[str], Tuple[bool, str]]] = None,
                 grab: Optional[Callable[[str], None]] = None, root: Optional[Path] = None,
                 on_done: Optional[Callable[[Optional[tasks.Task], str], None]] = None,
                 on_state: Optional[Callable[[bool], None]] = None,
                 max_minutes: float = 10.0, max_shots: int = 60,
                 screen: Callable[[], Tuple[int, int]] = lambda: (1920, 1080)):
        self.sampler_factory, self.ask, self.grab = sampler_factory, ask, grab
        self.root = root
        self.on_done = on_done or (lambda task, message: None)
        self.on_state = on_state or (lambda recording: None)
        self.max_seconds, self.max_shots, self.screen = max_minutes * 60.0, max_shots, screen
        self.sampler: Optional[Any] = None
        self._lock = threading.Lock()
        self._shots: Optional[ThreadPoolExecutor] = None
        self._shot_n = 0
        self._timer: Optional[threading.Timer] = None
        self._hint = ""
        self.build_thread: Optional[threading.Thread] = None
        self.last_error = ""

    # --- состояние ------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self.sampler is not None

    def stats(self) -> Dict[str, Any]:
        s = self.sampler
        if s is None:
            return {"recording": False, "seconds": 0.0, "events": 0}
        return {"recording": True, "seconds": round(s.elapsed(), 1), "events": len(s.events)}

    def _rec_dir(self) -> Path:
        return (self.root or tasks.tasks_dir()) / "_recording"

    def _set_state(self, recording: bool) -> None:
        try:
            self.on_state(recording)
        except Exception:  # noqa: BLE001 — сбой интерфейса не должен ломать запись
            log.exception("Сбой обработчика состояния записи")

    # --- запуск ---------------------------------------------------------------
    def start(self, name: str = "") -> bool:
        with self._lock:
            if self.sampler is not None:
                return False
            self.last_error = ""
            shutil.rmtree(self._rec_dir(), ignore_errors=True)
            (self._rec_dir() / "shots").mkdir(parents=True, exist_ok=True)
            try:
                sampler = self.sampler_factory()
            except OSError as exc:
                self.last_error = str(exc)
                log.error("Запись недоступна: %s", exc)
                return False
            self._hint, self._shot_n = name, 0
            if self.grab:
                self._shots = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-shot")
                sampler.listeners.append(self._on_event)
            self.sampler = sampler
            sampler.start()
            self._timer = threading.Timer(self.max_seconds, self._auto_stop)
            self._timer.daemon = True
            self._timer.start()
        self._set_state(True)
        log.info("Запись действий началась")
        return True

    def _on_event(self, ev: Dict[str, Any]) -> None:
        """Снимок экрана при каждом нажатии мыши (не больше max_shots), кроме окон самого Джарвиса."""
        if ev.get("type") != "mouse" or ev.get("action") != "down" or not self._shots:
            return
        if self._shot_n >= self.max_shots or trace._is_own(ev.get("win")):
            return
        self._shot_n += 1
        rel = f"shots/{self._shot_n:03d}.jpg"
        ev["shot"] = rel
        dest = str(self._rec_dir() / rel)
        grab = self.grab

        def job() -> None:
            try:
                grab(dest)             # type: ignore[misc]
            except Exception:  # noqa: BLE001 — снимок вторичен, запись продолжается
                log.warning("Не удалось сделать снимок экрана", exc_info=True)
        self._shots.submit(job)

    def _auto_stop(self) -> None:
        if self.recording:
            log.info("Запись остановлена по времени")
            self.stop(auto=True)

    # --- остановка ------------------------------------------------------------
    def discard(self) -> bool:
        with self._lock:
            sampler, self.sampler = self.sampler, None
            self._cancel_timer()
        if sampler is None:
            return False
        sampler.stop()
        self._finish_shots()
        shutil.rmtree(self._rec_dir(), ignore_errors=True)
        self._set_state(False)
        log.info("Запись отменена")
        return True

    def stop(self, name: str = "", auto: bool = False) -> bool:
        """Останавливает запись и в фоне собирает задачу. Итог придёт в on_done."""
        with self._lock:
            sampler, self.sampler = self.sampler, None
            self._cancel_timer()
        if sampler is None:
            return False
        events = sampler.stop()
        self._set_state(False)
        hint = name or self._hint
        try:
            screen = self.screen()
        except Exception:  # noqa: BLE001 — размер экрана нужен лишь для запасных координат
            screen = (1920, 1080)
        self.build_thread = threading.Thread(target=self._build, args=(events, screen, hint, auto),
                                             name="jarvis-teach-build", daemon=True)
        self.build_thread.start()
        return True

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _finish_shots(self) -> None:
        pool, self._shots = self._shots, None
        if pool:
            pool.shutdown(wait=True)

    # --- сборка ---------------------------------------------------------------
    def _build(self, events: List[Dict[str, Any]], screen: Tuple[int, int], hint: str, auto: bool) -> None:
        try:
            self._finish_shots()
            steps = trace.compile_steps(events, screen)
            if not [s for s in steps if s["kind"] != "window"]:
                shutil.rmtree(self._rec_dir(), ignore_errors=True)
                self.on_done(None, "Я ничего не увидел: не было ни щелчков, ни набранного текста.")
                return
            ann = self._annotate(steps, hint)
            steps = apply_annotation(steps, ann)
            task = tasks.Task(name=ann["name"], description=ann.get("description", ""), phrases=ann.get("phrases", []),
                              steps=steps, screen=list(screen))
            tasks.save_task(task, shots_from=self._rec_dir() / "shots", root=self.root)
            shutil.rmtree(self._rec_dir(), ignore_errors=True)
            note = " Запись остановлена по времени." if auto else ""
            self.on_done(task, f"Задача «{task.name}» готова: {len(steps)} шагов.{note}")
        except Exception as exc:  # noqa: BLE001 — любой сбой должен дойти до пользователя, а не пропасть в потоке
            log.exception("Не удалось собрать задачу")
            self.last_error = str(exc)
            self.on_done(None, "Не получилось сохранить задачу. Подробности в диагностике.")

    def _annotate(self, steps: List[Dict[str, Any]], hint: str) -> Dict[str, Any]:
        """Название и пометки от нейросети; если не вышло, то безопасные значения по умолчанию."""
        default = {"name": hint or fallback_name(), "description": "", "phrases": [], "notes": {}, "drop": []}
        if not self.ask:
            return default
        prompt = build_prompt(steps, hint)
        for attempt in (1, 2):
            ok, reply = self.ask(prompt)
            if not ok:
                log.warning("Нейросеть не разметила задачу: %s", reply[:200])
                return default
            try:
                ann = parse_annotation(reply, len(steps))
            except TeachError as exc:
                log.warning("Разметка не принята (попытка %s): %s", attempt, exc)
                prompt += "\n\nПрошлый ответ не подошёл. Нужен только корректный JSON."
                continue
            if hint:
                ann["name"] = hint
            return ann
        return default
