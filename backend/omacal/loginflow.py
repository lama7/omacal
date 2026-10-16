"""Browser-assisted provider sign-in: a shared flow registry + Nextcloud
Login Flow v2.

Both this and the Google OAuth path (C3, not yet wired) are the same shape —
start a flow, hand the user a URL to open in their browser, poll until the
provider hands back credentials. The registry below is the "shared plumbing":
an in-process dict of flow_id -> state with a TTL.

Flow state lives ONLY in this process. That is deliberate: the API is a single
long-lived service, and a restart abandoning an in-flight flow is harmless (the
user simply starts the sign-in again).

Nextcloud Login Flow v2 (verified against the Nextcloud developer docs):
  * `POST <server>/index.php/login/v2` (anonymous) returns
    {"poll": {"token", "endpoint"}, "login": "<url>"}
  * the user opens `login` in their DEFAULT browser and signs in (2FA included)
  * `POST <endpoint>` with form field `token` returns 404 until done, then 200:
    {"server", "loginName", "appPassword"} — returned exactly ONCE
  * the poll token is valid for 20 minutes

No OAuth client registration and no Nextcloud admin rights are needed, which is
why this beats Nextcloud's confidential-client OAuth2 for the default path.
See references/provider-setup-flows.md (omacal-backend skill).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from urllib.parse import urlparse

# Shared, reused by the Google flow when it lands.
from omacal.oauth import _post_form  # noqa: PLC2701 - shared stdlib POST helper

logger = logging.getLogger("omacal.loginflow")

# Login Flow v2's poll token is valid 20 minutes; drop flows after that.
_TTL_S = 20 * 60

_FLOWS: dict[str, dict] = {}
_LOCK = threading.Lock()


# --- shared flow registry ---------------------------------------------------


def new_flow(**state: object) -> str:
    """Register a pending flow and return its id."""
    flow_id = secrets.token_urlsafe(24)
    state["started"] = time.time()
    with _LOCK:
        _FLOWS[flow_id] = state
    return flow_id


def get_flow(flow_id: str) -> dict | None:
    """Return a live flow's state, dropping (and hiding) it once expired."""
    with _LOCK:
        entry = _FLOWS.get(flow_id)
        if entry is None:
            return None
        if time.time() - float(entry.get("started", 0)) > _TTL_S:
            _FLOWS.pop(flow_id, None)
            return None
        return entry


def drop_flow(flow_id: str) -> None:
    with _LOCK:
        _FLOWS.pop(flow_id, None)


# --- Nextcloud Login Flow v2 ------------------------------------------------


def _normalise_server(server: str) -> str:
    server = (server or "").strip()
    if server and not urlparse(server).scheme:
        server = "https://" + server
    return server.rstrip("/")


def nextcloud_start(server: str) -> dict:
    """Begin a Nextcloud Login Flow v2.

    Returns {"flow_id", "login_url"} where `login_url` must be opened in the
    user's browser. Raises ValueError (user-facing message) when the server
    does not answer the login flow.
    """
    server = _normalise_server(server)
    if not server:
        raise ValueError("Server URL is required")

    status, data = _post_form(f"{server}/index.php/login/v2", {})
    data = data or {}
    poll = data.get("poll") or {}
    login_url = data.get("login")
    if status != 200 or not login_url or not poll.get("token") or not poll.get("endpoint"):
        raise ValueError(
            f"{server} did not answer the Nextcloud login flow (HTTP {status}). "
            "Check the server URL."
        )

    flow_id = new_flow(
        kind="nextcloud-login",
        server=server,
        token=poll["token"],
        endpoint=poll["endpoint"],
    )
    return {"flow_id": flow_id, "login_url": login_url}


def nextcloud_poll(flow_id: str) -> dict:
    """Poll one Nextcloud login flow.

    Status: pending | ok | expired | unknown | error.
    "ok" carries {server, login_name, app_password} and CONSUMES the flow
    (Login Flow v2 hands the credentials back exactly once).
    """
    entry = get_flow(flow_id)
    if entry is None:
        # Either never existed or aged out past the token's 20-minute life.
        return {"status": "unknown"}

    status, data = _post_form(entry["endpoint"], {"token": entry["token"]})
    if status == 404:
        return {"status": "pending"}  # not authorised in the browser yet
    if status != 200:
        drop_flow(flow_id)
        return {"status": "error", "error": f"login flow poll failed (HTTP {status})"}

    data = data or {}
    login_name = data.get("loginName")
    app_password = data.get("appPassword")
    if not login_name or not app_password:
        drop_flow(flow_id)
        return {"status": "error", "error": "login flow returned no credentials"}

    drop_flow(flow_id)
    return {
        "status": "ok",
        "server": data.get("server") or entry["server"],
        "login_name": login_name,
        "app_password": app_password,
    }
