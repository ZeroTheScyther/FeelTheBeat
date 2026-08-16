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

Saving a setup
--------------
Add --save-config once to make the current flags the defaults, after which the
app can be launched with no arguments at all — from the Start menu, the app
menu, or a double-click:

    python main.py --dual --heavy-mode adaptive --heavy-threshold -50 \
                   --filter-apps youtube,spotify --save-config

  --show-config    print what is saved
  --reset-config   forget it
  --no-dual        override a saved --dual for one run

Controls
--------
  Right-click tray icon → Unlock to drag the window, then Lock again.
  Right-click tray icon → Quit to exit.
"""

import argparse
import os
import queue
import sys

from paths import app_dir, config_dir, setup_output

IS_WINDOWS = sys.platform == "win32"


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from the first .env found, without overriding the env."""
    candidates = [
        config_dir() / ".env",
        app_dir() / ".env",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    for path in candidates:
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
            return
        except OSError:
            continue


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    # Windowed builds have no console: borrow the caller's terminal, or log to
    # file.  Must happen before the first print().
    log = setup_output()
    _load_dotenv()

    ap = argparse.ArgumentParser(
        description="FeelTheBeat – beat-synced animation overlay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--device", default=None,
                    help="Audio input device index or name substring")
    ap.add_argument("--list-devices", action="store_true",
                    help="Print available input devices and exit")
    ap.add_argument("--pos", default=None,
                    help="Window top-left as X,Y  (default: last saved position or 100,100)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="Frame scale factor  (default: 1.0; try 0.5 for half-size)")
    ap.add_argument("--heavy-mode", choices=["threshold", "adaptive"], default="threshold",
                    dest="heavy_mode",
                    help="Heavy-hit detection: 'threshold' = fixed dBFS on snare band (default); "
                         "'adaptive' = threshold tracks rolling average of snare band level")
    ap.add_argument("--heavy-threshold", type=float, default=-50.0, dest="heavy_threshold",
                    help="Snare band peak level (dBFS) required to trigger a heavy hit  (default: -50.0)")
    ap.add_argument("--light-threshold", type=float, default=-65.0, dest="light_threshold",
                    help="Light band peak level (dBFS) required to trigger a light hit  (default: -65.0)")
    ap.add_argument("--bass-sensitivity", type=float, default=1.0, dest="bass_sensitivity",
                    help="Adaptive mode: multiplier on the heavy-hit margin  (default: 1.0, higher = fewer heavy hits)")
    ap.add_argument("--heavy-band", choices=["snare", "kick"], default="snare", dest="heavy_band",
                    help="Frequency band for heavy-hit detection: 'snare' = 220–270 Hz (default); 'kick' = 80–220 Hz")
    ap.add_argument("--no-continuous", action="store_false", dest="continuous",
                    help="Disable continuous light-hit loop (on by default)")
    ap.set_defaults(continuous=True)
    ap.add_argument("--debug", action="store_true",
                    help="Open a real-time FFT spectrum window for threshold tuning")
    ap.add_argument("--filter-apps", default=None, dest="filter_apps",
                    help="Comma-separated app name substrings to allow beats from "
                         "(e.g. 'youtube,spotify'). Beats are suppressed when none "
                         "of these apps have an active audio stream. "
                         "Default: no filter (all audio triggers beats).")
    ap.add_argument("--dual", action="store_true", dest="dual",
                    help="Show two characters side by side across the full monitor width")
    ap.add_argument("--no-dual", action="store_false", dest="dual",
                    help="Disable dual mode (overrides a saved --dual)")
    ap.set_defaults(dual=False)
    ap.add_argument("--no-monitor-detect", action="store_true",
                    help="Skip automatic PulseAudio monitor source detection (Linux only)")
    ap.add_argument("--save-config", action="store_true", dest="save_config",
                    help="Save the options given on this run as the defaults, then start "
                         "normally. Afterwards the app can be launched with no arguments.")
    ap.add_argument("--reset-config", action="store_true", dest="reset_config",
                    help="Forget saved options and exit")
    ap.add_argument("--show-config", action="store_true", dest="show_config",
                    help="Print the saved options and exit")

    # Saved options become the defaults; anything given on the command line
    # still wins, because argparse applies explicit arguments over defaults.
    from settings import (OPTION_KEYS, clear_options, load_options,
                          load_settings, save_options, settings_path)
    # Capture the built-in defaults first — Settings' "Reset to defaults"
    # restores these, not whatever happens to be saved.
    cli_defaults = {k: ap.get_default(k) for k in OPTION_KEYS}
    ap.set_defaults(**load_options())

    args = ap.parse_args()

    if args.reset_config:
        clear_options()
        print(f"[config] Cleared saved options in {settings_path()}")
        return
    if args.show_config:
        saved_opts = load_options()
        print(f"[config] {settings_path()}")
        if saved_opts:
            for k, v in sorted(saved_opts.items()):
                print(f"         {k} = {v!r}")
        else:
            print("         (none saved — run once with your flags plus --save-config)")
        return
    if args.save_config:
        save_options(args)
        print(f"[config] Saved launch options to {settings_path()}")

    if log:
        print(f"[log]   Logging to {log}")

    import audio_backend

    # ── Device listing ─────────────────────────────────────────────────
    if args.list_devices:
        audio_backend.list_input_devices()
        return

    # ── Auto-configure monitor source (Linux; no-op on Windows) ────────
    if not args.no_monitor_detect and not IS_WINDOWS:
        src = audio_backend.configure_monitor_source()
        if src:
            print(f"[audio] Monitor source set: {src}")
        else:
            print("[audio] No running monitor source detected; "
                  "using current default input.")
            print("        Run with --list-devices to see options, or "
                  "configure via pavucontrol.")

    # ── Resolve audio device ───────────────────────────────────────────
    if args.device:
        device = audio_backend.find_device_by_hint(args.device)
        if device is None:
            print(f"[error] Could not find device matching '{args.device}'")
            sys.exit(1)
    else:
        device = audio_backend.auto_pick_device()

    # ── Parse window position ──────────────────────────────────────────
    saved = load_settings()
    if args.pos:
        try:
            pos_x, pos_y = map(int, args.pos.split(","))
        except ValueError:
            print("[warn] Could not parse --pos; using saved or 100,100")
            pos_x = saved.get("window_x", 100)
            pos_y = saved.get("window_y", 100)
    else:
        pos_x = saved.get("window_x", 100)
        pos_y = saved.get("window_y", 100)

    # ── Wire up components ─────────────────────────────────────────────
    bq: queue.Queue = queue.Queue()


    filter_apps = [s.strip() for s in args.filter_apps.split(",")] if args.filter_apps else None

    from beat_detector import BeatDetector
    try:
        detector = BeatDetector(
            device=device,
            beat_queue=bq,
            heavy_threshold_db=args.heavy_threshold,
            light_threshold_db=args.light_threshold,
            filter_apps=filter_apps,
            heavy_mode=args.heavy_mode,
            bass_sensitivity=args.bass_sensitivity,
            heavy_band=args.heavy_band,
        )
    except Exception as exc:
        print(f"[error] Could not open audio capture: {exc}")
        print("        Run with --list-devices to see what is available.")
        sys.exit(1)
    print(f"[audio] Device: {detector.device_name!r}  |  Sample rate: {detector.SR} Hz  "
          f"|  Heavy mode: {args.heavy_mode}")
    if filter_apps:
        print(f"[audio] App filter active: {filter_apps}")

    # Windows groups windows (and picks the taskbar icon) by AppUserModelID;
    # without this the app inherits the host Python's identity and icon.
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "ZeroTheScyther.FeelTheBeat")
        except Exception as exc:
            print(f"[ui] Could not set AppUserModelID: {exc}")

    # HiDPI must be enabled before the QApplication exists, or the overlay lands
    # in the wrong place on the 125/150 % scaling that is common on Windows.
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # keep alive when window is hidden
    app.setApplicationName("FeelTheBeat")
    app.setDesktopFileName("feelthebeat")  # links to the installed .desktop entry

    from icons import app_icon
    app.setWindowIcon(app_icon())

    from overlay import OverlayWindow
    _win = OverlayWindow(
        beat_queue=bq,
        pos=(pos_x, pos_y),
        scale=args.scale,
        continuous=args.continuous,
        get_bpm=lambda: detector.bpm,
        get_active=lambda: detector.is_audio_active,
        queue_heavies=(args.heavy_band == "kick"),
        dual=args.dual,
        detector=detector,
        defaults=cli_defaults,
    )

    # ── BPM lookup via MPRIS + Deezer ─────────────────────────────────
    from track_watcher import TrackWatcher
    watcher = TrackWatcher(on_bpm_found=detector.set_bpm,
                           on_bpm_unavailable=detector.unlock_bpm)
    print("[track] TrackWatcher enabled (MPRIS + Deezer BPM lookup)")

    if args.debug:
        # Same entry point the Settings button uses, so the two cannot drift.
        _win.open_visualizer()
        print("[debug] Spectrum window open.")

    try:
        detector.start()
        if watcher:
            watcher.start()
        print("[audio] Stream started.")
        print("[ui]    Click the tray icon for Settings, Donate and Quit.")
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
