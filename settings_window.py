"""
Settings window — every tunable in one place, applied live.

Changes take effect immediately so thresholds can be dialled in against the
spectrum visualizer, but nothing is written to disk until Save.  Lock and Flip
are the exception: they mirror the tray actions, so they apply and persist at
once.  The device selector needs a restart and is excluded from the live path.
"""

import settings as settings_mod
from icons import app_icon

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

HEAVY_MODES = [("threshold", "Threshold  (fixed dBFS)"),
               ("adaptive", "Adaptive  (tracks rolling average)")]
HEAVY_BANDS = [("snare", "Snare  (240–260 Hz)"),
               ("kick", "Kick  (80–220 Hz)")]


class _SliderSpin(QWidget):
    """A slider and spinbox bound to each other, over a float range."""

    def __init__(self, lo: float, hi: float, step: float, suffix: str = "",
                 decimals: int = 1, on_change=None):
        super().__init__()
        self._scale = 1.0 / step
        self._on_change = on_change
        self._guard = False

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(lo * self._scale), int(hi * self._scale))
        self.spin = QDoubleSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setSingleStep(step)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(suffix)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)

        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def _from_slider(self, v: int) -> None:
        if self._guard:
            return
        self._guard = True
        self.spin.setValue(v / self._scale)
        self._guard = False
        self._emit()

    def _from_spin(self, v: float) -> None:
        if self._guard:
            return
        self._guard = True
        self.slider.setValue(int(v * self._scale))
        self._guard = False
        self._emit()

    def _emit(self) -> None:
        if self._on_change:
            self._on_change()

    def value(self) -> float:
        return self.spin.value()

    def setValue(self, v: float) -> None:
        self._guard = True
        self.spin.setValue(v)
        self.slider.setValue(int(v * self._scale))
        self._guard = False


