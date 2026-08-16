import json
from pathlib import Path

from paths import app_dir, config_dir

_SETTINGS_FILE = config_dir() / "settings.json"
_LEGACY_FILE = Path(__file__).resolve().parent / "settings.json"


def _migrate_legacy() -> None:
    """One-time copy of the old repo-local settings.json into the config dir."""
    if _SETTINGS_FILE.exists() or _LEGACY_FILE == _SETTINGS_FILE:
        return
    for candidate in (_LEGACY_FILE, app_dir() / "settings.json"):
        try:
            if candidate.exists():
                _SETTINGS_FILE.write_text(candidate.read_text())
                return
        except OSError:
            continue


_migrate_legacy()


# Launch options that may be persisted, so the app can be started bare — from
# the Start menu, the app menu, or a double-click — with no flags to retype.
OPTION_KEYS = (
    "device",
    "scale",
    "heavy_mode",
    "heavy_threshold",
    "light_threshold",
    "bass_sensitivity",
    "heavy_band",
    "continuous",
    "filter_apps",
    "dual",
)


def load_settings() -> dict:
    try:
        return json.loads(_SETTINGS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_settings(data: dict) -> None:
    current = load_settings()
    current.update(data)
    try:
        _SETTINGS_FILE.write_text(json.dumps(current, indent=2))
    except OSError as exc:
        print(f"[warn] Could not save settings to {_SETTINGS_FILE}: {exc}")


def load_options() -> dict:
    """Persisted launch options, filtered to known keys."""
    opts = load_settings().get("options", {})
    if not isinstance(opts, dict):
        return {}
    return {k: v for k, v in opts.items() if k in OPTION_KEYS}


def save_options(args) -> None:
    """Persist the current launch options as the new defaults."""
    save_settings({"options": {k: getattr(args, k) for k in OPTION_KEYS}})


def clear_options() -> None:
    save_settings({"options": {}})


def settings_path() -> Path:
    return _SETTINGS_FILE
