"""OAuth 2.0 for calendar providers that require it (Google, and CalDAV
servers that grow OAuth gates).

Implements the installed-app flow with PKCE — no client secret is ever
embedded; the PKCE verifier proves the public client at exchange time.

  login:  build an authorize URL, catch the redirect on a loopback HTTP
          server, exchange the code for tokens, store the token set in
          the keyring (service "omacal-oauth", one entry per provider).
  token:  return a valid access token, refreshing transparently; raise
          OAuthReauthRequired when the refresh token is dead (revoked in
          account settings, or expired under a Testing-status consent
          screen), which the API layer surfaces like `needs_setup`.

Google quirks handled here (see the OAuth design notes):
  * Refresh tokens are only issued on the FIRST consent — login forces
    prompt=consent + access_type=offline so the flow always captures one
    when Google offers it, and stores it whenever present.
  * Token-set rotation: if a refresh response carries a new refresh_token
    (some providers rotate), it replaces the old one.
  * Revocation surfaces as invalid_grant -> OAuthReauthRequired, never a
    silent None.

Provider config lives in config.json under "oauth", keyed by provider
name:

  "oauth": {
    "google": {
      "client_id": "....apps.googleusercontent.com",
      "client_secret": "....",                       # Google wants it on the
                                                     # token exchange even for
                                                     # Desktop-app clients
      "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
      "token_url": "https://oauth2.googleapis.com/token",
      "scopes": ["https://www.googleapis.com/auth/calendar"],
      "extra_params": {"access_type": "offline", "prompt": "consent"}
    }
  }

`redirect_port` is OPTIONAL: set it to bind that exact loopback port (needed
when the provider compares the redirect URI string, e.g. Nextcloud OAuth2);
leave it out and the OS picks a free port. Google Desktop-app clients accept
any loopback port, so the Google flow leaves it unset.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from urllib.parse import parse_qs, urlencode

import http.server as _http  # loopback redirect receiver (stdlib)
import urllib.error
import urllib.request

logger = logging.getLogger("omacal.oauth")

# Keyring service for OAuth token sets, one entry per provider name.
_SERVICE = "omacal-oauth"

# Skew so a token is refreshed before it actually expires.
_EXPIRY_SKEW_S = 60

# How long the loopback receiver waits for the browser redirect.
_LOGIN_TIMEOUT_S = 300


class OAuthReauthRequired(Exception):
    """Refresh token is dead (revoked/expired): the user must re-consent."""


def _keyring_get(provider: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(_SERVICE, provider)
    except Exception as e:  # noqa: BLE001 - keyring can fail in many ways
        logger.error("Failed to read OAuth token set from keyring: %s", e)
        return None


def _keyring_set(provider: str, value: str) -> bool:
    try:
        import keyring

        keyring.set_password(_SERVICE, provider, value)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to store OAuth token set in keyring: %s", e)
        return False


def _keyring_delete(provider: str) -> bool:
    try:
        import keyring

        keyring.delete_password(_SERVICE, provider)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to delete OAuth token set from keyring: %s", e)
        return False


def _post_form(url: str, form: dict) -> tuple[int, dict]:
    """Form-encode POST; return (status, parsed-json-or-{})."""
    body = urlencode(form).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except (json.JSONDecodeError, OSError):
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256 challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    # PKCE (RFC 7636) demands the BASE64URL alphabet, unpadded: Google
    # strictly rejects a challenge containing '+' or '/' (Nextcloud
    # tolerated it, which is why the bug survived the NC test).
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _store_tokens(provider: str, tokens: dict) -> bool:
    """Persist the token set with an absolute expiry timestamp."""
    tokens = dict(tokens)
    # Nextcloud returns camelCase "expiresIn"; standard is "expires_in".
    expires_in = tokens.get("expires_in") or tokens.get("expiresIn") or 3600
    tokens["expires_at"] = time.time() + int(expires_in) - _EXPIRY_SKEW_S
    return _keyring_set(provider, json.dumps(tokens))


def _load_tokens(provider: str) -> dict | None:
    raw = _keyring_get(provider)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("OAuth token set for %s is corrupt; re-consent needed", provider)
        return None


def oauth_status(provider: str) -> dict:
    """Report consent state for a provider without touching secrets."""
    tokens = _load_tokens(provider)
    if not tokens:
        return {"consented": False}
    return {
        "consented": True,
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "access_expires_at": tokens.get("expires_at"),
        "scopes": (tokens.get("scope") or "").split(),
    }


def start_login(cfg: dict, provider: str) -> dict | None:
    """Bind the loopback receiver and build the authorize URL (NON-blocking).

    Returns a flow dict carrying the listener plus everything the exchange
    needs, or None when the provider has no configured client. The caller MUST
    drive ``finish_login`` (which blocks on the browser leg) on a background
    thread, and SHOULD call ``cancel_login`` if it abandons the flow.

    Loopback port: a provider spec with ``redirect_port`` binds that exact port
    (needed when the redirect URI must be pre-registered, as with Nextcloud's
    confidential OAuth2); absent, the port is 0 -> the OS picks a free one and
    the redirect URI is derived from the actual bound port. Google Desktop-app
    clients accept any loopback port, which is why the Google path leaves it
    unset. The SAME redirect_uri is used on authorize and on the exchange.
    """
    spec = (cfg.get("oauth") or {}).get(provider)
    if not spec or not spec.get("client_id"):
        logger.error("No OAuth client configured for provider %r", provider)
        return None

    confidential = bool(spec.get("client_secret"))
    verifier = challenge = None
    if not confidential:
        verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    sink: dict = {}

    class _Handler(_http.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: D102 - socket API
            fields = parse_qs(self.path.split("?", 1)[-1])
            if fields.get("code", [""])[0] and fields.get("state", [""]) == [state]:
                sink["code"] = fields["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"omacal: you may close this window.")
            else:
                # A cancel_login poke (bare socket connect) lands here; answer
                # and move on so the blocked handle_request returns.
                self.send_response(400)
                self.end_headers()

        def log_message(self, *args: object) -> None:  # noqa: D102
            pass  # one redirect on loopback; no need to spam the journal

    requested = int(spec.get("redirect_port") or 0)
    try:
        srv = _http.HTTPServer(("127.0.0.1", requested), _Handler)
    except OSError as e:
        logger.error("Could not bind the OAuth loopback port %s: %s",
                     requested or "(auto)", e)
        return None
    srv.timeout = _LOGIN_TIMEOUT_S
    port = srv.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"

    params = {
        "client_id": spec["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(spec.get("scopes") or []),
        "state": state,
    }
    if not confidential:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    params.update(spec.get("extra_params") or {})
    auth_url = spec["authorize_url"] + "?" + urlencode(params)

    return {
        "provider": provider,
        "spec": spec,
        "srv": srv,
        "port": port,
        "redirect_uri": redirect_uri,
        "verifier": verifier,
        "confidential": confidential,
        "sink": sink,
        "cancelled": False,
        "auth_url": auth_url,
    }


def finish_login(flow: dict) -> tuple[bool, str]:
    """Wait for the browser redirect, exchange the code, store tokens.

    Blocking — run it on a background thread when called from the API. Returns
    ``(ok, error)``; ``error`` is "cancelled" when ``cancel_login`` unblocked it.
    """
    srv = flow["srv"]
    try:
        srv.handle_request()  # serves exactly one request; returns on timeout
    except OSError:
        pass  # listener was closed underneath us (cancel_login / teardown)
    finally:
        try:
            srv.server_close()
        except OSError:
            pass

    if flow.get("cancelled"):
        return False, "cancelled"
    code = flow["sink"].get("code")
    if not code:
        return False, "timed out waiting for the browser sign-in"

    spec = flow["spec"]
    exchange = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow["redirect_uri"],
        "client_id": spec["client_id"],
    }
    if flow["verifier"]:
        exchange["code_verifier"] = flow["verifier"]
    if flow["confidential"]:
        exchange["client_secret"] = spec["client_secret"]
    status, tokens = _post_form(spec["token_url"], exchange)
    if status != 200:
        logger.error("Token exchange failed: %s", json.dumps(tokens)[:200])
        return False, f"token exchange failed (HTTP {status})"
    if not tokens.get("access_token"):
        return False, "token exchange returned no access_token"
    if not tokens.get("refresh_token"):
        logger.warning("No refresh token in exchange — token set expires with the "
                       "access token; re-consent will be needed (Testing-status "
                       "consent screens do this after 7 days)")
    if not _store_tokens(flow["provider"], tokens):
        return False, "could not store tokens in the system keyring"
    return True, ""


def cancel_login(flow: dict) -> None:
    """Abandon a pending sign-in, unblocking a waiting ``finish_login``.

    Idempotent. Marks the flow cancelled and pokes the loopback listener with a
    throwaway TCP connect so a ``finish_login`` blocked in ``handle_request``
    returns promptly instead of waiting out the timeout.
    """
    if not flow:
        return
    flow["cancelled"] = True
    import socket

    try:
        with socket.create_connection(("127.0.0.1", int(flow.get("port") or 0)), timeout=1):
            pass
    except OSError:
        pass


def oauth_login(cfg: dict, provider: str) -> bool:
    """Interactive PKCE login (CLI). Blocks on the browser leg.

    The API uses ``start_login`` + a background ``finish_login`` instead, so it
    never stalls its single-threaded HTTP server.
    """
    flow = start_login(cfg, provider)
    if flow is None:
        return False
    print(f"OAuth sign-in for {provider!r}: open this URL in a browser —")
    print(flow["auth_url"])
    ok, err = finish_login(flow)
    if not ok:
        logger.error("OAuth login failed: %s", err)
    return ok


def oauth_revoke(provider: str) -> bool:
    """Forget a provider's token set (idempotent: absent counts as done)."""
    if _keyring_get(provider) is None:
        return True
    return _keyring_delete(provider)


