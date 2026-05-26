"""
SpectrumWindow — real-time FFT spectrum debug view.

64 logarithmically-spaced bands from 20 Hz to 20 kHz, coloured by
frequency region. The heavy-hit threshold is shown as a dashed red line;
the active sub-bass detection zone (50–80 Hz) is bracketed with markers.
"""

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QWidget

NUM_BANDS = 64
FREQ_MIN  = 20.0
FREQ_MAX  = 20000.0
DB_MIN    = -90.0
DB_MAX    = 0.0

# (upper-freq-limit, RGB) — band coloured by whichever region it falls in
_REGIONS = [
    (80,    (220,  50,  50)),   # sub-bass  – red
    (250,   (220, 130,  50)),   # bass      – orange
    (500,   (200, 200,  50)),   # low-mid   – yellow
    (2000,  ( 50, 180,  80)),   # mid       – green
    (8000,  ( 50, 180, 180)),   # high-mid  – teal
    (20000, ( 80, 100, 220)),   # high      – blue
]

def _band_color(freq: float) -> QColor:
    for limit, rgb in _REGIONS:
        if freq <= limit:
            return QColor(*rgb)
    return QColor(*_REGIONS[-1][1])


class SpectrumWindow(QWidget):

    PAD_L, PAD_R, PAD_T, PAD_B = 48, 12, 14, 28

    def __init__(self, detector) -> None:
        super().__init__()
        self._det = detector

        self.setWindowTitle("FeelTheBeat — Spectrum Debug")
        self.setMinimumSize(800, 280)
        self.resize(900, 300)
        self.setStyleSheet("background-color: #0d0d0d;")

        # Precompute log-spaced band edges and centre frequencies
        edges = np.logspace(np.log10(FREQ_MIN), np.log10(FREQ_MAX), NUM_BANDS + 1)
        self._edges  = edges
        self._centers = (edges[:-1] + edges[1:]) / 2.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)          # ~30 fps
        self._timer.timeout.connect(self.update)
        self._timer.start()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        w, h = self.width(), self.height()
        pl, pr, pt, pb = self.PAD_L, self.PAD_R, self.PAD_T, self.PAD_B
        dw = w - pl - pr        # drawable width
        dh = h - pt - pb        # drawable height

        # ── Grab a snapshot of the current spectrum ────────────────────
        mag         = self._det._spectrum.copy()
        fft_size    = self._det.FFT_SIZE
        sr          = self._det.SR
        fpb         = sr / fft_size          # Hz per FFT bin
        threshold   = self._det.heavy_threshold_db

        # ── Compute per-band peak dB ───────────────────────────────────
        band_db = np.full(NUM_BANDS, DB_MIN, dtype="float32")
        for i, (f_lo, f_hi) in enumerate(zip(self._edges[:-1], self._edges[1:])):
            b_lo = max(1, int(f_lo / fpb))
            b_hi = min(len(mag), max(b_lo + 1, int(f_hi / fpb)))
            if b_lo >= len(mag):
                continue
            peak = float(np.max(mag[b_lo:b_hi]))
            band_db[i] = 20.0 * np.log10(peak / (fft_size / 2) + 1e-9)

        # ── Grid lines ────────────────────────────────────────────────
        mono_font = QFont("Monospace", 8)
        painter.setFont(mono_font)
        for db in range(int(DB_MIN), int(DB_MAX) + 1, 10):
            y = self._db_y(db, pt, dh)
            painter.setPen(QPen(QColor(35, 35, 35), 1))
            painter.drawLine(pl, y, w - pr, y)
            if db % 20 == 0:
                painter.setPen(QColor(90, 90, 90))
                painter.drawText(2, y + 4, f"{db}")

        band_mode = getattr(self._det, '_band_mode', 'sub-bass')
        light_threshold = self._det.light_threshold_db

        # ── Frequency bars ────────────────────────────────────────────
        bar_w = max(1, dw // NUM_BANDS - 1)
        for i in range(NUM_BANDS):
            x  = pl + int(i * dw / NUM_BANDS)
            db = float(band_db[i])
            y  = self._db_y(db, pt, dh)
            bh = (pt + dh) - y
            if bh <= 0:
                continue
            col = _band_color(self._centers[i])
            if band_mode == "bass-lowmid":
                if self._centers[i] <= 80 and db < light_threshold:
                    col = QColor(col.red() // 4, col.green() // 4, col.blue() // 4)
                elif 220 < self._centers[i] <= 270 and db < threshold:
                    col = QColor(col.red() // 4, col.green() // 4, col.blue() // 4)
            else:
                if self._centers[i] <= 80 and db < threshold:
                    col = QColor(col.red() // 4, col.green() // 4, col.blue() // 4)
            painter.fillRect(x, y, bar_w, bh, col)

        # ── Threshold lines and zone brackets ─────────────────────────
        if band_mode == "bass-lowmid":
            # Heavy threshold (red dashed) for orange / bass band
            ty = self._db_y(threshold, pt, dh)
            painter.setPen(QPen(QColor(255, 60, 60), 1, Qt.DashLine))
            painter.drawLine(pl, ty, w - pr, ty)
            painter.setPen(QColor(255, 90, 90))
            painter.drawText(pl + 6, ty - 4, f"heavy threshold  {threshold:.0f} dB  (220–270 Hz)")

            # Light threshold (yellow dashed) for light band
            ly = self._db_y(light_threshold, pt, dh)
            painter.setPen(QPen(QColor(200, 200, 60), 1, Qt.DashLine))
            painter.drawLine(pl, ly, w - pr, ly)
            painter.setPen(QColor(200, 200, 90))
            painter.drawText(pl + 6, ly - 4, f"light threshold  {light_threshold:.0f} dB  (0–80 Hz)")

            # Light band bracket (0–80 Hz)
            x_o_hi = pl + int(self._freq_x(80, dw))
            painter.setPen(QPen(QColor(220, 130, 50, 120), 1, Qt.DotLine))
            painter.drawLine(pl, pt, pl, pt + dh)
            painter.drawLine(x_o_hi, pt, x_o_hi, pt + dh)
            painter.setPen(QColor(180, 110, 60))
            painter.drawText(pl + 2, pt + 10, "0")
            painter.drawText(x_o_hi + 2, pt + 10, "80 Hz (light)")

            # Heavy band bracket (240–270 Hz)
            x_y_lo = pl + int(self._freq_x(220, dw))
            x_y_hi = pl + int(self._freq_x(270, dw))
            painter.setPen(QPen(QColor(200, 200, 50, 120), 1, Qt.DotLine))
            painter.drawLine(x_y_lo, pt, x_y_lo, pt + dh)
            painter.drawLine(x_y_hi, pt, x_y_hi, pt + dh)
            painter.setPen(QColor(160, 160, 60))
            painter.drawText(x_y_lo + 2, pt + 10, "220")
            painter.drawText(x_y_hi + 2, pt + 10, "270 Hz (heavy)")
        else:
            # Sub-bass mode: original display
            ty = self._db_y(threshold, pt, dh)
            painter.setPen(QPen(QColor(255, 60, 60), 1, Qt.DashLine))
            painter.drawLine(pl, ty, w - pr, ty)
            painter.setPen(QColor(255, 90, 90))
            painter.drawText(pl + 6, ty - 4, f"heavy threshold  {threshold:.0f} dB")

            x_lo = pl + int(self._freq_x(50, dw))
            x_hi = pl + int(self._freq_x(80, dw))
            painter.setPen(QPen(QColor(255, 120, 120, 100), 1, Qt.DotLine))
            painter.drawLine(x_lo, pt, x_lo, pt + dh)
            painter.drawLine(x_hi, pt, x_hi, pt + dh)
            painter.setPen(QColor(180, 100, 100))
            painter.drawText(x_lo + 2, pt + 10, "50")
            painter.drawText(x_hi + 2, pt + 10, "80 Hz")

        # ── Live readout ───────────────────────────────────────────────
        painter.setPen(QColor(210, 210, 210))
        painter.setFont(QFont("Monospace", 9))
        if band_mode == "bass-lowmid":
            l_peak = float(np.max(mag[self._det.orange_lo : self._det.orange_hi + 1]))
            l_db   = 20.0 * np.log10(l_peak / (fft_size / 2) + 1e-9)
            h_peak = float(np.max(mag[self._det.yellow_lo : self._det.yellow_hi + 1]))
            h_db   = 20.0 * np.log10(h_peak / (fft_size / 2) + 1e-9)
            hit    = "HEAVY" if h_db >= threshold else ("light" if l_db >= light_threshold else "none")
            painter.drawText(pl + 6, pt + dh - 4,
                             f"0-80Hz: {l_db:+.1f} dB (light)   "
                             f"220-270Hz: {h_db:+.1f} dB (heavy)   "
                             f"BPM: {self._det.bpm:.0f}   "
                             f"hit: {hit}")
        else:
            sub_peak = float(np.max(mag[self._det.sub_bass_lo : self._det.sub_bass_hi + 1]))
            sub_db   = 20.0 * np.log10(sub_peak / (fft_size / 2) + 1e-9)
            hit      = "HEAVY" if sub_db >= threshold else "light"
            painter.drawText(pl + 6, pt + dh - 4,
                             f"sub-bass: {sub_db:+.1f} dB   "
                             f"BPM: {self._det.bpm:.0f}   "
                             f"hit: {hit}")

        # ── Frequency axis labels ──────────────────────────────────────
        painter.setPen(QColor(110, 110, 110))
        painter.setFont(mono_font)
        for freq in (30, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            bx = pl + int(self._freq_x(freq, dw))
            label = f"{freq // 1000}k" if freq >= 1000 else str(freq)
            painter.drawText(bx - 6, h - 6, label)

        painter.end()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _db_y(self, db: float, pt: int, dh: int) -> int:
        ratio = (db - DB_MAX) / (DB_MIN - DB_MAX)
        return pt + int(ratio * dh)

    def _freq_x(self, freq: float, dw: int) -> float:
        idx = float(np.searchsorted(self._centers, freq))
        return idx * dw / NUM_BANDS

    def closeEvent(self, event) -> None:
        self._timer.stop()
        event.accept()
