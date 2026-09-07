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
        "password": password,
        "enabled": True,
    }
    cfg.setdefault("calendars", []).append(cal)
    save_config(args.config, cfg)
    print(f"Added {display}. Run 'omacal sync' to pull events.")


def cmd_sync(args):
    """Sync all configured calendars."""
    result = sync_all(args.config)
    print(json.dumps(result, indent=2, default=str))


def cmd_init(args):
    """Create a default config file."""
    cfg = load_config()
    path = args.config or DEFAULT_CONFIG_PATH
    save_config(path, cfg)
    print(f"Config written to {path}")
    print("Edit it to add your CalDAV calendars, then run 'omacal sync'.")


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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
