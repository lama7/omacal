"""Browser-assisted provider sign-in: a shared flow registry, Nextcloud
Login Flow v2, and Google OAuth loopback.

Both providers are the same shape — start a flow, hand the user a URL to open
in their browser, poll until the provider hands back credentials. The registry
below is the "shared plumbing": an in-process dict of flow_id -> state with a
TTL.

The two differ in HOW the credentials come back:
  * Nextcloud Login Flow v2 is a poll-only protocol — the browser leg happens
    entirely on the server, so `nextcloud_poll` does the work inline.
  * Google uses a loopback redirect (see omacal.oauth): a one-shot HTTP server
    on 127.0.0.1 catches the redirect, which means SOMETHING must block waiting
    for it. Because the API server is single-threaded, `google_start` runs that
    blocking wait+exchange on a DAEMON thread and writes the outcome into the
    flow entry; `google_poll` just reads it.

Flow state lives ONLY in this process. That is deliberate: the API is a single
long-lived service, and a restart abandoning an in-flight flow is harmless (the
user simply starts the sign-in again).

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


def _pop_flow(flow_id: str) -> dict | None:
    """Remove and return a flow WITHOUT cancelling anything attached to it."""
    with _LOCK:
        return _FLOWS.pop(flow_id, None)


def drop_flow(flow_id: str) -> None:
    """Remove a flow, cancelling any in-flight browser sign-in it owns.

    Idempotent. The cancel happens outside the lock (it does socket work) so a
    Google flow blocked in its loopback wait wakes up and clears its daemon
    thread instead of lingering until the redirect timeout.
    """
    entry = _pop_flow(flow_id)
    if entry and entry.get("oauth"):
        from omacal.oauth import cancel_login

        cancel_login(entry["oauth"])


# --- Nextcloud Login Flow v2 ------------------------------------------------


def _normalise_server(server: str) -> str:
    server = (server or "").strip()
    if server and not urlparse(server).scheme:
        server = "https://" + server
    return server.rstrip("/")


def nextcloud_start(server: str, name: str | None = None) -> dict:
    """Begin a Nextcloud Login Flow v2.

    Returns {"flow_id", "login_url"} where `login_url` must be opened in the
    user's browser. ``name`` is the user's optional ACCOUNT label, carried
    through the flow so it can be stored when the credentials come back.
    Raises ValueError (user-facing message) when the server does not answer the
    login flow.
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
        name=(name or "").strip(),
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
        "name": entry.get("name") or "",
    }


# --- Google OAuth (loopback redirect) ---------------------------------------


def google_start(cfg: dict, name: str | None = None) -> dict:
    """Begin a Google OAuth sign-in.

    Binds the loopback receiver and builds the authorize URL, then runs the
    blocking wait+exchange on a DAEMON thread (see the module docstring for
    why). Returns {"flow_id", "login_url"} where `login_url` must be opened in
    the user's browser. Raises ValueError (user-facing message) when no OAuth
    client is configured for Google.

    The flow entry's ``status`` moves pending -> ok | error | cancelled; the
    token set is stored in the keyring by the worker, the calendars[] account
    is committed by the API poll on "ok".
    """
    from omacal.oauth import finish_login, start_login

    flow = start_login(cfg, "google")
    if flow is None:
        raise ValueError("Google OAuth client is not configured (client_id missing)")

    flow_id = new_flow(
        kind="google-login",
        provider="google",
        name=(name or "").strip(),
        status="pending",
        error="",
        oauth=flow,
    )

    def _work() -> None:
        ok, err = finish_login(flow)
        entry = get_flow(flow_id)
        if entry is None:
            return  # cancelled or aged out; nothing to record
        if flow.get("cancelled"):
            entry["status"] = "cancelled"
        elif ok:
            entry["status"] = "ok"
        else:
            entry["status"] = "error"
            entry["error"] = err

    threading.Thread(target=_work, daemon=True, name=f"oauth-{flow_id[:8]}").start()
    return {"flow_id": flow_id, "login_url": flow["auth_url"]}


def google_poll(flow_id: str) -> dict:
    """Poll one Google sign-in flow.

    Status: pending | ok | cancelled | error | unknown. "ok" carries the
    optional account label and CONSUMES the flow (the token set is already in
    the keyring; the API commits the account).
    """
    entry = get_flow(flow_id)
    if entry is None:
        return {"status": "unknown"}

    status = entry.get("status") or "pending"
    if status == "pending":
        return {"status": "pending"}
    if status == "ok":
        name = entry.get("name") or ""
        _pop_flow(flow_id)  # done: hand the account back exactly once
        return {"status": "ok", "name": name}
    # error | cancelled — surface it and consume the flow.
    error = entry.get("error") or ""
    _pop_flow(flow_id)
    return {"status": status, "error": error}