class SettingsWindow(QDialog):

    def __init__(self, overlay, detector, defaults: dict):
        super().__init__()
        self._overlay = overlay
        self._detector = detector
        self._defaults = defaults
        self._loading = True

        self.setWindowTitle("FeelTheBeat — Settings")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)

        # ── Status line ────────────────────────────────────────────────
        self._status = QLabel()
        self._status.setStyleSheet("color: #888;")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        root.addWidget(self._detection_group())
        root.addWidget(self._appearance_group())
        root.addWidget(self._audio_group())

        # ── Buttons ────────────────────────────────────────────────────
        row = QHBoxLayout()
        vis = QPushButton("📊  Open Visualizer")
        vis.setToolTip("Live spectrum view — drag the thresholds against it")
        vis.clicked.connect(self._overlay.open_visualizer)
        row.addWidget(vis)
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._reset_defaults)
        row.addWidget(reset)
        row.addStretch(1)
        root.addLayout(row)

        buttons = QDialogButtonBox()
        self._save_btn = buttons.addButton("Save", QDialogButtonBox.AcceptRole)
        self._revert_btn = buttons.addButton("Revert", QDialogButtonBox.ResetRole)
        close_btn = buttons.addButton("Close", QDialogButtonBox.RejectRole)
        self._save_btn.clicked.connect(self._save)
        self._revert_btn.clicked.connect(self._revert)
        close_btn.clicked.connect(self.close)
        root.addWidget(buttons)

        # ── State ──────────────────────────────────────────────────────
        self._snapshot = self._current_options()
        self._saved = dict(self._snapshot)
        self._load(self._snapshot)
        self._loading = False
        self._update_dirty()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()
        self._refresh_status()

    # ------------------------------------------------------------------
    # Widget groups
    # ------------------------------------------------------------------

    def _detection_group(self) -> QGroupBox:
        box = QGroupBox("Detection")
        form = QFormLayout(box)

        self.heavy_mode = QComboBox()
        for key, label in HEAVY_MODES:
            self.heavy_mode.addItem(label, key)
        self.heavy_mode.currentIndexChanged.connect(self._on_change)
        form.addRow("Heavy mode", self.heavy_mode)

        self.heavy_band = QComboBox()
        for key, label in HEAVY_BANDS:
            self.heavy_band.addItem(label, key)
        self.heavy_band.currentIndexChanged.connect(self._on_change)
        form.addRow("Heavy band", self.heavy_band)

        self.heavy_threshold = _SliderSpin(-90.0, 0.0, 0.5, " dBFS", 1, self._on_change)
        form.addRow("Heavy threshold", self.heavy_threshold)

        self.light_threshold = _SliderSpin(-90.0, 0.0, 0.5, " dBFS", 1, self._on_change)
        form.addRow("Light threshold", self.light_threshold)

        self.bass_sensitivity = QDoubleSpinBox()
        self.bass_sensitivity.setRange(0.1, 5.0)
        self.bass_sensitivity.setSingleStep(0.1)
        self.bass_sensitivity.setToolTip("Adaptive mode only — higher means fewer heavy hits")
        self.bass_sensitivity.valueChanged.connect(self._on_change)
        form.addRow("Bass sensitivity", self.bass_sensitivity)

        self.continuous = QCheckBox("Fire light hits at the BPM rate")
        self.continuous.setToolTip(
            "On: lights are driven by a BPM timer and audio only triggers heavy hits.\n"
            "Off: every detected light onset fires an animation.")
        self.continuous.toggled.connect(self._on_change)
        form.addRow("Continuous", self.continuous)

        return box

    def _appearance_group(self) -> QGroupBox:
        box = QGroupBox("Appearance")
        form = QFormLayout(box)

        self.dual = QCheckBox("Two characters across the full monitor width")
        self.dual.toggled.connect(self._on_change)
        form.addRow("Dual mode", self.dual)

        self.scale = _SliderSpin(0.1, 2.0, 0.05, "×", 2, self._on_change)
        form.addRow("Scale", self.scale)

        # These two mirror the tray actions: immediate and self-persisting.
        self.flipped = QCheckBox("Face right")
        self.flipped.toggled.connect(
            lambda v: None if self._loading else self._overlay.set_flipped(v))
        form.addRow("Flip", self.flipped)

        self.unlocked = QCheckBox("Unlocked  (draggable, not click-through)")
        self.unlocked.toggled.connect(
            lambda v: None if self._loading else self._overlay.set_locked(not v))
        form.addRow("Lock", self.unlocked)

        return box

    def _audio_group(self) -> QGroupBox:
        box = QGroupBox("Audio")
        form = QFormLayout(box)

        self.device = QComboBox()
        self.device.addItem("Automatic  (system audio)", None)
        try:
            import audio_backend
            for idx, name in audio_backend.enumerate_devices():
                self.device.addItem(f"{idx}: {name}", str(idx))
        except Exception as exc:
            print(f"[ui] Could not enumerate devices: {exc}")
        form.addRow("Input device", self.device)

        note = QLabel("Device changes take effect on restart.")
        note.setStyleSheet("color: #888; font-style: italic;")
        form.addRow("", note)

        self.filter_apps = QLineEdit()
        self.filter_apps.setPlaceholderText("youtube,spotify   (blank = react to all audio)")
        self.filter_apps.textChanged.connect(self._on_change)
        form.addRow("App filter", self.filter_apps)

        return box

    # ------------------------------------------------------------------
    # Option dict <-> widgets
    # ------------------------------------------------------------------

    def _current_options(self) -> dict:
        """The live values, seeded from what the app is actually running with."""
        d, o = self._detector, self._overlay
        return {
            "device": settings_mod.load_options().get("device"),
            "scale": o._scale,
            "heavy_mode": d._heavy_mode,
            "heavy_threshold": d.heavy_threshold_db,
            "light_threshold": d.light_threshold_db,
            "bass_sensitivity": d._bass_sensitivity,
            "heavy_band": d.heavy_band,
            "continuous": o._continuous,
            "filter_apps": ",".join(d._filter_apps) if d._filter_apps else "",
            "dual": o._dual,
        }

    def _load(self, opts: dict) -> None:
        self._loading = True
        self._select(self.heavy_mode, opts.get("heavy_mode", "threshold"))
        self._select(self.heavy_band, opts.get("heavy_band", "snare"))
        self.heavy_threshold.setValue(float(opts.get("heavy_threshold", -50.0)))
        self.light_threshold.setValue(float(opts.get("light_threshold", -65.0)))
        self.bass_sensitivity.setValue(float(opts.get("bass_sensitivity", 1.0)))
        self.continuous.setChecked(bool(opts.get("continuous", True)))
        self.dual.setChecked(bool(opts.get("dual", False)))
        self.scale.setValue(float(opts.get("scale", 1.0)))
        self.filter_apps.setText(opts.get("filter_apps") or "")
        self._select(self.device, opts.get("device"))
        self.sync_toggles()
        self._loading = False
        self._sync_enabled()

    def _collect(self) -> dict:
        return {
            "device": self.device.currentData(),
            "scale": self.scale.value(),
            "heavy_mode": self.heavy_mode.currentData(),
            "heavy_threshold": self.heavy_threshold.value(),
            "light_threshold": self.light_threshold.value(),
            "bass_sensitivity": self.bass_sensitivity.value(),
            "heavy_band": self.heavy_band.currentData(),
            "continuous": self.continuous.isChecked(),
            "filter_apps": self.filter_apps.text().strip(),
            "dual": self.dual.isChecked(),
        }

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        idx = combo.findData(value)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def sync_toggles(self) -> None:
        """Re-read Lock/Flip from the overlay, so tray and window stay in step."""
        was = self._loading
        self._loading = True
        self.flipped.setChecked(self._overlay.flipped)
        self.unlocked.setChecked(not self._overlay.locked)
        self._loading = was

    # ------------------------------------------------------------------
    # Live apply
    # ------------------------------------------------------------------

    def _on_change(self, *_) -> None:
        if self._loading:
            return
        opts = self._collect()
        self._detector.apply_settings(opts)
        self._overlay.apply_settings(opts)
        self._sync_enabled()
        self._update_dirty()

    def _sync_enabled(self) -> None:
        self.bass_sensitivity.setEnabled(self.heavy_mode.currentData() == "adaptive")

    def _update_dirty(self) -> None:
        dirty = self._collect() != self._saved
        self._save_btn.setEnabled(dirty)
        self._revert_btn.setEnabled(self._collect() != self._snapshot)
        self.setWindowTitle("FeelTheBeat — Settings" + ("  •" if dirty else ""))

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def _save(self) -> None:
        opts = self._collect()
        settings_mod.save_options_dict(opts)
        self._saved = dict(opts)
        self._snapshot = dict(opts)
        self._update_dirty()
        print(f"[config] Saved settings to {settings_mod.settings_path()}")

    def _revert(self) -> None:
        self._load(self._snapshot)
        self._apply_now()

    def _reset_defaults(self) -> None:
        self._load(dict(self._defaults))
        self._apply_now()

    def _apply_now(self) -> None:
        opts = self._collect()
        self._detector.apply_settings(opts)
        self._overlay.apply_settings(opts)
        self._sync_enabled()
        self._update_dirty()

    # ------------------------------------------------------------------
    # Status / lifecycle
    # ------------------------------------------------------------------

    def _refresh_status(self) -> None:
        d = self._detector
        source = "locked to track BPM" if d._bpm_from_api else "estimated from audio"
        active = "playing" if d.is_audio_active else "silent"
        self._status.setText(
            f"{d.device_name}  ·  {d.SR} Hz  ·  {active}\n"
            f"BPM {d.bpm:.0f}  ({source})"
        )

    def closeEvent(self, event) -> None:
        if self._collect() != self._saved:
            choice = QMessageBox.question(
                self, "Unsaved settings",
                "Save your changes before closing?\n\n"
                "Unsaved changes stay active until you quit, but will not come "
                "back next time you launch.",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if choice == QMessageBox.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.Save:
                self._save()
        self._status_timer.stop()
        event.accept()