def oauth_token(cfg: dict, provider: str) -> str | None:
    """Return a valid access token, refreshing when expired.

    None means "never consented" (setup needed). OAuthReauthRequired means
    the refresh token died — the user must re-consent; the caller should
    clear the token set and surface setup-needed state.
    """
    spec = (cfg.get("oauth") or {}).get(provider)
    if not spec:
        return None
    tokens = _load_tokens(provider)
    if not tokens:
        return None
    if time.time() < tokens.get("expires_at", 0):
        return tokens["access_token"]

    refresh = tokens.get("refresh_token")
    if not refresh:
        _keyring_delete(provider)
        raise OAuthReauthRequired(f"{provider}: access token expired, no refresh token")

    refresh_form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": spec["client_id"],
    }
    if spec.get("client_secret"):
        refresh_form["client_secret"] = spec["client_secret"]
    status, fresh = _post_form(spec["token_url"], refresh_form)
    if status != 200:
        # invalid_grant: revoked in account settings or expired under a
        # Testing-status consent screen. Forget the set; demand re-consent.
        _keyring_delete(provider)
        raise OAuthReauthRequired(f"{provider}: refresh rejected ({json.dumps(fresh)[:120]})")
    # Some providers rotate refresh tokens; keep the new one when present.
    merged = dict(tokens)
    merged.update(fresh)
    if not fresh.get("refresh_token"):
        merged["refresh_token"] = refresh
    if not _store_tokens(provider, merged):
        raise OAuthReauthRequired(f"{provider}: token refresh stored in keyring failed")
    return merged["access_token"]
