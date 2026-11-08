# -*- mode: python ; coding: utf-8 -*-
# Сборка окна: pyinstaller packaging/jarvis.spec  ->  dist/Jarvis/Jarvis.exe
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH задаёт PyInstaller)

datas, binaries, hiddenimports = [(str(ROOT / "jarvis" / "resources"), "jarvis/resources")], [], []
# Эти библиотеки подгружают свои файлы и библиотеки динамически, поэтому берём их целиком.
# sounddevice — один файл, его DLL (PortAudio) лежит в пакете _sounddevice_data.
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers", "huggingface_hub",
            "_sounddevice_data", "edge_tts", "certifi"):
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:  # noqa: BLE001 — пакета может не быть на этой системе
        print(f"[spec] пропускаю {pkg}: {exc}")
        continue
    datas += d
    binaries += b
    hiddenimports += h

icon = ROOT / "packaging" / "jarvis.ico"

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry_jarvis.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["jarvis.gui.pages", "jarvis.gui.pages2"],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "IPython", "pytest",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick",
              "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Jarvis",
    console=False,                      # окно без чёрной консоли
    icon=str(icon) if icon.exists() and os.name == "nt" else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Jarvis", upx=False)  # noqa: F821
