"""
Path resolution that works both from source and inside a PyInstaller bundle.

Read-only assets (frames, icons) live next to the code — or in ``sys._MEIPASS``
when frozen.  Anything writable (settings, logs, .env) goes to the per-user
config directory, because a frozen bundle's own directory is read-only.
"""

import os
import sys
from pathlib import Path

APP_NAME = "FeelTheBeat"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def resource_path(*parts: str) -> Path:
    """Locate a read-only bundled asset."""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent
    return root.joinpath(*parts)


def app_dir() -> Path:
    """Directory holding the executable (frozen) or the source tree."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_dir() -> Path:
    """Writable per-user config directory, created on first access."""
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = root / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return config_dir() / "feelthebeat.log"


def _attach_parent_console() -> bool:
    """
    A windowed Windows build has no console of its own, but when it was started
    from cmd/PowerShell we can borrow the caller's — so `--list-devices` and
    `--help` still print where the user is looking.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        ATTACH_PARENT_PROCESS = -1
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return False
        stream = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        return False
    sys.stdout = stream
    sys.stderr = stream
    return True


def setup_output() -> Path | None:
    """
    Windowed PyInstaller builds have no console: ``sys.stdout`` is None and the
    first print() would raise.  Attach to the launching terminal if there is
    one, otherwise fall back to the log file.

    Returns the log path when logging to file, else None.
    """
    if not is_frozen() or (sys.stdout is not None and sys.stderr is not None):
        return None
    if _attach_parent_console():
        return None
    path = log_path()
    try:
        stream = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return None
    sys.stdout = stream
    sys.stderr = stream
    return path
