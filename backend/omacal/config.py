"""Config management for omacal.

Reads ~/.config/omacal/config.json. Falls back to sane defaults.
"""

import json
import os
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "omacal" / "config.json"
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "omacal" / "calendar.db"
DEFAULT_PORT = 9876
DEFAULT_POLL_INTERVAL = 300  # seconds


def default_config() -> dict:
    return {
        "calendars": [],
        "database": str(DEFAULT_DB_PATH),
        "port": DEFAULT_PORT,
        "poll_interval": DEFAULT_POLL_INTERVAL,
        "log_level": "info",
    }


def load_config(path: Path | None = None) -> dict:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return default_config()
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default_config()
    # Merge with defaults so new keys aren't missing.
    merged = default_config()
    merged.update(cfg)
    merged.setdefault("calendars", [])
    return merged


def save_config(path: Path | None = None, cfg: dict | None = None) -> None:
    path = path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg if cfg is not None else load_config(path), f, indent=2)
