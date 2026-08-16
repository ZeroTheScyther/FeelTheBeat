"""
TrackWatcher — polls the platform's now-playing source for the current track
(MPRIS on Linux, GlobalSystemMediaTransportControls on Windows) and retrieves
BPM from GetSongBPM, falling back to Deezer.
"""

import json
import os
import threading
import urllib.parse
import urllib.request

import media_session


class TrackWatcher:
    _POLL_INTERVAL = 3.0
    _DEEZER_SEARCH_URL = "https://api.deezer.com/search"
    _DEEZER_TRACK_URL = "https://api.deezer.com/track"
    _GETSONGBPM_URL = "https://api.getsong.co/search/"

    def __init__(self, on_bpm_found: callable, on_bpm_unavailable: callable = None):
        self._on_bpm_found = on_bpm_found
        self._on_bpm_unavailable = on_bpm_unavailable
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
                bpm = self._getsongbpm_bpm(artist, title)
                if not bpm:
                    bpm = self._deezer_bpm(artist, title)
                if bpm:
                    self._on_bpm_found(bpm)
                else:
                    print(f"[track] BPM not found; falling back to audio detection")
                    if self._on_bpm_unavailable:
                        self._on_bpm_unavailable()

    # ------------------------------------------------------------------
    # Now playing
    # ------------------------------------------------------------------

    def _get_current_track(self) -> tuple[str, str] | None:
        return media_session.get_current_track()

    # ------------------------------------------------------------------
    # Deezer BPM
    # ------------------------------------------------------------------

    def _deezer_get(self, url: str) -> dict | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[track] HTTP error: {e}")
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
            "lookup": title,
        })
        result = self._deezer_get(f"{self._GETSONGBPM_URL}?{query}")
        if not result:
            return None
        search = result.get("search", [])
        # API returns {"search": {"error": "no result"}} when nothing found
        if not search or isinstance(search, str) or (isinstance(search, dict) and "error" in search):
            print(f"[track] GetSongBPM: no results for '{artist} – {title}'")
            return None
        # Response may be a list or a dict with numeric string keys
        items = list(search.values()) if isinstance(search, dict) else list(search)
        items = [i for i in items if isinstance(i, dict)]
        if not items:
            print(f"[track] GetSongBPM: no results for '{artist} – {title}'")
            return None
        # Prefer a result whose artist name matches
        artist_lower = artist.lower()
        def _artist_name(item: dict) -> str:
            a = item.get("artist", {})
            if isinstance(a, dict):
                return a.get("name", "").lower()
            return str(a).lower()
        best = next(
            (i for i in items if artist_lower in _artist_name(i)),
            items[0],
        )
        try:
            bpm = float(best.get("tempo") or 0)
        except (ValueError, TypeError):
            bpm = 0.0
        if bpm > 0:
            print(f"[track] GetSongBPM: {bpm:.1f}")
            return bpm
        print(f"[track] GetSongBPM: track found but tempo is {best.get('tempo')!r}")
        return None
