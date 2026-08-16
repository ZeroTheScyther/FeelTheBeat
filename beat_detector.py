"""
Real-time beat detector using spectral flux onset detection and
autocorrelation-based BPM estimation.

Two independent per-band onset detectors:
  Light band  0–80 Hz   (kick body / low thump)
  Heavy band  220–270 Hz (snare crack)

Audio thread → per-band flux onset detection → beat queue
                                              ↘ ACF thread (every 2 s, BPM fallback)
"""

import queue
import threading
import time
from collections import deque

import numpy as np
from scipy.signal import correlate

import app_filter
from audio_backend import create_capture


class BeatDetector:
    HOP = 512        # audio block size (≈ 11 ms at 48 kHz)
    FFT_SIZE = 2048  # zero-padded FFT for better frequency resolution

    def __init__(self, device=None, beat_queue: queue.Queue | None = None,
                 heavy_threshold_db: float = -50.0,
                 light_threshold_db: float = -65.0,
                 filter_apps: list[str] | None = None,
                 heavy_mode: str = "threshold",
                 bass_sensitivity: float = 1.0,
                 heavy_band: str = "snare"):
        self.device = device
        self.beat_queue = beat_queue if beat_queue is not None else queue.Queue()
        self.heavy_threshold_db = heavy_threshold_db
        self.light_threshold_db = light_threshold_db
        self._heavy_mode = heavy_mode   # "threshold" | "adaptive"
        self._bass_sensitivity = bass_sensitivity

        self._filter_apps = [s.lower() for s in filter_apps] if filter_apps else None
        self._filter_active = True
        self._stop_event = threading.Event()

        # The capture backend resolves the device and its sample rate, so it
        # must exist before the band-edge maths below.
        self._capture = create_capture(device, self.HOP, self.process_block)
        self.SR = self._capture.samplerate
        self.device_name = self._capture.device_name

        self.hop_rate = self.SR / self.HOP
        freq_per_bin = self.SR / self.FFT_SIZE

        # Light band: 0–80 Hz
        self.orange_lo = 1  # skip DC bin
        self.orange_hi = max(2, int(80 / freq_per_bin))
        # Heavy band: snare (220–270 Hz) or kick (80–220 Hz)
        if heavy_band == "kick":
            self.yellow_lo = max(1, int(80 / freq_per_bin))
            self.yellow_hi = max(2, int(220 / freq_per_bin))
        else:
            self.yellow_lo = max(1, int(240 / freq_per_bin))
            self.yellow_hi = max(2, int(260 / freq_per_bin))

        # Rolling heavy-band dB history for adaptive mode
        self.heavy_db_hist: deque[float] = deque(maxlen=20)
        self._effective_heavy_threshold: float = heavy_threshold_db
        self._peak_heavy_threshold: float = heavy_threshold_db

        # Onset envelopes — one per band
        self.light_onset_env: deque[float] = deque(maxlen=int(8 * self.hop_rate))
        self.heavy_onset_env: deque[float] = deque(maxlen=int(8 * self.hop_rate))
        self._last_light_t: float = 0.0
        self._last_heavy_t: float = 0.0

        # Full-spectrum onset envelope for ACF BPM estimation
        self.onset_env: deque[float] = deque(maxlen=int(8 * self.hop_rate))

        # BPM state
        self.bpm: float = 120.0
        self.beat_period: float = 0.5
        self.bpm_stable: bool = False
        self._bpm_from_api: bool = False
        self._bpm_lo: float = 50.0
        self._bpm_hi: float = 220.0

        # Silence detection — rolling peak over ~1 s
        _hops_1s = max(1, int(1.0 * self.SR / self.HOP))
        self._peak_window: deque[float] = deque(maxlen=_hops_1s)
        self._silence_threshold: float = 1e-3

        self._spectrum: np.ndarray = np.zeros(self.FFT_SIZE // 2 + 1, dtype="float32")
        self._spectrum_peak: np.ndarray = np.zeros(self.FFT_SIZE // 2 + 1, dtype="float32")
        self._prev_mag: np.ndarray | None = None

        self._last_acf_t: float = 0.0
        self._acf_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._capture.start()

        if self._filter_apps:
            t = threading.Thread(target=self._app_filter_loop, daemon=True)
            t.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._capture.stop()

    def set_bpm(self, bpm: float) -> None:
        """Override BPM with an externally sourced value and lock against audio drift."""
        if bpm > 130.0:
            bpm /= 2.0
        self.bpm = bpm
        self.beat_period = 60.0 / bpm
        self.bpm_stable = True
        self._bpm_from_api = True
        self._reset_adaptive()

    def unlock_bpm(self) -> None:
        """Release API lock so ACF can re-estimate BPM."""
        self._bpm_from_api = False
        self.bpm_stable = False
        self._reset_adaptive()

    def _reset_adaptive(self) -> None:
        """Reset adaptive threshold and BPM state on song change."""
        self.heavy_db_hist.clear()
        self._effective_heavy_threshold = self.heavy_threshold_db
        self._peak_heavy_threshold = self.heavy_threshold_db
        self._bpm_lo = 50.0
        self._bpm_hi = 220.0

    @property
    def is_audio_active(self) -> bool:
        """True when the audio signal has been above the silence threshold recently."""
        if not self._peak_window:
            return True
        return max(self._peak_window) > self._silence_threshold

    # ------------------------------------------------------------------
    # App filter helpers
    # ------------------------------------------------------------------

    def _app_filter_loop(self) -> None:
        app_filter.init_thread()
        self._filter_active = app_filter.is_target_playing(self._filter_apps)
        while not self._stop_event.wait(2.0):
            self._filter_active = app_filter.is_target_playing(self._filter_apps)

    # ------------------------------------------------------------------
    # Audio processing  (PortAudio thread – keep it fast)
    # ------------------------------------------------------------------

    def process_block(self, audio: np.ndarray) -> None:
        """Handle one HOP-sized block of mono float32 audio."""
        self._peak_window.append(float(np.max(np.abs(audio))))

        buf = np.zeros(self.FFT_SIZE, dtype="float32")
        buf[: len(audio)] = audio * np.hanning(len(audio))
        mag = np.abs(np.fft.rfft(buf))

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

        self._prev_mag = mag
        self._spectrum[:] = mag
        np.maximum(self._spectrum_peak, mag, out=self._spectrum_peak)

        orange_peak = float(np.max(mag[self.orange_lo : self.orange_hi + 1]))
        light_db = 20.0 * np.log10(orange_peak / (self.FFT_SIZE / 2) + 1e-9)
        yellow_peak = float(np.max(mag[self.yellow_lo : self.yellow_hi + 1]))
        heavy_db = 20.0 * np.log10(yellow_peak / (self.FFT_SIZE / 2) + 1e-9)

        self.onset_env.append(flux)
        now = time.monotonic()

        self._detect_band_onset(self.light_onset_env, light_flux, light_db,
                                "light", self.light_threshold_db, now)
        self._detect_band_onset(self.heavy_onset_env, heavy_flux, heavy_db,
                                "heavy", self.heavy_threshold_db, now)

        if now - self._last_acf_t > 2.0:
            self._last_acf_t = now
            snap = list(self.onset_env)
            t = threading.Thread(target=self._acf_update, args=(snap,), daemon=True)
            t.start()

    # ------------------------------------------------------------------
    # Onset detection
    # ------------------------------------------------------------------

    def _detect_band_onset(self, env: deque, flux: float, db: float,
                           kind: str, threshold_db: float, now: float) -> None:
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
        if onset_t - last_t < 0.30:
            return

        if kind == "heavy" and self._heavy_mode == "adaptive":
            self.heavy_db_hist.append(db)
            if len(self.heavy_db_hist) >= 4:
                arr = np.array(self.heavy_db_hist)
                hist_mean = arr[:-1].mean()
                hist_std = arr[:-1].std()
                margin = max(hist_std * 0.7, 2.0) * self._bass_sensitivity
                new_threshold = max(hist_mean + margin, self.heavy_threshold_db)
                self._peak_heavy_threshold = max(new_threshold, self._peak_heavy_threshold)
                self._effective_heavy_threshold = max(new_threshold, self._peak_heavy_threshold - 3.0)
                if db <= self._effective_heavy_threshold:
                    return
            else:
                if db < threshold_db:
                    return
        else:
            if db < threshold_db:
                return

        if not self._filter_active:
            return

        if kind == "light":
            self._last_light_t = onset_t
        else:
            self._last_heavy_t = onset_t

        self.beat_queue.put((kind, self.bpm))

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

        acf = correlate(env, env, mode="full")
        acf = acf[len(acf) // 2:]

        lag_lo = max(1, int(self.hop_rate * 60.0 / 220.0))
        lag_hi = int(self.hop_rate * 60.0 / 50.0)
        if lag_hi >= len(acf):
            return

        best_lag = int(np.argmax(acf[lag_lo:lag_hi])) + lag_lo
        if best_lag <= 0:
            return

        cand_bpm = 60.0 * self.hop_rate / best_lag
        if cand_bpm > 160.0:
            cand_bpm /= 2.0
        if not (50 <= cand_bpm <= 220):
            return

        with self._acf_lock:
            if not self.bpm_stable:
                # Narrow the allowed range toward the candidate
                if cand_bpm < self._bpm_hi:
                    self._bpm_hi += (cand_bpm - self._bpm_hi) * 0.5
                if cand_bpm > self._bpm_lo:
                    self._bpm_lo += (cand_bpm - self._bpm_lo) * 0.5
                self._bpm_hi = max(self._bpm_hi, self._bpm_lo)
                # Move halfway toward candidate, clamped to narrowed range
                new_bpm = self.bpm + (cand_bpm - self.bpm) * 0.5
                self.bpm = max(self._bpm_lo, min(self._bpm_hi, new_bpm))
                self.beat_period = 60.0 / self.bpm
            else:
                ratio = cand_bpm / self.bpm
                if 0.45 < ratio < 0.55:
                    self.beat_period *= 2.0
                    self.bpm *= 0.5
                elif 1.8 < ratio < 2.2:
                    self.beat_period *= 0.5
                    self.bpm *= 2.0
