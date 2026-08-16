"""
"Now playing" lookup, abstracted over the platform.

Linux queries MPRIS through playerctl; Windows queries the WinRT
GlobalSystemMediaTransportControls session (the same source that backs the
Windows media flyout), which covers Spotify, browsers and most native players.
"""

import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

_warned = False


def get_current_track() -> tuple[str, str] | None:
    """Return (artist, title) for the active player, or None."""
    try:
        if IS_WINDOWS:
            return _windows_track()
        return _playerctl_track()
    except Exception:
        return None


# ----------------------------------------------------------------------
# Linux — MPRIS via playerctl
# ----------------------------------------------------------------------

def _playerctl_track() -> tuple[str, str] | None:
    try:
        r = subprocess.run(
            ["playerctl", "metadata", "--format", "{{artist}}\t{{title}}"],
            capture_output=True, text=True, timeout=2,
        )
    except FileNotFoundError:
        _warn_once("playerctl not found — install it for now-playing BPM lookup")
        return None
    except Exception:
        return None

    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = r.stdout.strip().split("\t", 1)
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0].strip(), parts[1].strip()


# ----------------------------------------------------------------------
# Windows — GlobalSystemMediaTransportControls
# ----------------------------------------------------------------------

async def _gsmtc_track():
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )
    manager = await SessionManager.request_async()
    session = manager.get_current_session()
    if session is None:
        return None
    props = await session.try_get_media_properties_async()
    artist = (props.artist or "").strip()
    title = (props.title or "").strip()
    if not artist or not title:
        return None
    return artist, title


def _windows_track() -> tuple[str, str] | None:
    import asyncio
    try:
        return asyncio.run(_gsmtc_track())
    except ImportError:
        _warn_once("winrt media control unavailable — now-playing lookup disabled")
        return None
    except Exception:
        return None


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(f"[track] {msg}")
