"""
`--filter-apps` support: is one of the named applications actively playing audio?

Linux reads PulseAudio sink-inputs via pactl; Windows enumerates Core Audio
sessions via pycaw.  Both fail open (return True), so a broken filter never
silences the overlay entirely.
"""

import subprocess
import sys

from paths import is_frozen

IS_WINDOWS = sys.platform == "win32"

_AUDIO_SESSION_STATE_ACTIVE = 1


def init_thread() -> None:
    """
    Call once at the top of the polling thread.  COM apartments are per-thread,
    so pycaw needs CoInitialize here rather than at import time.
    """
    if not IS_WINDOWS:
        return
    try:
        import comtypes
        if is_frozen():
            # A frozen bundle is read-only; stop comtypes writing generated
            # COM wrappers into it and make it use in-memory modules instead.
            import comtypes.client
            comtypes.client.gen_dir = None
        comtypes.CoInitialize()
    except Exception as exc:
        print(f"[filter] COM init failed, app filter disabled: {exc}")


def is_target_playing(apps: list[str]) -> bool:
    if not apps:
        return True
    try:
        if IS_WINDOWS:
            return _windows_playing(apps)
        return _pulse_playing(apps)
    except Exception:
        return True


# ----------------------------------------------------------------------
# Linux — PulseAudio sink-inputs
# ----------------------------------------------------------------------

def _pulse_playing(apps: list[str]) -> bool:
    r = subprocess.run(
        ["pactl", "list", "sink-inputs"],
        capture_output=True, text=True, timeout=3,
    )
    blocks: list[str] = []
    current: list[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("Sink Input #") and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))

    return any(
        "corked: no" in block.lower()
        and any(app in block.lower() for app in apps)
        for block in blocks
    )


# ----------------------------------------------------------------------
# Windows — Core Audio sessions
# ----------------------------------------------------------------------

def _windows_playing(apps: list[str]) -> bool:
    from pycaw.pycaw import AudioUtilities

    for session in AudioUtilities.GetAllSessions():
        if session.State != _AUDIO_SESSION_STATE_ACTIVE:
            continue
        names = []
        try:
            if session.Process is not None:
                names.append(session.Process.name())
        except Exception:
            pass
        if session.DisplayName:
            names.append(session.DisplayName)
        haystack = " ".join(names).lower()
        if haystack and any(app in haystack for app in apps):
            return True
    return False
