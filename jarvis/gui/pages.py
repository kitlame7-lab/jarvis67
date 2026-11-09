"""Страницы: Главное, Модель (часть «Подключений»), Голос, Слух, Возможности."""
from __future__ import annotations

from typing import Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton, QSlider, QTableWidget, QTableWidgetItem,
                               QWidget)

from .. import backend, service
from ..core import Hermes, list_voices, load_settings
from . import theme
from .common import Card, Page, SecretEdit, Status, background, field, note


def primary(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("primary")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def button(text: str, name: str = "") -> QPushButton:
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def set_combo(box: QComboBox, data: str, fallback_text: str = "") -> None:
    """Выбирает пункт по данным; если такого нет — добавляет, чтобы значение не потерялось."""
    i = box.findData(data)
    if i < 0 and data:
        box.addItem(fallback_text or data, data)
        i = box.findData(data)
    box.setCurrentIndex(max(i, 0))


# --------------------------------------------------------------------------
# Главное
# --------------------------------------------------------------------------

class HomePage(Page):
    title = "Главное"
    lead = "Скажите «Джарвис» и попросите что угодно. Или напишите просьбу ниже: это то же самое, только текстом."

    def build(self) -> None:
        card = Card()
        top = QHBoxLayout()
        self.dot = QLabel()
        self.dot.setPixmap(theme.dot(theme.MUTED, 18))
        self.state = QLabel("Остановлен")
        self.state.setStyleSheet("font-size: 20px; font-weight: 600;")
        top.addWidget(self.dot)
        top.addWidget(self.state)
        top.addStretch(1)
        card.lay.addLayout(top)
        self.detail = note("")
        card.add(self.detail)
        self.btn_start = primary("Запустить")
        self.btn_pause = button("Пауза")
        self.btn_restart = button("Перезапустить")
        self.btn_stop = button("Остановить", "danger")
        card.row(self.btn_start, self.btn_pause, self.btn_restart, self.btn_stop)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.hide()
        card.add(self.progress)
        self.col.addWidget(card)

        ask = Card("Написать Джарвису")
        row = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Например: найди файл «отчёт» или выполни задачу «утренний запуск»")
        self.btn_send = primary("Отправить")
        row.addWidget(self.cmd, 1)
        row.addWidget(self.btn_send)
        wrap = QWidget()
        wrap.setLayout(row)
        ask.add(wrap)
        self.speak = QCheckBox("Озвучивать ответ")
        ask.add(self.speak)
        self.col.addWidget(ask)
        self.busy = False

        talk = Card("Последний разговор")
        self.heard = QLabel("—")
        self.heard.setWordWrap(True)
        self.reply = QLabel("—")
        self.reply.setWordWrap(True)
        talk.add(field("Вы сказали", self.heard))
        talk.add(field("Джарвис ответил", self.reply))
        self.col.addWidget(talk)

        opts = Card("Запуск")
        self.autostart = QCheckBox("Запускать Джарвиса вместе с Windows (в трее, без окна)")
        self.greeting = QCheckBox("Здороваться голосом при запуске")
        opts.add(self.autostart)
        opts.add(self.greeting)
        self.opt_status = Status()
        opts.add(self.opt_status)
        self.col.addWidget(opts)

        self.btn_start.clicked.connect(self.ctx.service.start)
        self.btn_stop.clicked.connect(self.ctx.service.stop)
        self.btn_restart.clicked.connect(self.ctx.restart_service)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_send.clicked.connect(self.submit)
        self.cmd.returnPressed.connect(self.submit)
        self.autostart.toggled.connect(self.set_autostart)
        self.greeting.toggled.connect(self.set_greeting)
        self._loading = False
        self.on_state(self.ctx.service.state, "")

    def submit(self) -> None:
        text = self.cmd.text().strip()
        if not text or self.busy:
            return
        self.cmd.clear()
        self._set_busy(True)
        self.ctx.service.ask_text(text, speak=self.speak.isChecked())

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.btn_send.setEnabled(not busy)
        self.btn_send.setText("Думаю…" if busy else "Отправить")

    def on_progress(self, fraction: float, text: str) -> None:
        shown = 0.0 <= fraction <= 1.0
        self.progress.setVisible(shown)
        if shown:
            self.progress.setValue(int(fraction * 1000))

    def toggle_pause(self) -> None:
        self.ctx.service.set_paused(self.ctx.service.state != service.PAUSED)

    def set_autostart(self, on: bool) -> None:
        if self._loading:
            return
        try:
            backend.set_autostart(on)
            self.opt_status.ok("Автозапуск включён." if on else "Автозапуск выключен.")
        except Exception as exc:  # noqa: BLE001
            self.opt_status.bad(f"Не удалось изменить автозапуск: {exc}")

    def set_greeting(self, on: bool) -> None:
        if self._loading:
            return
        backend.save_settings({"greeting": on})
        self.opt_status.ok("Сохранено.")

    def load(self) -> None:
        self._loading = True
        self.autostart.setChecked(backend.autostart_enabled())
        self.greeting.setChecked(bool(load_settings()["greeting"]))
        self._loading = False

    def on_state(self, state: str, detail: str) -> None:
        self.dot.setPixmap(theme.dot(theme.STATE_COLOR.get(state, theme.MUTED), 18))
        self.state.setText(service.STATE_TEXT.get(state, state))
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        running = state in (service.LISTENING, service.PAUSED, service.LOADING, service.ERROR)
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_restart.setEnabled(running)
        self.btn_pause.setEnabled(state in (service.LISTENING, service.PAUSED))
        self.btn_pause.setText("Продолжить" if state == service.PAUSED else "Пауза")

    def on_event(self, kind: str, text: str) -> None:
        if kind == "heard":
            self.heard.setText(text)
        elif kind == "reply":
            self.reply.setText(text)
            self._set_busy(False)
        elif kind == "error":
            self.reply.setText(f"⚠ {text}")
            self._set_busy(False)


# --------------------------------------------------------------------------
# Модель
# --------------------------------------------------------------------------

class ModelPage(Page):
    title = "Модель"
    lead = ("Мозг Джарвиса — Hermes. Здесь выбирается, какая нейросеть ему отвечает. "
            "Ключ и адрес сохраняются в самом Hermes, а в чат их вставлять не нужно.")

    def build(self) -> None:
        self.snap: Dict = {}
        card = Card("Откуда брать модель")
        self.provider = QComboBox()
        for p in backend.PROVIDERS:
            self.provider.addItem(p["label"], p["id"])
        self.provider.addItem("Другой провайдер Hermes…", "__other__")
        card.add(field("Провайдер", self.provider))
        self.provider_hint = note("")
        card.add(self.provider_hint)
        self.col.addWidget(card)

        self.box = Card("Подключение")
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://api.example.ru/v1")
        self.url_row = field("Base URL", self.url, "Адрес сервера, обычно заканчивается на /v1. "
                             "Если вставите ссылку целиком до /chat/completions, лишнее отрежется.")
        self.box.add(self.url_row)
        self.other_row = QWidget()
        orow = QHBoxLayout(self.other_row)
        orow.setContentsMargins(0, 0, 0, 0)
        self.other_id = QLineEdit()
        self.other_id.setPlaceholderText("например, my-provider")
        self.other_env = QLineEdit()
        self.other_env.setPlaceholderText("MY_PROVIDER_API_KEY")
        orow.addWidget(field("Идентификатор провайдера в Hermes", self.other_id))
        orow.addWidget(field("Переменная для ключа", self.other_env))
        self.box.add(self.other_row)
        self.key = SecretEdit("sk-…")
        self.key_row = field("API Key", self.key)
        self.key_state = note("")
        self.box.add(self.key_row)
        self.box.add(self.key_state)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_row = field("Model", self.model, "Название модели ровно так, как его называет сервер.")
        self.box.add(self.model_row)
        self.btn_list = button("Показать модели сервера")
        self.box.row(self.btn_list)
        self.col.addWidget(self.box)

        act = Card()
        self.btn_save = button("Сохранить")
        self.btn_check = primary("Сохранить и проверить")
        act.row(self.btn_save, self.btn_check)
        self.status = Status()
        act.add(self.status)
        self.col.addWidget(act)

        warn = Card("О сторонних сайтах с ключами")
        warn.add(note("Если ключ куплен у перепродавца, ваши запросы идут через его сервер: он технически может их читать. "
                      "Не диктуйте Джарвису пароли и личные данные. Если ключ перестанет работать, проверьте баланс на сайте "
                      "продавца."))
        self.col.addWidget(warn)

        self.provider.currentIndexChanged.connect(self.refresh_visibility)
        self.btn_save.clicked.connect(lambda: self.save(False))
        self.btn_check.clicked.connect(lambda: self.save(True))
        self.btn_list.clicked.connect(self.list_models)

    # --- состояние ------------------------------------------------------
    def pid(self) -> str:
        return self.provider.currentData() or ""

    def refresh_visibility(self) -> None:
        pid = self.pid()
        info = backend.provider_by_id(pid) or {}
        follow = pid == ""
        self.box.setVisible(not follow)
        self.url_row.setVisible(pid == "custom")
        self.other_row.setVisible(pid == "__other__")
        self.btn_list.setVisible(pid == "custom")
        self.provider_hint.setText(info.get("hint", "") if pid != "__other__" else
                                   "Для провайдеров, которых нет в списке. Идентификатор и переменную ключа смотрите в документации Hermes.")
        if follow and self.snap:
            now = self.snap["now"]
            self.provider_hint.setText(
                f"Сейчас в Hermes: {now['provider'] or 'провайдер не задан'}"
                f"{' / ' + now['model'] if now['model'] else ''}. Джарвис ничего не переопределяет.")
        if not follow:
            models = (info.get("models") or []) if pid != "custom" else []
            cur = self.model.currentText()
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItems(models)
            self.model.setEditText(cur)
            self.model.blockSignals(False)
            self.update_key_state()

    def update_key_state(self) -> None:
        pid = self.pid()
        if not self.snap or pid in ("", "__other__"):
            self.key_state.setText("")
            return
        if pid == "custom":
            mask = self.snap["custom_key"]
        else:
            mask = self.snap["keys"].get(backend.provider_by_id(pid)["env"], {"set": False, "tail": ""})
        self.key_state.setText(self.key.describe(mask))

    def load(self) -> None:
        self.snap = backend.hermes_snapshot()
        cfg = load_settings()
        override, now = cfg["hermes"].get("provider", ""), self.snap["now"]
        self.provider.blockSignals(True)
        if now["provider"] == "custom" and not override:
            set_combo(self.provider, "custom")
            self.url.setText(now["base_url"])
            self.refresh_visibility()
            self.model.setEditText(now["model"])
        elif override:
            if backend.provider_by_id(override) is None:
                set_combo(self.provider, "__other__")
                self.other_id.setText(override)
            else:
                set_combo(self.provider, override)
            self.refresh_visibility()
            self.model.setEditText(cfg["hermes"].get("model", ""))
        else:
            set_combo(self.provider, "")
            self.refresh_visibility()
        self.provider.blockSignals(False)
        self.refresh_visibility()
        self.update_key_state()

    # --- действия -------------------------------------------------------
    def stored_or_typed_key(self) -> str:
        typed = self.key.value()
        if typed:
            return typed
        return backend.stored_key(backend.CUSTOM_KEY_ENV)

    def list_models(self) -> None:
        self.status.info("Запрашиваю список моделей…")
        url, key = self.url.text(), self.stored_or_typed_key()

        def done(models, err):
            if err:
                self.status.bad(str(err))
                return
            cur = self.model.currentText()
            self.model.clear()
            self.model.addItems(models)
            self.model.setEditText(cur or (models[0] if models else ""))
            self.status.ok(f"Моделей на сервере: {len(models)}. Выберите нужную из списка.")
        background(self.ctx.bus, lambda: backend.list_endpoint_models(url, key), done)

    def save(self, check: bool) -> None:
        pid = self.pid()
        try:
            backend.save_model_choice(
                pid, self.model.currentText(), api_key=self.key.value(), base_url=self.url.text(),
                custom_provider=self.other_id.text(), custom_env=self.other_env.text())
        except ValueError as exc:
            self.status.bad(str(exc))
            return
        self.key.clear()
        self.snap = backend.hermes_snapshot()
        self.update_key_state()
        self.ctx.restart_service_soon()
        if not check:
            self.status.ok("Сохранено. Hermes подхватит изменения при следующей просьбе.")
            return
        self.run_check(pid)

    def run_check(self, pid: str) -> None:
        for b in (self.btn_save, self.btn_check):
            b.setEnabled(False)
        url, key, model = self.url.text(), self.stored_or_typed_key(), self.model.currentText()
        self.status.info("Проверяю… Это может занять до полутора минут.")

        def work():
            lines: List[str] = []
            if pid == "custom":
                reply = backend.probe_endpoint(url, key, model)       # сначала сам сервер
                lines.append(f"Сервер отвечает: «{reply}».")
            cfg = load_settings()
            ok, text = Hermes(dict(cfg["hermes"], timeout_s=90)).run("Ответь одним словом: готов.")
            if not ok:
                raise RuntimeError(("\n".join(lines) + "\n" if lines else "") + "Hermes: " + text)
            lines.append(f"Hermes отвечает: «{text[:80]}».")
            return "\n".join(lines)

        def done(result, err):
            for b in (self.btn_save, self.btn_check):
                b.setEnabled(True)
            if err:
                self.status.bad(str(err))
            else:
                self.status.ok(str(result) + "\nВсё работает.")
        background(self.ctx.bus, work, done)


# --------------------------------------------------------------------------
# Голос
# --------------------------------------------------------------------------

class VoicePage(Page):
    title = "Голос"
    lead = ("Джарвис говорит нейроголосом Microsoft: он бесплатный, хорошо читает по-русски и не требует ключа. "
            "Нужен интернет; без него включится обычный голос Windows.")

    def build(self) -> None:
        card = Card("Голос")
        self.enabled = QCheckBox("Озвучивать ответы")
        card.add(self.enabled)
        self.voice = QComboBox()
        card.add(field("Голос", self.voice))
        self.backend_box = QComboBox()
        self.backend_box.addItem("Нейроголос Microsoft, при сбое голос Windows", "auto")
        self.backend_box.addItem("Только нейроголос Microsoft", "edge")
        self.backend_box.addItem("Только голос Windows (без интернета)", "sapi")
        card.add(field("Источник", self.backend_box))
        self.rate = QSlider(Qt.Orientation.Horizontal)
        self.rate.setRange(-50, 50)
        self.rate_lab = QLabel("")
        self.pitch = QSlider(Qt.Orientation.Horizontal)
        self.pitch.setRange(-50, 50)
        self.pitch_lab = QLabel("")
        for sl, lab, title in ((self.rate, self.rate_lab, "Скорость"), (self.pitch, self.pitch_lab, "Высота")):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(sl, 1)
            lab.setMinimumWidth(60)
            h.addWidget(lab)
            card.add(field(title, row))
        self.rate.valueChanged.connect(lambda v: self.rate_lab.setText(f"{v:+d}%"))
        self.pitch.valueChanged.connect(lambda v: self.pitch_lab.setText(f"{v:+d} Гц"))
        self.col.addWidget(card)

        who = Card("Обращение")
        self.name = QLineEdit()
        self.address = QLineEdit()
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(field("Имя ассистента", self.name))
        h.addWidget(field("Как к вам обращаться", self.address))
        who.add(row)
        self.col.addWidget(who)

        act = Card()
        self.btn_preview = button("Прослушать")
        self.btn_save = primary("Сохранить")
        act.row(self.btn_preview, self.btn_save)
        self.status = Status()
        act.add(self.status)
        self.col.addWidget(act)

        self.btn_preview.clicked.connect(self.preview)
        self.btn_save.clicked.connect(self.save)
        self._voices_loaded = False

    def load(self) -> None:
        cfg = load_settings()
        t = cfg["tts"]
        self.enabled.setChecked(bool(t["enabled"]))
        set_combo(self.backend_box, t["backend"] if t["backend"] in ("auto", "edge", "sapi") else "auto")
        self.rate.setValue(self._num(t["rate"]))
        self.pitch.setValue(self._num(t["pitch"]))
        self.rate_lab.setText(f"{self.rate.value():+d}%")
        self.pitch_lab.setText(f"{self.pitch.value():+d} Гц")
        self.name.setText(cfg["name"])
        self.address.setText(cfg["address"])
        self.current_voice = t["voice"]
        if not self._voices_loaded:
            self._voices_loaded = True
            self.voice.addItem("Загружаю список голосов…", self.current_voice)
            background(self.ctx.bus, list_voices, self.fill_voices)
        else:
            set_combo(self.voice, self.current_voice)

    @staticmethod
    def _num(text: str) -> int:
        try:
            return int(str(text).replace("%", "").replace("Hz", "").replace("+", ""))
        except ValueError:
            return 0

    def fill_voices(self, voices, err) -> None:
        self.voice.clear()
        for v in voices or []:
            self.voice.addItem(v["label"], v["name"])
        set_combo(self.voice, self.current_voice)

    def collect(self) -> Dict:
        return {"enabled": self.enabled.isChecked(), "backend": self.backend_box.currentData(),
                "voice": self.voice.currentData() or self.current_voice,
                "rate": f"{self.rate.value():+d}%", "pitch": f"{self.pitch.value():+d}Hz"}

    def preview(self) -> None:
        tts = dict(load_settings()["tts"], **self.collect())
        text = f"Добрый день, {self.address.text() or 'сэр'}. {self.name.text() or 'Джарвис'} к вашим услугам."
        self.btn_preview.setEnabled(False)
        self.status.info("Синтезирую…")

        def done(result, err):
            self.btn_preview.setEnabled(True)
            self.status.bad(str(err)) if err else self.status.ok(str(result))
        background(self.ctx.bus, lambda: backend.preview_voice(tts, text), done)

    def save(self) -> None:
        data = {"tts": self.collect(), "name": self.name.text(), "address": self.address.text()}
        backend.save_settings(data)
        self.ctx.restart_service_soon()
        self.status.ok("Сохранено. Джарвис перезапускается с новым голосом.")


# --------------------------------------------------------------------------
# Слух
# --------------------------------------------------------------------------

STT_MODELS = [
    ("tiny", "tiny — самая быстрая, ошибается чаще"),
    ("base", "base — быстрая, для слабых компьютеров"),
    ("small", "small — рекомендуется: хорошо понимает русский"),
    ("medium", "medium — точнее, нужен мощный процессор"),
    ("large-v3", "large-v3 — самая точная, очень тяжёлая"),
]


class HearingPage(Page):
    title = "Слух"
    lead = "Какой микрофон слушать, как Джарвис понимает речь и по каким словам просыпается."

    def build(self) -> None:
        mic = Card("Микрофон")
        self.device = QComboBox()
        mic.add(field("Устройство", self.device))
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        mic.add(self.level)
        self.btn_mic = button("Проверить микрофон (2 секунды)")
        mic.row(self.btn_mic)
        self.mic_status = Status()
        mic.add(self.mic_status)
        self.auto = QCheckBox("Автоматически подбирать чувствительность по шуму в комнате")
        mic.add(self.auto)
        self.thr = QSlider(Qt.Orientation.Horizontal)
        self.thr.setRange(2, 120)       # в тысячных долях
        self.thr_lab = QLabel("")
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.thr, 1)
        h.addWidget(self.thr_lab)
        self.thr_row = field("Порог тишины (ниже — тише, чувствительнее)", row)
        mic.add(self.thr_row)
        self.thr.valueChanged.connect(lambda v: self.thr_lab.setText(f"{v / 1000:.3f}"))
        self.auto.toggled.connect(lambda on: self.thr_row.setEnabled(not on))
        self.col.addWidget(mic)

        stt = Card("Распознавание речи", "Работает прямо на вашем компьютере. Первый запуск скачивает модель.")
        self.stt = QComboBox()
        for key, label in STT_MODELS:
            self.stt.addItem(label, key)
        stt.add(field("Модель", self.stt))
        self.col.addWidget(stt)

        wake = Card("Слово вызова")
        self.words = QLineEdit()
        wake.add(field("Слова через запятую", self.words,
                       "Первые два — основное имя, остальное — как Whisper иногда слышит его по ошибке."))
        self.follow = QDoubleSpinBox()
        self.follow.setRange(0, 60)
        self.follow.setSuffix(" с")
        self.follow.setDecimals(0)
        wake.add(field("После ответа можно продолжать без имени", self.follow))
        self.col.addWidget(wake)

        act = Card()
        self.btn_save = primary("Сохранить")
        act.row(self.btn_save)
        self.status = Status()
        act.add(self.status)
        self.col.addWidget(act)

        self.btn_mic.clicked.connect(self.test_mic)
        self.btn_save.clicked.connect(self.save)

    def load(self) -> None:
        cfg = load_settings()
        a = cfg["audio"]
        self.device.clear()
        self.device.addItem("Устройство по умолчанию", "")
        devices = backend.list_input_devices()
        for d in devices:
            self.device.addItem(d["label"], d["id"])
        if not devices:
            self.device.addItem("Микрофоны не найдены", "")
        set_combo(self.device, str(a.get("device") or ""), f"Устройство {a.get('device')}")
        self.auto.setChecked(bool(a["auto_threshold"]))
        self.thr.setValue(int(float(a["silence_threshold"]) * 1000))
        self.thr_row.setEnabled(not self.auto.isChecked())
        set_combo(self.stt, cfg["stt"]["model"])
        self.words.setText(", ".join(cfg["wake"]["words"]))
        self.follow.setValue(float(cfg["wake"]["followup_seconds"]))

    def test_mic(self) -> None:
        self.btn_mic.setEnabled(False)
        self.mic_status.info("Скажите что-нибудь…")
        dev = self.device.currentData() or ""

        def done(res, err):
            self.btn_mic.setEnabled(True)
            if err:
                self.mic_status.bad(f"Микрофон не отвечает: {err}")
                return
            peak = res["peak"]
            self.level.setValue(min(100, int(peak * 100 * 3)))
            if peak < 0.01:
                self.mic_status.warn("Почти тишина. Проверьте, не выключен ли микрофон и выбрано ли нужное устройство.")
            elif peak > 0.95:
                self.mic_status.warn("Слишком громко, звук искажается. Уменьшите усиление микрофона в Windows.")
            else:
                self.mic_status.ok("Микрофон работает.")
        background(self.ctx.bus, lambda: backend.mic_level(dev), done)

    def save(self) -> None:
        words = [w.strip() for w in self.words.text().split(",") if w.strip()]
        if not words:
            self.status.bad("Нужно хотя бы одно слово вызова.")
            return
        backend.save_settings({
            "audio": {"device": self.device.currentData() or "", "auto_threshold": self.auto.isChecked(),
                      "silence_threshold": self.thr.value() / 1000},
            "stt": {"model": self.stt.currentData()},
            "wake": {"words": words, "followup_seconds": self.follow.value()}})
        self.ctx.restart_service_soon()
        self.status.ok("Сохранено. Джарвис перезапускается.")


