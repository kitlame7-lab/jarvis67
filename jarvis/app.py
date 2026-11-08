"""Точка входа: окно, трей, один экземпляр, ключи командной строки."""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__, paths


def _attach_console() -> None:
    """Окно-программа без консоли: для --selftest подключаемся к консоли, из которой запустили."""
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):  # type: ignore[attr-defined]
            sys.stdout = open("CONOUT$", "w", encoding="utf-8")
            sys.stderr = sys.stdout


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="Jarvis", description="Голосовой помощник Джарвис")
    p.add_argument("--minimized", action="store_true", help="запуск в трей без окна (так стартует с Windows)")
    p.add_argument("--selftest", action="store_true", help="проверить всю цепочку и выйти")
    p.add_argument("--smoke", action="store_true",
                   help="проверить библиотеки, создать окно и выйти; результат в smoke.txt (проверка сборки)")
    p.add_argument("--no-voice", action="store_true", help="не запускать прослушивание")
    p.add_argument("--version", action="store_true")
    return p.parse_args(argv)


SMOKE_MODULES = ("numpy", "sounddevice", "faster_whisper", "ctranslate2", "edge_tts", "PySide6.QtWidgets")


def smoke_check() -> List[str]:
    """Пробует импортировать всё, что нужно в работе. Так сборка без нужной библиотеки замечается сразу."""
    import importlib
    failed = []
    for name in SMOKE_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    for rel in ("jarvis-pc/SKILL.md", "jarvis-pc/scripts/pc.py"):
        if not (paths.RES_DIR / rel).exists():
            failed.append(f"нет файла {rel} в {paths.RES_DIR}")
    return failed


def server_name() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"Jarvis-{user}"


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.version:
        _attach_console()
        print(__version__)
        return 0

    from .core import load_settings, log, run_selftest, setup_logging
    if args.smoke:
        failed = smoke_check()
        paths.ensure_dirs()
        (paths.DATA_DIR / "smoke.txt").write_text("\n".join(failed) or "ok", encoding="utf-8")
        if failed:
            return 2
    if args.selftest:
        _attach_console()
        setup_logging(console=True)
        return run_selftest(load_settings())

    from PySide6.QtCore import QTimer
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    setup_logging()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Jarvis")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)

    # Один экземпляр: второй запуск лишь просит первый показать окно
    sock = QLocalSocket()
    sock.connectToServer(server_name())
    if sock.waitForConnected(400) and not args.smoke:
        sock.write(b"show")
        sock.waitForBytesWritten(500)
        log.info("Джарвис уже запущен, показываю его окно")
        return 0
    QLocalServer.removeServer(server_name())
    server = QLocalServer()
    server.listen(server_name())

    from . import service
    from .gui import theme
    from .gui.window import Context, MainWindow, shutdown

    app.setStyleSheet(theme.QSS + theme.asset_qss(paths.CACHE_DIR))
    app.setWindowIcon(theme.make_icon("listening"))
    ctx = Context()
    window = MainWindow(ctx)
    window.quit_requested.connect(lambda: shutdown(app, ctx, window))
    ctx.updates.quit_requested.connect(lambda: shutdown(app, ctx, window))
    server.newConnection.connect(lambda: (server.nextPendingConnection(), window.bring_to_front()))
    app.aboutToQuit.connect(lambda: window.tray and window.tray.hide())

    log.info("Джарвис %s запущен (данные: %s)", __version__, paths.DATA_DIR)
    service.prepare_background()
    window.start_update_checks()
    if not args.minimized or window.tray is None:
        window.show()
    if not args.no_voice and not args.smoke:
        ctx.service.start()
    if args.smoke:
        QTimer.singleShot(1500, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
