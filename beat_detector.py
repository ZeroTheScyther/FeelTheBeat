"""
Real-time beat detector using spectral flux onset detection and
autocorrelation-based BPM estimation.

Audio thread → onset envelope → peak detection → beat phase tracking
                                               ↘ ACF thread (every 2 s)
"""

import queue
import subprocess
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
from scipy.signal import correlate


class BeatDetector:
    HOP = 512       # audio block size (≈ 11 ms at 48 kHz)
    FFT_SIZE = 2048  # zero-padded FFT for better frequency resolution

    def __init__(self, device=None, beat_queue: queue.Queue | None = None,
                 heavy_threshold_db: float = -50.0,
                 light_threshold_db: float = -65.0,
                 bass_only: bool = False,
                 filter_apps: list[str] | None = None,
                 mode: str = "phase-locked",
                 heavy_mode: str = "threshold",
                 bass_sensitivity: float = 1.0,
                 peak_hold_ms: float = 0.0,
                 band_mode: str = "sub-bass"):
        self.device = device
        self.beat_queue = beat_queue if beat_queue is not None else queue.Queue()
        self.heavy_threshold_db = heavy_threshold_db
        self.light_threshold_db = light_threshold_db
        self._bass_only = bass_only
        self._mode = mode          # "phase-locked" | "onset"
        self._heavy_mode = heavy_mode  # "threshold" | "adaptive"
        self._bass_sensitivity = bass_sensitivity
        self._band_mode = band_mode  # "sub-bass" | "bass-lowmid"

        # App filter: only fire beats when one of these substrings appears in
        # a running PulseAudio sink-input's properties (application.name,
        # application.process.binary, media.name, etc.)
        self._filter_apps = [s.lower() for s in filter_apps] if filter_apps else None
        self._filter_active = True   # becomes False when filter is set but no match
        self._stop_event = threading.Event()

        # Query device sample rate
        try:
            info = sd.query_devices(device if device is not None
                                    else sd.default.device[0])
            self.SR = int(info["default_samplerate"])
        except Exception:
            self.SR = 48000

        self.hop_rate = self.SR / self.HOP          # hops per second
        freq_per_bin = self.SR / self.FFT_SIZE
        # Bins covering the sub-bass kick region (~50–80 Hz)
        self.sub_bass_lo = max(1, int(50 / freq_per_bin))
        self.sub_bass_hi = max(2, int(80 / freq_per_bin))
        # Bins covering the full bass range (20–150 Hz) for adaptive mode
        self.bass_lo = max(1, int(20 / freq_per_bin))
        self.bass_hi = max(2, int(150 / freq_per_bin))
        # Light band (0–80 Hz) and heavy band (240–270 Hz) for bass-lowmid mode
        self.orange_lo = 1  # skip DC bin
        self.orange_hi = max(2, int(80 / freq_per_bin))
        self.yellow_lo = max(1, int(220 / freq_per_bin))
        self.yellow_hi = max(2, int(270 / freq_per_bin))
        # Rolling bass ratio history for adaptive heavy-hit detection
        self.bass_hist: deque[float] = deque(maxlen=20)

        # Onset envelope – rolling 8-second buffer
        self.onset_env: deque[float] = deque(maxlen=int(8 * self.hop_rate))

        # Independent onset envelopes for bass-lowmid mode (one per band)
        self.light_onset_env: deque[float] = deque(maxlen=int(8 * self.hop_rate))
        self.heavy_onset_env: deque[float] = deque(maxlen=int(8 * self.hop_rate))
        self._last_light_t: float = 0.0
        self._last_heavy_t: float = 0.0

        # BPM / beat-phase state
        self.bpm: float = 120.0
        self.beat_period: float = 0.5        # seconds
        self.last_beat_t: float = 0.0
        self.bpm_stable: bool = False
        self._bpm_from_api: bool = False     # when True, suppress audio-based BPM updates

        # Silence detection — rolling peak over ~1 s of hops
        _hops_1s = max(1, int(1.0 * self.SR / self.HOP))
        self._peak_window: deque[float] = deque(maxlen=_hops_1s)
        self._silence_threshold: float = 1e-3

        self._spectrum: np.ndarray = np.zeros(self.FFT_SIZE // 2 + 1, dtype="float32")

        # Sub-bass peak hold — keeps the max dB over a short window so that
        # kick drum transients don't flicker between heavy and light
        if peak_hold_ms > 0:
            hold_hops = max(1, int(peak_hold_ms / 1000.0 * self.SR / self.HOP))
            self._peak_hold: deque[float] | None = deque(maxlen=hold_hops)
        else:
            self._peak_hold = None

        self._prev_mag: np.ndarray | None = None
        self._channels: int = 1
        self._stream: sd.InputStream | None = None

        # ACF background update bookkeeping
        self._last_acf_t: float = 0.0
        self._acf_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        # Determine how many channels the device offers
        try:
            info = sd.query_devices(self.device)
            self._channels = min(2, max(1, info["max_input_channels"]))
        except Exception:
            self._channels = 1

        self._stream = sd.InputStream(
            device=self.device,
            channels=self._channels,
            samplerate=self.SR,
            blocksize=self.HOP,
            dtype="float32",
            callback=self._audio_cb,
            latency="low",
        )
        self._stream.start()

        if self._filter_apps:
            self._poll_sink_inputs()  # immediate first check before first beat
            t = threading.Thread(target=self._app_filter_loop, daemon=True)
            t.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def set_bpm(self, bpm: float) -> None:
        """Override BPM with an externally sourced value and lock against audio drift."""
        if bpm > 160.0:
            bpm /= 2.0
        self.bpm = bpm
        self.beat_period = 60.0 / bpm
        self.bpm_stable = True
        self._bpm_from_api = True

    @property
    def is_audio_active(self) -> bool:
        """True when the audio signal has been above the silence threshold recently."""
        if not self._peak_window:
            return True
        return max(self._peak_window) > self._silence_threshold

    # ------------------------------------------------------------------
    # App filter helpers
    # ------------------------------------------------------------------

    def _poll_sink_inputs(self) -> None:
        """Update _filter_active by checking pactl sink-inputs for matching apps."""
        try:
            r = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True, text=True, timeout=3,
            )
            # Split output into per-sink-input blocks and require both the app
            # name match AND Corked: no (i.e. actually playing, not paused)
            blocks: list[str] = []
            current: list[str] = []
            for line in r.stdout.splitlines():
                if line.startswith("Sink Input #") and current:
                    blocks.append("\n".join(current))
                    current = []
                current.append(line)
            if current:
                blocks.append("\n".join(current))

            self._filter_active = any(
                "corked: no" in block.lower()
                and any(app in block.lower() for app in self._filter_apps)
                for block in blocks
            )
        except Exception:
            self._filter_active = True  # fail open — don't silence beats on error

    def _app_filter_loop(self) -> None:
        while not self._stop_event.wait(2.0):
            self._poll_sink_inputs()

    # ------------------------------------------------------------------
    # Audio callback  (PortAudio thread – keep it fast)
    # ------------------------------------------------------------------

    def _audio_cb(self, indata, frames, time_info, status):
        # Mix to mono
        audio = indata.mean(axis=1) if self._channels > 1 else indata[:, 0]
        self._peak_window.append(float(np.max(np.abs(audio))))

        # Zero-pad and compute magnitude spectrum
        buf = np.zeros(self.FFT_SIZE, dtype="float32")
        buf[: len(audio)] = audio * np.hanning(len(audio))
        mag = np.abs(np.fft.rfft(buf))

        # Spectral flux (positive half-wave rectified) — compute BEFORE updating _prev_mag
        if self._prev_mag is not None:
            flux = float(np.sum(np.maximum(mag - self._prev_mag, 0.0)))
            light_flux = float(np.sum(np.maximum(
                mag[self.orange_lo:self.orange_hi + 1]
                - self._prev_mag[self.orange_lo:self.orange_hi + 1], 0.0)))
            heavy_flux = float(np.sum(np.maximum(
                mag[self.yellow_lo:self.yellow_hi + 1]
                - self._prev_mag[self.yellow_lo:self.yellow_hi + 1], 0.0)))
        else:
            flux = light_flux = heavy_flux = 0.0
        self._prev_mag = mag  # no copy needed – mag is freshly allocated
        self._spectrum[:] = mag

        # Sub-bass (50–80 Hz) — always computed for spectrum display
        sub_peak = float(np.max(mag[self.sub_bass_lo : self.sub_bass_hi + 1]))
        sub_bass_db = 20.0 * np.log10(sub_peak / (self.FFT_SIZE / 2) + 1e-9)

        if self._band_mode == "bass-lowmid":
            orange_peak = float(np.max(mag[self.orange_lo : self.orange_hi + 1]))
            light_db = 20.0 * np.log10(orange_peak / (self.FFT_SIZE / 2) + 1e-9)
            yellow_peak = float(np.max(mag[self.yellow_lo : self.yellow_hi + 1]))
            heavy_db = 20.0 * np.log10(yellow_peak / (self.FFT_SIZE / 2) + 1e-9)
        else:
            heavy_db = sub_bass_db
            light_db = sub_bass_db

        if self._peak_hold is not None:
            self._peak_hold.append(heavy_db)
            heavy_db = max(self._peak_hold)

        # Bass energy ratio (20–150 Hz) used by adaptive heavy-hit mode
        total = float(np.sum(mag[1:]))
        bass = float(np.sum(mag[self.bass_lo : self.bass_hi]))
        bass_ratio = bass / max(total, 1e-9)

        self.onset_env.append(flux)
        now = time.monotonic()

        if self._band_mode == "bass-lowmid":
            self._detect_band_onset(self.light_onset_env, light_flux, light_db,
                                    "light", self.light_threshold_db, now)
            self._detect_band_onset(self.heavy_onset_env, heavy_flux, heavy_db,
                                    "heavy", self.heavy_threshold_db, now)
        else:
            self._detect_onset(flux, heavy_db, light_db, bass_ratio, now)

        # Kick off ACF refresh at most every 2 s
        if now - self._last_acf_t > 2.0:
            self._last_acf_t = now
            snap = list(self.onset_env)
            t = threading.Thread(target=self._acf_update, args=(snap,), daemon=True)
            t.start()

    # ------------------------------------------------------------------
    # Onset / beat detection
    # ------------------------------------------------------------------

    def _detect_band_onset(self, env: deque, flux: float, db: float,
                           kind: str, threshold_db: float, now: float) -> None:
        """Independent per-band onset detector used in bass-lowmid mode."""
        env.append(flux)
        if len(env) < 5:
            return

        window = list(env)[-min(len(env), int(1.5 * self.hop_rate)):]
        mean_v = float(np.mean(window))
        std_v  = float(np.std(window))
        onset_threshold = max(mean_v + 1.2 * std_v, mean_v * 1.5, 1e-6)

        vals = list(env)
        if not (len(vals) >= 3
                and vals[-2] > onset_threshold
                and vals[-2] > vals[-3]
                and vals[-2] >= vals[-1]):
            return

        onset_t = now - 2.0 * self.HOP / self.SR
        last_t  = self._last_light_t if kind == "light" else self._last_heavy_t
        if onset_t - last_t < 0.25:
            return

        if db < threshold_db:
            return

        if not self._filter_active:
            return

        if kind == "light":
            self._last_light_t = onset_t
        else:
            self._last_heavy_t = onset_t

        self.beat_queue.put((kind, self.bpm))

    def _detect_onset(self, flux: float, heavy_db: float, light_db: float, bass_ratio: float, now: float) -> None:
        env = self.onset_env
        if len(env) < 5:
            return

        # Adaptive threshold over a ~1.5-second window
        window = list(env)[-min(len(env), int(1.5 * self.hop_rate)):]
        mean_v = float(np.mean(window))
        std_v = float(np.std(window))
        # Require the peak to exceed both a relative and a small absolute floor
        threshold = max(mean_v + 1.2 * std_v, mean_v * 1.5, 1e-5)

        # Confirm peak 2 hops ago (so we have both neighbours)
        vals = list(env)
        if (len(vals) >= 3
                and vals[-2] > threshold
                and vals[-2] > vals[-3]
                and vals[-2] >= vals[-1]):
            # Approximate onset time (1 hop back from *previous* sample)
            onset_t = now - 2.0 * self.HOP / self.SR
            self._on_onset(onset_t, heavy_db, light_db, bass_ratio)

    def _on_onset(self, onset_t: float, heavy_db: float, light_db: float, bass_ratio: float) -> None:
        # Hard gate: ignore anything closer than 250 ms (> 240 BPM)
        if onset_t - self.last_beat_t < 0.25:
            return

        if self._mode == "phase-locked":
            if not self._phase_locked_accept(onset_t):
                return
        else:
            self.last_beat_t = onset_t

        if not self._filter_active:
            return

        if self._heavy_mode == "adaptive":
            self.bass_hist.append(bass_ratio)
            is_heavy = False
            if len(self.bass_hist) >= 4:
                arr = np.array(self.bass_hist)
                hist_mean = arr[:-1].mean()
                hist_std = arr[:-1].std()
                margin = max(hist_std * 0.7, 0.04) * self._bass_sensitivity
                is_heavy = bass_ratio > hist_mean + margin
        else:
            is_heavy = heavy_db >= self.heavy_threshold_db

        if not is_heavy:
            # bass-lowmid mode always gates light hits on the yellow band; bass-only does the same for sub-bass
            if self._band_mode == "bass-lowmid" or self._bass_only:
                if light_db < self.light_threshold_db:
                    return

        self.beat_queue.put(("heavy" if is_heavy else "light", self.bpm))

    def _phase_locked_accept(self, onset_t: float) -> bool:
        """Gate and update beat phase. Returns True if this onset should fire."""
        if self.last_beat_t == 0.0:
            self.last_beat_t = onset_t
            return False  # seed only

        since = onset_t - self.last_beat_t
        n = max(1, round(since / self.beat_period))
        error = onset_t - (self.last_beat_t + n * self.beat_period)
        tolerance = self.beat_period * 0.30

        if abs(error) <= tolerance or not self.bpm_stable:
            if not self._bpm_from_api:
                actual_period = since / n
                if 0.27 <= actual_period <= 2.0:   # ≈ 30–222 BPM
                    alpha = 0.08 if self.bpm_stable else 0.25
                    self.beat_period = (1.0 - alpha) * self.beat_period + alpha * actual_period
                    self.bpm = 60.0 / self.beat_period
                    self.bpm_stable = True
            self.last_beat_t = onset_t
            return True

        return False

    # ------------------------------------------------------------------
    # Autocorrelation BPM estimation  (background thread, every 2 s)
    # ------------------------------------------------------------------

    def _acf_update(self, env_list: list[float]) -> None:
        if self._bpm_from_api:
            return
        min_samples = int(2.0 * self.hop_rate)
        if len(env_list) < min_samples:
            return

        env = np.array(env_list, dtype="float64")
        env -= env.mean()

        # Full autocorrelation (FFT-accelerated by scipy)
        acf = correlate(env, env, mode="full")
        acf = acf[len(acf) // 2 :]  # keep positive lags

        # Lag range corresponding to 50–220 BPM
        lag_lo = max(1, int(self.hop_rate * 60.0 / 220.0))
        lag_hi = int(self.hop_rate * 60.0 / 50.0)
        if lag_hi >= len(acf):
            return

        best_lag = int(np.argmax(acf[lag_lo:lag_hi])) + lag_lo
        if best_lag <= 0:
            return

        cand_bpm = 60.0 * self.hop_rate / best_lag

        if not (50 <= cand_bpm <= 220):
            return

        with self._acf_lock:
            if not self.bpm_stable:
                # No onset-based estimate yet – seed from ACF
                self.beat_period = 60.0 / cand_bpm
                self.bpm = cand_bpm
            else:
                # Correct octave errors: if ACF says half/double our current BPM
                ratio = cand_bpm / self.bpm
                if 0.45 < ratio < 0.55:     # ACF found half-tempo → we're doubled
                    self.beat_period *= 2.0
                    self.bpm *= 0.5
                elif 1.8 < ratio < 2.2:     # ACF found double-tempo → we're halved
                    self.beat_period *= 0.5
                    self.bpm *= 2.0
