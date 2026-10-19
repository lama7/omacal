"""CLI entrypoints for omacal."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from omacal.config import DEFAULT_CONFIG_PATH, load_config, save_config
from omacal.sync import sync_all

logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s", stream=sys.stderr)
logger = logging.getLogger("omacal")


def cmd_syncconfig(args):
    """Interactively configure calendars."""
    cfg = load_config(args.config)
    print("Current calendars:")
    for i, c in enumerate(cfg.get("calendars", []), 1):
        print(f"  {i}. {c.get('display_name', 'unknown')} @ {c.get('url', '?')}")

    print("\nAdd a calendar (CalDAV).")
    url = input("CalDAV URL (e.g. https://calendar.example/dav/): ").strip()
    if not url:
        return

    display = input("Display name: ").strip() or "Calendar"
    username = input("Username (leave blank for none): ").strip() or None
    password = input("Password (leave blank for none): ").strip() or None

    cal = {
        "url": url,
        "display_name": display,
        "username": username,
        "enabled": True,
    }
    cfg.setdefault("calendars", []).append(cal)
    save_config(args.config, cfg)
    # The password lives in the keyring, never config.json.
    from omacal.keyring_store import store_password
    if username and password:
        store_password(url, username, password)
    print(f"Added {display}. Run 'omacal sync' to pull events.")


def cmd_sync(args):
    """Sync all configured calendars (user-initiated: force fresh discovery)."""
    result = sync_all(args.config, force_discovery=True)
    print(json.dumps(result, indent=2, default=str))


def cmd_init(args):
    """Create a default config file."""
    cfg = load_config()
    path = args.config or DEFAULT_CONFIG_PATH
    save_config(path, cfg)
    print(f"Config written to {path}")
    print("Edit it to add your CalDAV calendars, then run 'omacal sync'.")


def cmd_oauth(args):
    """OAuth consent/token management (login, status, revoke)."""
    from omacal.oauth import oauth_login, oauth_status, oauth_revoke

    provider = args.provider
    if args.oauth_cmd == "login":
        cfg = load_config(args.config)
        if oauth_login(cfg, provider):
            print(f"Consented {provider}; tokens stored in the keyring.")
        else:
            print(f"OAuth login for {provider} did not complete.")
            sys.exit(1)
    elif args.oauth_cmd == "status":
        if provider:
            print(json.dumps({provider: oauth_status(provider)}, indent=2))
        else:
            cfg = load_config(args.config)
            out = {name: oauth_status(name) for name in (cfg.get("oauth") or {})}
            print(json.dumps(out, indent=2))
    elif args.oauth_cmd == "revoke":
        if not provider:
            print("revoke needs a provider name")
            sys.exit(1)
        oauth_revoke(provider)
        print(f"Token set for {provider} forgotten.")


def main():
    parser = argparse.ArgumentParser(prog="omacal", description="omacal CalDAV calendar CLI")
    parser.add_argument("--config", type=Path, default=None, help="config file path")

    sub = parser.add_subparsers()
    sub.required = True

    p_init = sub.add_parser("init", help="create default config")
    p_init.set_defaults(func=cmd_init)

    p_sync = sub.add_parser("sync", help="sync calendars")
    p_sync.set_defaults(func=cmd_sync)

    p_config = sub.add_parser("config", help="interactive config")
    p_config.set_defaults(func=cmd_syncconfig)

    p_oauth = sub.add_parser("oauth", help="OAuth consent/token management")
    oauth_sub = p_oauth.add_subparsers(dest="oauth_cmd")
    oauth_sub.required = True
    p_oauth_login = oauth_sub.add_parser("login", help="run the OAuth consent flow")
    p_oauth_login.add_argument("provider")
    p_oauth_login.set_defaults(func=cmd_oauth)
    p_oauth_status = oauth_sub.add_parser("status", help="show OAuth consent state")
    p_oauth_status.add_argument("provider", nargs="?")
    p_oauth_status.set_defaults(func=cmd_oauth)
    p_oauth_revoke = oauth_sub.add_parser("revoke", help="forget stored tokens")
    p_oauth_revoke.add_argument("provider")
    p_oauth_revoke.set_defaults(func=cmd_oauth)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
