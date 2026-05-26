"""
FeelTheBeat – beat-synced desktop overlay visualizer.

Usage
-----
    python main.py                        # auto-detect system audio
    python main.py --list-devices         # show available input devices
    python main.py --device 16            # use device index 16 (pulse)
    python main.py --device pulse         # match by name substring
    python main.py --pos 200,300          # window top-left position
    python main.py --scale 0.5            # shrink frames to 50 %
    python main.py --heavy-threshold -45  # require louder sub-bass for heavy hits
    python main.py --filter-apps youtube,spotify  # only react to music apps

Controls
--------
  Right-click tray icon → Unlock to drag the window, then Lock again.
  Right-click tray icon → Quit to exit.
"""

import argparse
import os
import queue
import subprocess
import sys


def _load_dotenv() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass

_load_dotenv()

from PyQt5.QtWidgets import QApplication


# ------------------------------------------------------------------
# Device helpers
# ------------------------------------------------------------------

def list_input_devices() -> None:
    import sounddevice as sd
    print("\nAvailable input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  {i:2d}: {d['name']}")
    print()
    print("For system audio capture, use --device pulse (or --device pipewire)")
    print("and ensure your monitor source is the default PulseAudio input.")
    print("Run: pactl set-default-source <monitor-source-name>")
    print("  or use pavucontrol → Input Devices → set the correct monitor as default.")


def find_device_by_hint(hint: str) -> int | None:
    """Accept a device index (int string) or a name substring."""
    import sounddevice as sd
    try:
        idx = int(hint)
        return idx
    except ValueError:
        pass
    for i, d in enumerate(sd.query_devices()):
        if hint.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    return None


def auto_pick_device() -> int | None:
    """Find pulse / pipewire / default device by exact name."""
    import sounddevice as sd
    devices = sd.query_devices()
    for keyword in ("pulse", "pipewire", "default"):
        for i, d in enumerate(devices):
            if d["name"].lower() == keyword and d["max_input_channels"] > 0:
                return i
    return None


# ------------------------------------------------------------------
# PulseAudio / PipeWire monitor auto-configuration
# ------------------------------------------------------------------

