# omacal

CalDAV-backed calendar popup for the Omarchy desktop. A bar widget replacing
the clock label with a proper calendar: month grid, day view, event
add/edit/delete, recurring events, and a first-run setup flow — pulling from
any CalDAV server (Nextcloud, Fastmail, iCloud, your own, …).

```
┌──────────────┐
│  September 2026  │   ← click the clock in the bar
│ Mo Tu We Th Fr Sa Su │
│    1  2  3  4  5  6 │   · dots mark days with events
│  7  · 9 10 11 12 13 │
│ 14 15 16 17 18 19 20 │
│ 21 22 23 24 25 26 27 │
│ 28 29 30             │
└──────────────┘
```

## Install

Two steps: install the plugin, then install its backend daemon.

```bash
# 1. Clone + enable the plugin (shell only, loads the QML frontend)
omarchy plugin add <repo-url> --enable

# 2. Install the Python backend (CalDAV sync + local API). Run from the
#    plugin folder.
cd ~/.config/omarchy/plugins/omacal
./install.sh

# 3. Start the daemon (once; it is enabled to run on login)
systemctl --user start omacal
```

That's it. On first use the clock shows a "Setup needed" popup — enter your
CalDAV server address (a bare `https://cloud.example.com` works; discovery
finds the CalDAV path), your username, and password. The password is stored in
the system keyring, never in a file.

**Uninstall:**

```bash
cd ~/.config/omarchy/plugins/omacal
./install.sh --uninstall        # removes backend, venv, and systemd unit
omarchy plugin remove omacal    # removes the plugin itself
```

### What each piece does

| Piece | Installed by | Where |
|-------|-------------|-------|
| QML frontend (bar widget + panel) | `omarchy plugin add` | `~/.config/omarchy/plugins/omacal/` |
| Python backend + sync daemon | `install.sh` | venv `~/.local/omacal/venv/`, API on `127.0.0.1:9876` |
| systemd user service | `install.sh` | `~/.config/systemd/user/omacal.service` |
| CalDAV config | first-run setup | `~/.config/omacal/config.json` (URL/username only; password in keyring) |
| Event cache (SQLite) | backend | `~/.local/share/omacal/calendar.db` |

## Features

- **Month grid** with 5 or 6 week rows, colored per-calendar event dots, week
  start toggling (`w`).
- **Day view** of any day's events — click a day, or arrow between days and
  months.
- **Add / edit / delete events** (keyboard + mouse): summary, location,
  calendar, all-day, start/end time, repeat (daily/weekly/2-weekly/monthly/
  yearly with count or end-date), notes.
- **Recurring events** expanded client-side; editing one occurrence (EXDATE)
  or the whole series, without clobbering the master.
- **All-day and multi-day events** handled with correct iCalendar semantics.
- **First-run setup** — no manual config file editing to get started.
- **Incremental sync** (RFC 6578) + on-open refresh; the cache window keeps
  one-offs for -30/+90 days and syncs every 60s.

## Keyboard

| Key | Month view | Day view | Add/edit form |
|-----|-----------|----------|----------------|
| `←` / `→` | prev/next month | prev/next day | form field |
| `↑` / `↓` | prev/next year | — | form field |
| `[` / `]` | prev/next month | prev/next month | — |
| `t` | go to today | go to today | — |
| `w` | toggle week start | toggle week start | — |
| `+` | — | new event | — |
| `Enter` | close | close *(new event: submit)* | save |
| `Esc` | close | close | dismiss form |
| `Backspace` | — | back to month | dismiss form |

## Development

```bash
git clone <repo-url> omacal && cd omacal

# Backend
python3 -m venv backend/.venv
backend/.venv/bin/pip install -e backend/
backend/.venv/bin/omacal-api --debug        # serves 127.0.0.1:9876

# Frontend — this repo IS the plugin folder, so point the shell at a clone:
#   cp -r . ~/.config/omarchy/plugins/omacal    (or `omarchy plugin add`)
# The plugin hot-reloads on file save; a full `pkill -SIGUSR1 quickshell`
# reloads BarWidget.qml if a change doesn't take.

curl -s http://127.0.0.1:9876/api/health    # {"status":"ok",...}
```

Validate a clone as a plugin folder:

```bash
omarchy plugin validate .
```

## Layout

```
omacal/
├── manifest.json         # plugin manifest (root — this repo is a plugin folder)
├── frontend/             # QML bar widget + calendar panel
│   ├── BarWidget.qml     #   bar label + panel host
│   ├── omacal-panel.qml  #   month/day calendar (installed as Panel.qml)
│   ├── SetupForm.qml     #   first-run CalDAV setup
│   ├── AddEventForm.qml  #   add/edit event form
│   ├── Model.js          #   date math + clock format ring (shared)
│   └── …                 #   DateEntry, FormLabel, KeypadTextField, …
├── backend/              # Python package: sync daemon + local API
├── systemd/              # omacal.service (backend unit)
└── install.sh            # backend installer (run from the plugin folder)
```

## License

MIT — see [LICENSE](LICENSE).
