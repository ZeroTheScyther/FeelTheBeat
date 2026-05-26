"""
Transparent, frameless, always-on-top overlay that plays either the
LightHit or HeavyHit frame sequence whenever a beat is detected.

While locked the window passes all input events through to whatever is
below it (Qt.WindowTransparentForInput).  Unlock via the system-tray
icon to drag the window to a new position, then lock again.
"""

import queue
import re
import time
from pathlib import Path

from PIL import Image
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QImage, QPainter, QPixmap, QTransform
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

FRAMES_DIR = Path(__file__).parent / "Frames"
FRAME_MS = 20  # 50 fps  (matches the 0.02 s delay baked into filenames)


def _load_frames(folder: Path) -> list[QPixmap]:
    """Load all *.gif files from *folder*, sorted by embedded frame number."""
    paths = sorted(
        folder.glob("*.gif"),
        key=lambda p: int(re.search(r"(\d+)", p.name).group(1)),
    )
    if not paths:
        raise FileNotFoundError(f"No .gif frames found in {folder}")
    out: list[QPixmap] = []
    for p in paths:
        img = Image.open(p).convert("RGBA")
        raw = img.tobytes("raw", "RGBA")
        qi = QImage(raw, img.width, img.height, img.width * 4,
                    QImage.Format_RGBA8888)
        out.append(QPixmap.fromImage(qi))
    return out


