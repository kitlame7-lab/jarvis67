# -*- mode: python ; coding: utf-8 -*-
# Инструменты ПК для Hermes: pyinstaller packaging/jarvis-pc.spec  ->  dist/pc/jarvis-pc.exe
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821

a = Analysis(  # noqa: F821
    [str(ROOT / "tools" / "jarvis_pc.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "jarvis" / "resources" / "jarvis-pc" / "scripts" / "pc.py"),
            "jarvis/resources/jarvis-pc/scripts")],
    # pc.py подгружается как файл, поэтому его библиотеки перечисляем сами
    hiddenimports=["__future__", "argparse", "ctypes", "ctypes.wintypes", "datetime", "json", "os", "pathlib",
                   "shutil", "subprocess", "sys", "time", "typing", "urllib.error", "urllib.parse",
                   "urllib.request", "xml.etree.ElementTree"],
    excludes=["tkinter", "numpy", "PySide6"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="jarvis-pc",
    console=True,                       # результат печатается в консоль, Hermes читает его оттуда
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="pc", upx=False)  # noqa: F821
