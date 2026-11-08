"""Проверка и установка обновлений для окна: одно состояние на всё приложение."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

from .. import __version__, updater
from ..core import log
from .common import UiBus, background


class UpdateController(QObject):
    changed = Signal()
    quit_requested = Signal()

    def __init__(self, bus: UiBus):
        super().__init__()
        self.bus = bus
        self.release: Optional[updater.Release] = None
        self.text = f"Версия {__version__}"
        self.error = False
        self.busy = False
        self.progress = (0, 0)
        self._manual_wanted = False

    def _set(self, text: str, error: bool = False) -> None:
        self.text, self.error = text, error
        self.changed.emit()

    def check(self, manual: bool = False) -> None:
        if self.busy:
            # Фоновая проверка уже идёт: щелчок пользователя не теряем, итог покажем как после ручной
            if manual:
                self._manual_wanted = True
                self._set("Проверяю обновления…")
            return
        self.busy = True
        self._manual_wanted = manual
        if manual:
            self._set("Проверяю обновления…")

        def done(rel, err):
            self.busy = False
            shown = self._manual_wanted
            if err:
                log.warning("Проверка обновлений: %s", err)
                self._set(f"Не удалось проверить: {err}" if shown else self.text, bool(shown))
                return
            self.release = rel
            if rel:
                self._set(f"Доступна версия {rel.version}")
            else:
                self._set(f"У вас последняя версия ({__version__})")
        background(self.bus, updater.check, done)

    def install(self) -> None:
        rel = self.release
        if rel is None or self.busy:
            return
        self.busy = True
        self._set(f"Скачиваю версию {rel.version}…")

        def work():
            def progress(done: int, total: int) -> None:
                self.bus.post(lambda: self._progress(done, total))
            path = updater.download(rel, progress)
            updater.launch_installer(path)
            return path

        def done(_path, err):
            self.busy = False
            if err:
                log.warning("Установка обновления: %s", err)
                self._set(f"Не удалось обновить: {err}", True)
                return
            self._set("Устанавливаю… Джарвис перезапустится сам.")
            self.quit_requested.emit()
        background(self.bus, work, done)

    def _progress(self, done: int, total: int) -> None:
        self.progress = (done, total)
        pct = f" {done * 100 // total}%" if total else ""
        self._set(f"Скачиваю версию {self.release.version if self.release else ''}…{pct}")


def auto_check_enabled() -> bool:
    from ..core import load_settings
    return bool(load_settings()["updates"]["auto_check"])