def configure_monitor_source() -> str | None:
    """
    Find the currently RUNNING monitor source via pactl and set it as
    the PulseAudio default input so that 'pulse' captures system audio.
    Returns the source name on success, None if nothing is found.
    """
    try:
        r = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=3,
        )
        for line in r.stdout.splitlines():
            parts = line.split()
            # fields: idx  name  driver  format  state  (state is last column)
            if len(parts) >= 3 and "monitor" in parts[1] and parts[-1] == "RUNNING":
                name = parts[1]
                subprocess.run(
                    ["pactl", "set-default-source", name],
                    capture_output=True, timeout=3,
                )
                return name
    except Exception:
        pass
    return None


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="FeelTheBeat – beat-synced animation overlay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--device", default=None,
                    help="Audio input device index or name substring")
    ap.add_argument("--list-devices", action="store_true",
                    help="Print available input devices and exit")
    ap.add_argument("--pos", default="100,100",
                    help="Window top-left as X,Y  (default: 100,100)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="Frame scale factor  (default: 1.0; try 0.5 for half-size)")
    ap.add_argument("--heavy-mode", choices=["threshold", "adaptive"], default="threshold",
                    dest="heavy_mode",
                    help="Heavy-hit detection: 'threshold' = absolute dBFS on sub-bass (default); "
                         "'adaptive' = relative to recent beat history")
    ap.add_argument("--heavy-threshold", type=float, default=-50.0, dest="heavy_threshold",
                    help="Sub-bass peak level (dBFS) required to trigger a heavy hit  (default: -50.0)")
    ap.add_argument("--bass-sensitivity", type=float, default=1.0, dest="bass_sensitivity",
                    help="Adaptive mode: multiplier on the heavy-hit margin  (default: 1.0, higher = fewer heavy hits)")
    ap.add_argument("--peak-hold", type=float, default=0.0, dest="peak_hold_ms",
                    help="Hold the sub-bass peak for this many milliseconds to stabilise "
                         "heavy/light decisions across a kick transient  (default: 0 = off, try 80)")
    ap.add_argument("--light-threshold", type=float, default=-65.0, dest="light_threshold",
                    help="Sub-bass peak level (dBFS) required to trigger a light hit in --bass-only mode  (default: -65.0)")
    ap.add_argument("--bass-only", action="store_true", dest="bass_only",
                    help="Only trigger light hits when sub-bass is present; drop non-bass onsets")
    mode_group = ap.add_mutually_exclusive_group()
    mode_group.add_argument("--phase-locked", action="store_const", const="phase-locked",
                            dest="mode", help="Gate beats to a phase-locked tempo grid (default)")
    mode_group.add_argument("--onset", action="store_const", const="onset",
                            dest="mode", help="Fire on every detected onset directly, no phase gating")
    ap.set_defaults(mode="phase-locked")
    ap.add_argument("--continuous", action="store_true",
                    help="Loop light hits at the BPM rate continuously; heavy hits interrupt on bass")
    ap.add_argument("--debug", action="store_true",
                    help="Open a real-time FFT spectrum window for threshold tuning")
    ap.add_argument("--filter-apps", default=None, dest="filter_apps",
                    help="Comma-separated app name substrings to allow beats from "
                         "(e.g. 'youtube,spotify'). Beats are suppressed when none "
                         "of these apps have an active audio stream. "
                         "Default: no filter (all audio triggers beats).")
    ap.add_argument("--band-mode", choices=["sub-bass", "bass-lowmid"], default="sub-bass",
                    dest="band_mode",
                    help="Frequency bands used for hit detection: "
                         "'sub-bass' = 50-80 Hz for heavy (default); "
                         "'bass-lowmid' = 80-250 Hz for heavy, 250-500 Hz for light")
    ap.add_argument("--no-monitor-detect", action="store_true",
                    help="Skip automatic PulseAudio monitor source detection")
    args = ap.parse_args()

    # ── Device listing ─────────────────────────────────────────────────
    if args.list_devices:
        list_input_devices()
        return

    # ── Auto-configure monitor source ──────────────────────────────────
    if not args.no_monitor_detect:
        src = configure_monitor_source()
        if src:
            print(f"[audio] Monitor source set: {src}")
        else:
            print("[audio] No running monitor source detected; "
                  "using current default input.")
            print("        Run with --list-devices to see options, or "
                  "configure via pavucontrol.")

    # ── Resolve audio device ───────────────────────────────────────────
    if args.device:
        device = find_device_by_hint(args.device)
        if device is None:
            print(f"[error] Could not find device matching '{args.device}'")
            sys.exit(1)
    else:
        device = auto_pick_device()

    # ── Parse window position ──────────────────────────────────────────
    try:
        pos_x, pos_y = map(int, args.pos.split(","))
    except ValueError:
        print("[warn] Could not parse --pos; using 100,100")
        pos_x, pos_y = 100, 100

    # ── Wire up components ─────────────────────────────────────────────
    bq: queue.Queue = queue.Queue()


    filter_apps = [s.strip() for s in args.filter_apps.split(",")] if args.filter_apps else None

    from beat_detector import BeatDetector
    detector = BeatDetector(
        device=device,
        beat_queue=bq,
        heavy_threshold_db=args.heavy_threshold,
        light_threshold_db=args.light_threshold,
        bass_only=args.bass_only,
        filter_apps=filter_apps,
        mode=args.mode,
        heavy_mode=args.heavy_mode,
        bass_sensitivity=args.bass_sensitivity,
        peak_hold_ms=args.peak_hold_ms,
        band_mode=args.band_mode,
    )
    print(f"[audio] Device index: {device!r}  |  Sample rate: {detector.SR} Hz  |  Mode: {args.mode}")
    if filter_apps:
        print(f"[audio] App filter active: {filter_apps}")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # keep alive when window is hidden

    from overlay import OverlayWindow
    _win = OverlayWindow(
        beat_queue=bq,
        pos=(pos_x, pos_y),
        scale=args.scale,
        continuous=args.continuous,
        get_bpm=lambda: detector.bpm,
        get_active=lambda: detector.is_audio_active,
    )

    # ── BPM lookup via MPRIS + Deezer ─────────────────────────────────
    from track_watcher import TrackWatcher
    watcher = TrackWatcher(on_bpm_found=detector.set_bpm)
    print("[track] TrackWatcher enabled (MPRIS + Deezer BPM lookup)")

    if args.debug:
        from spectrum_window import SpectrumWindow
        _spec_win = SpectrumWindow(detector)
        _spec_win.show()
        print("[debug] Spectrum window open.")

    try:
        detector.start()
        if watcher:
            watcher.start()
        print("[audio] Stream started.")
        print("[ui]    Right-click the tray icon (orange dot) to unlock/quit.")
        sys.exit(app.exec_())
    except Exception as exc:
        print(f"[error] {exc}")
        sys.exit(1)
    finally:
        if watcher:
            watcher.stop()
        detector.stop()


if __name__ == "__main__":
    main()
