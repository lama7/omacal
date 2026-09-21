"""Password storage in the system keyring (gnome-keyring via Secret Service).

The CalDAV password is the only secret omacal holds. It lives in exactly one
place: the user's keyring, keyed by the calendar's server URL. Config.json and
the SQLite cache store the username but never the password.

Uses the `keyring` package, which on this desktop resolves to the Secret
Service backend backed by gnome-keyring. If the keyring is unavailable the
functions degrade to None / False rather than raising, so the rest of the
pipeline can report a clear "setup needed" instead of crashing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("omacal.keyring")

# Service name under which omacal passwords are stored. The "username" slot
# holds the calendar's server URL (unique per calendar source).
_SERVICE = "omacal"


def _key(username: str) -> str:
    return username


def store_password(url: str, username: str, password: str) -> bool:
    """Store a calendar password in the keyring. Returns True on success."""
    try:
        import keyring

        keyring.set_password(_SERVICE, _key(url), password)
        return True
    except Exception as e:  # noqa: BLE001 - keyring can fail in many ways
        logger.error("Failed to store password in keyring: %s", e)
        return False


def get_password(url: str, username: str) -> str | None:
    """Return the stored password for a calendar URL, or None."""
    try:
        import keyring

        return keyring.get_password(_SERVICE, _key(url))
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to read password from keyring: %s", e)
        return None


def has_password(url: str, username: str) -> bool:
    """True if a password is stored for this calendar URL."""
    return get_password(url, username) is not None


def delete_password(url: str, username: str) -> bool:
    """Remove a stored password. Returns True on success (or if none existed)."""
    try:
        import keyring

        keyring.delete_password(_SERVICE, _key(url))
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to delete password from keyring: %s", e)
        return False
