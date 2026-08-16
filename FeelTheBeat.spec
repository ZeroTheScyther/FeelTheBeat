# -*- mode: python ; coding: utf-8 -*-
"""
One spec, both platforms.  Onedir rather than onefile: startup is faster and
onefile's temp extraction would defeat the point of resolving writable state
through paths.config_dir().
"""

import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
ROOT = Path(SPECPATH)

hiddenimports = []
if IS_WINDOWS:
    hiddenimports += [
        "winrt.windows.media.control",
        "winrt.windows.foundation",
        "winrt.system",
        "pycaw",
        "pycaw.pycaw",
        "comtypes",
        "pyaudiowpatch",
    ]
else:
    hiddenimports += ["sounddevice"]

# Qt ships a lot this overlay never touches; dropping it roughly halves the bundle.
excludes = [
    "tkinter", "matplotlib", "pytest", "PIL.ImageQt",
    "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore", "PyQt5.QtWebChannel",
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets",
    "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtBluetooth", "PyQt5.QtNfc", "PyQt5.QtPositioning",
    "PyQt5.QtSerialPort", "PyQt5.QtSql", "PyQt5.QtTest",
    "PyQt5.QtWebSockets", "PyQt5.QtDesigner", "PyQt5.QtHelp", "PyQt5.QtXml",
]

_ico = ROOT / "assets" / "icon.ico"
icon = str(_ico) if IS_WINDOWS and _ico.exists() else None

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[("Frames", "Frames")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FeelTheBeat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console on Windows: main() redirects stdout to the log file instead.
    console=not IS_WINDOWS,
    disable_windowed_traceback=False,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FeelTheBeat",
)
