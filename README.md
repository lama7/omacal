# omacal

CalDAV-backed calendar popup for the Omarchy desktop. Replaces the clock popup with a proper calendar grid showing events from CalDAV calendars.

## Architecture

- **Backend**: Python package (`omacal-api` server + `omacal` sync CLI) at `backend/`
  - `omacal-api` serves `/api/health`, `/api/calendars`, `/api/events`, `/api/events/today`, `/api/config`, `POST /api/sync` on port 9876
  - `omacal sync` pulls events from configured CalDAV calendars into SQLite cache
  - Periodic sync runs automatically every `poll_interval` seconds (from config.json)
  - Config at `~/.config/omacal/config.json`, DB at `~/.local/share/omacal/calendar.db`
- **Frontend**: Quickshell QML files at `frontend/`
  - `BarWidget.qml` — clock bar widget with hover tooltip showing calendar name + today's events
  - `omacal-panel.qml` — calendar grid panel (month/week views with event display)
  - Installed to `~/.config/omarchy/plugins/gerry.clock/`

## Project layout

```
omacal/
├── backend/
│   ├── omacal/          # Python package
│   │   ├── __init__.py
│   │   ├── config.py     # Config load/save
│   │   ├── db.py         # SQLite schema + operations
│   │   ├── sync.py       # CalDAV sync
│   │   ├── api.py        # REST API server
│   │   └── cli.py        # CLI entrypoints
│   ├── pyproject.toml
│   └── .venv/           # Python virtualenv
├── frontend/
│   ├── omacal-panel.qml # Calendar grid popup
│   ├── manifest.json    # Plugin manifest
│   └── Model.js         # Date math helpers
├── systemd/
│   └── omacal.service   # Systemd user service
├── install.sh
└── .venv/               # Symlink to backend/.venv
```

## Setup

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Usage

```bash
# Initialize config
omacal init

# Edit config to add your CalDAV calendars
# Then sync events
omacal sync

# Start the API server
omacal-api --port 9876

# Or run as a systemd user service
systemctl --user enable --now omacal.service
```

The clock popup should now show a calendar grid with your events.
