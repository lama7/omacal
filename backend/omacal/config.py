"""Config management for omacal.

Reads ~/.config/omacal/config.json. Falls back to sane defaults.
"""

import json
import os
import time
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


def account_has_credentials(cal: dict) -> bool:
    """True when a ``calendars[]`` ACCOUNT entry has credentials it can use.

    A config entry can outlive its secrets -- a wiped keyring, a revoked
    Google grant, a Testing-status consent screen that expired. Such an
    account can neither sign in nor sync, so it must not count as "configured"
    just because the entry exists.

    Google (``provider="google"``) needs a usable OAuth token set: a refresh
    token (always able to mint a new access token) or a still-valid access
    token. A CalDAV account needs a password in the keyring -- which lives
    under the account's server URL (see keyring_store).
    """
    if (cal.get("provider") or "caldav") == "google":
        from omacal.oauth import oauth_status

        status = oauth_status("google")
        if not status.get("consented"):
            return False
        if status.get("has_refresh_token"):
            return True
        return (status.get("access_expires_at") or 0) > time.time()

    url = (cal.get("url") or "").strip()
    if not url:
        return False
    from omacal.keyring_store import has_password

    return has_password(url, cal.get("username"))


def needs_setup(cfg: dict | None = None) -> bool:
    """True when no enabled account is configured with usable credentials.

    "Configured" means an entry in ``calendars[]`` that is BOTH enabled AND
    has credentials it can actually use (a keyring password for CalDAV, a
    usable OAuth token set for Google). An enabled entry whose secrets are
    gone is not enough -- nothing can sync, so the panel should fall back to
    the first-run setup notice (see ``account_has_credentials``).
    """
    cfg = cfg if cfg is not None else load_config()
    return not any(
        c.get("enabled", True) and account_has_credentials(c)
        for c in cfg.get("calendars", [])
    )