# --------------------------------------------------------------------------
# Возможности
# --------------------------------------------------------------------------

class AbilitiesPage(Page):
    title = "Возможности"
    lead = "Погода, игровой режим, запуск программ и права Джарвиса на вашем компьютере."

    def build(self) -> None:
        city = Card("Погода")
        self.city = QLineEdit()
        city.add(field("Город по умолчанию", self.city))
        self.col.addWidget(city)

        game = Card("Игровой режим", "Включает высокую производительность и закрывает программы из списка. Больше ничего.")
        self.kill = QPlainTextEdit()
        self.kill.setPlaceholderText("Discord.exe\nchrome.exe")
        self.kill.setFixedHeight(110)
        game.add(field("Закрывать при включении (по одному имени файла в строке)", self.kill))
        self.col.addWidget(game)

        apps = Card("Свои программы", "Что Джарвис откроет по слову «открой …».")
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Как называть", "Что запускать (путь или ссылка)"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(180)
        apps.add(self.table)
        self.btn_add = button("Добавить строку")
        self.btn_del = button("Удалить выбранную")
        apps.row(self.btn_add, self.btn_del)
        self.col.addWidget(apps)

        perm = Card("Права")
        self.yolo = QCheckBox("Выполнять действия без подтверждения (режим yolo)")
        perm.add(self.yolo)
        perm.add(note("Без подтверждения Hermes может удалять и менять файлы по голосовой просьбе, в том числе по ошибке "
                      "распознавания. Включайте, только если понимаете риск."))
        self.col.addWidget(perm)

        act = Card()
        self.btn_save = primary("Сохранить")
        act.row(self.btn_save)
        self.status = Status()
        act.add(self.status)
        self.col.addWidget(act)

        self.btn_add.clicked.connect(lambda: self.table.insertRow(self.table.rowCount()))
        self.btn_del.clicked.connect(self.remove_row)
        self.btn_save.clicked.connect(self.save)

    def remove_row(self) -> None:
        for idx in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(idx)

    def load(self) -> None:
        pub = backend.public_settings()
        self.city.setText(pub["city"])
        self.kill.setPlainText("\n".join(pub["game_mode"]["kill"]))
        self.table.setRowCount(0)
        for name, cmd in sorted(pub["apps"].items()):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(name))
            self.table.setItem(r, 1, QTableWidgetItem(cmd))
        self.yolo.setChecked(bool(pub["hermes"]["yolo"]))

    def save(self) -> None:
        apps = {}
        for r in range(self.table.rowCount()):
            a, b = self.table.item(r, 0), self.table.item(r, 1)
            if a and b and a.text().strip() and b.text().strip():
                apps[a.text().strip().lower()] = b.text().strip()
        kill = [x.strip() for x in self.kill.toPlainText().splitlines() if x.strip()]
        backend.save_settings({"city": self.city.text(), "game_mode": {"kill": kill}, "apps": apps,
                               "hermes": {"yolo": self.yolo.isChecked()}})
        self.ctx.restart_service_soon()
        self.status.ok("Сохранено.")

