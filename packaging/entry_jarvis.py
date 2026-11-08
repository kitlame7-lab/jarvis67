"""Вход для PyInstaller: пакет jarvis импортируется как обычно, без относительных импортов в скрипте."""
import sys

from jarvis.app import main

if __name__ == "__main__":
    sys.exit(main())
