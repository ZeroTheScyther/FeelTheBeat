"""
TrackWatcher — polls MPRIS (via playerctl) for the current track and
retrieves BPM from the Deezer API (no API key required).
"""

import json
import os
import subprocess
import threading
import urllib.parse
import urllib.request


class TrackWatcher:
    _POLL_INTERVAL = 3.0
    _DEEZER_SEARCH_URL = "https://api.deezer.com/search"
    _DEEZER_TRACK_URL = "https://api.deezer.com/track"
    _GETSONGBPM_URL = "https://api.getsongbpm.com/search/"

    def __init__(self, on_bpm_found: callable):
        self._on_bpm_found = on_bpm_found
        self._current_track: tuple[str, str] | None = None
        self._stop_event = threading.Event()
        self._getsongbpm_key: str | None = os.environ.get("GETSONGBPM_API_KEY")

    def start(self) -> None:
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self._POLL_INTERVAL):
            track = self._get_current_track()
            if track and track != self._current_track:
                self._current_track = track
                artist, title = track
                print(f"[track] Now playing: {artist} – {title}")
                bpm = self._deezer_bpm(artist, title)
                if not bpm:
                    bpm = self._getsongbpm_bpm(artist, title)
                if bpm:
                    self._on_bpm_found(bpm)
                else:
                    print(f"[track] BPM not found; keeping current BPM")

    # ------------------------------------------------------------------
    # MPRIS / playerctl
    # ------------------------------------------------------------------

    def _get_current_track(self) -> tuple[str, str] | None:
        try:
            r = subprocess.run(
                ["playerctl", "metadata", "--format", "{{artist}}\t{{title}}"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return None
            parts = r.stdout.strip().split("\t", 1)
            if len(parts) != 2 or not all(parts):
                return None
            return parts[0].strip(), parts[1].strip()
        except FileNotFoundError:
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Deezer BPM
    # ------------------------------------------------------------------

    def _deezer_get(self, url: str) -> dict | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[track] Deezer error: {e}")
            return None

    def _deezer_bpm(self, artist: str, title: str) -> float | None:
        query = urllib.parse.urlencode({"q": f"{artist} {title}"})
        result = self._deezer_get(f"{self._DEEZER_SEARCH_URL}?{query}")
        if not result:
            return None
        items = result.get("data", [])
        if not items:
            print(f"[track] Deezer: no results for '{artist} – {title}'")
            return None
        track = self._deezer_get(f"{self._DEEZER_TRACK_URL}/{items[0]['id']}")
        if not track:
            return None
        bpm = track.get("bpm")
        if bpm and bpm > 0:
            print(f"[track] Deezer BPM: {float(bpm):.1f}")
            return float(bpm)
        print(f"[track] Deezer: track found ('{items[0].get('title')}') but BPM field is {bpm!r}")
        return None

    def _getsongbpm_bpm(self, artist: str, title: str) -> float | None:
        if not self._getsongbpm_key:
            return None
        query = urllib.parse.urlencode({
            "api_key": self._getsongbpm_key,
            "type": "song",
            "lookup": f"{artist} {title}",
        })
        result = self._deezer_get(f"{self._GETSONGBPM_URL}?{query}")
        if not result:
            return None
        items = result.get("search", [])
        if not items:
            print(f"[track] GetSongBPM: no results for '{artist} – {title}'")
            return None
        try:
            bpm = float(items[0].get("tempo", 0))
        except (ValueError, TypeError):
            bpm = 0.0
        if bpm > 0:
            print(f"[track] GetSongBPM: {bpm:.1f}")
            return bpm
        print(f"[track] GetSongBPM: track found but tempo is {items[0].get('tempo')!r}")
        return None
