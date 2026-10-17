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
    # Never persist the password in config.json — it lives in the keyring.
    # Strip it from any calendar dicts before writing.
    cfg = cfg if cfg is not None else load_config(path)
    for cal in cfg.get("calendars", []):
        cal.pop("password", None)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def account_key(cal: dict) -> str:
    """Stable key for a config.json ``calendars[]`` ACCOUNT entry.

    The Google account is ``"google"``; a CalDAV account is keyed by its
    principal URL, because every CalDAV account shares ``provider='caldav'``
    and the principal is what tells them apart. This is also the key a cached
    DB row's ``principal_url`` maps to (see the API's row helper), and the key
    the panel sends to pick which account a new calendar is created on.
    """
    if (cal.get("provider") or "caldav") == "google":
        return "google"
    return (cal.get("url") or "").rstrip("/")


def needs_setup(cfg: dict | None = None) -> bool:
    """True when no enabled calendar is configured (fresh install)."""
    cfg = cfg if cfg is not None else load_config()
    return not any(c.get("enabled", True) for c in cfg.get("calendars", []))