class OverlayWindow(QWidget):

    def __init__(self, beat_queue: queue.Queue,
                 pos: tuple[int, int] = (100, 100),
                 scale: float = 1.0,
                 continuous: bool = False,
                 get_bpm: callable = None,
                 get_active: callable = None):
        super().__init__()
        self._beat_queue = beat_queue
        self._locked = True
        self._drag_origin = None

        # ── Load animation frame sets ──────────────────────────────────
        self._light = _load_frames(FRAMES_DIR / "LightHit")
        self._heavy = _load_frames(FRAMES_DIR / "HeavyHit")

        if scale != 1.0:
            w = int(self._light[0].width() * scale)
            h = int(self._light[0].height() * scale)
            self._light = [
                f.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                for f in self._light
            ]
            self._heavy = [
                f.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                for f in self._heavy
            ]

        fw, fh = self._light[0].width(), self._light[0].height()

        # ── Animation state ────────────────────────────────────────────
        self._frames = self._light
        self._frame_idx = len(self._light) - 1  # start at last (idle) frame
        self._current_pixmap: QPixmap = self._frames[self._frame_idx]
        self._playing = False

        # ── Flip state ────────────────────────────────────────────────
        self._flipped = False


        # ── Continuous BPM loop ────────────────────────────────────────
        self._continuous = continuous
        self._get_bpm = get_bpm or (lambda: 120.0)
        self._get_active = get_active or (lambda: True)
        self._next_beat_t: float = 0.0  # fires immediately on first tick

        # ── Window setup ───────────────────────────────────────────────
        self.setWindowTitle("FeelTheBeat")
        self.setFixedSize(fw, fh)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._apply_flags()
        self.move(pos[0], pos[1])

        # ── Animation timer (fires every FRAME_MS ms) ─────────────────
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(FRAME_MS)
        self._anim_timer.timeout.connect(self._step)

        # ── Beat-queue poll timer (100 Hz) ────────────────────────────
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(10)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

        # ── System tray ────────────────────────────────────────────────
        self._setup_tray()

        self.show()

    # ------------------------------------------------------------------
    # Painting  (draw directly — avoids QLabel background clobbering)
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if self._flipped:
            t = QTransform()
            t.translate(self.width(), 0)
            t.scale(-1, 1)
            painter.setTransform(t)
        painter.drawPixmap(0, 0, self._current_pixmap)
        painter.end()

    # ------------------------------------------------------------------
    # Window flags / clickthrough toggle
    # ------------------------------------------------------------------

    def _apply_flags(self) -> None:
        flags = (Qt.FramelessWindowHint
                 | Qt.WindowStaysOnTopHint
                 | Qt.X11BypassWindowManagerHint)
        if self._locked:
            flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.show()

    def _toggle_lock(self) -> None:
        self._locked = not self._locked
        self._apply_flags()
        if self._locked:
            self._lock_act.setText("🔓 Unlock  (drag to reposition)")
        else:
            self._lock_act.setText("🔒 Lock  (clickthrough)")

    def _toggle_flip(self) -> None:
        self._flipped = not self._flipped
        self._flip_act.setText("⬌ Flip  (face right)" if self._flipped else "⬌ Flip  (face left)")
        self.update()

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        pm = QPixmap(16, 16)
        pm.fill(QColor(255, 90, 30))
        self._tray = QSystemTrayIcon(QIcon(pm), self)

        menu = QMenu()

        self._lock_act = QAction("🔓 Unlock  (drag to reposition)", self)
        self._lock_act.triggered.connect(self._toggle_lock)
        menu.addAction(self._lock_act)

        self._flip_act = QAction("⬌ Flip  (face left)", self)
        self._flip_act.triggered.connect(self._toggle_flip)
        menu.addAction(self._flip_act)

        menu.addSeparator()

        self._bpm_act = QAction("BPM: detecting…", self)
        self._bpm_act.setEnabled(False)
        menu.addAction(self._bpm_act)

        menu.addSeparator()

        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)

        self._tray.setContextMenu(menu)
        self._tray.setToolTip("FeelTheBeat")
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self._toggle_lock()

    # ------------------------------------------------------------------
    # Beat queue polling
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        now = time.monotonic()

        # Drain the beat queue
        try:
            while True:
                anim, bpm = self._beat_queue.get_nowait()
                self._bpm_act.setText(f"BPM: {bpm:.0f}")
                if anim == "heavy":
                    self._fire("heavy")
                    if self._continuous:
                        self._next_beat_t = now + 60.0 / max(self._get_bpm(), 1.0)
                elif not self._continuous:
                    self._fire("light")
        except queue.Empty:
            pass

        # Continuous light loop
        if self._continuous:
            if not self._get_active():
                # No music playing — stay ready to fire immediately on resume
                self._next_beat_t = now
            elif now >= self._next_beat_t:
                bpm = max(self._get_bpm(), 1.0)
                self._next_beat_t = now + 60.0 / bpm
                self._bpm_act.setText(f"BPM: {bpm:.0f}")
                self._fire("light")

    # ------------------------------------------------------------------
    # Animation
    # ------------------------------------------------------------------

    def _fire(self, anim: str) -> None:
        """Trigger (or restart) the given animation from frame 0."""
        self._frames = self._heavy if anim == "heavy" else self._light
        self._frame_idx = 0
        self._current_pixmap = self._frames[0]
        self._playing = True
        self.update()
        if not self._anim_timer.isActive():
            self._anim_timer.start()

    def _step(self) -> None:
        """Advance one frame; stop at the last frame (hold idle pose)."""
        if not self._playing:
            self._anim_timer.stop()
            return
        self._frame_idx += 1
        if self._frame_idx >= len(self._frames):
            self._frame_idx = len(self._frames) - 1
            self._playing = False
            self._anim_timer.stop()
        self._current_pixmap = self._frames[self._frame_idx]
        self.update()

    # ------------------------------------------------------------------
    # Mouse drag (only when unlocked)
    # ------------------------------------------------------------------

    def mousePressEvent(self, ev) -> None:
        if not self._locked and ev.button() == Qt.LeftButton:
            self._drag_origin = ev.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev) -> None:
        if self._drag_origin and not self._locked and (
                ev.buttons() == Qt.LeftButton):
            self.move(ev.globalPos() - self._drag_origin)

    def mouseReleaseEvent(self, ev) -> None:
        self._drag_origin = None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, ev) -> None:
        self._tray.hide()
        ev.accept()
