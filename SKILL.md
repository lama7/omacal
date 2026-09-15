---
name: omacal
description: CalDAV calendar popup for the Omarchy desktop.
version: 0.1.0
author: Gerry
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [omacal, CalDAV, QML, Quickshell, Omarchy, calendar]
---

# omacal Project Skill

CalDAV-backed calendar popup for the Omarchy desktop. Replaces the clock popup
with a proper calendar grid showing events from CalDAV calendars.

## Architecture

- **Backend**: Python package at `backend/`
  - `omacal-api` — HTTP server on port 9876 serving `/api/health`, `/api/calendars`, `/api/events`, `/api/events/today`, `/api/config`; POST `/api/sync` and `/api/events`
  - `omacal sync` — CLI tool to pull CalDAV calendars into SQLite cache
  - Periodic sync runs automatically every `poll_interval` seconds (default 300)
  - Config at `~/.config/omacal/config.json`, DB at `~/.local/share/omacal/calendar.db`

- **Frontend**: Quickshell QML at `frontend/`
  - `omacal-panel.qml` — calendar grid popup (installed as `Panel.qml`)
  - `BarWidget.qml` — bar clock widget with hover tooltip showing calendar name + today's events
  - `Model.js` — shared date math helpers (week start, ISO week, month stepping, format ring)
  - Installed to `~/.config/omarchy/plugins/gerry.clock/`

## Development

```bash
# Backend: venv setup
cd backend
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install caldav icalendar   # already in pyproject

# Run API server in debug mode
.venv/bin/omacal-api --debug

# Run sync
.venv/bin/omacal sync

# Test
curl -s http://127.0.0.1:9876/api/health
curl -s http://127.0.0.1:9876/api/calendars | python3 -m json.tool

# Full install to Omarchy plugin dir + systemd
./install.sh

# Start as service
systemctl --user enable --now omacal.service
```

## Layout Constraint (QML Frontend)

The `KeyboardPanel` in `omacal-panel.qml` sets `contentWidth: panel.fittedContentWidth(360)`.
This is a **hard 360px ceiling**. The month grid, weekday header, header row, and
status text all stay at 360px. Do not widen individual columns — the panel system
clips the 7th day if you do.

## Date Navigation (QML Frontend)

- Month grid day cells carry `year`, `month`, `date` from the grid computation
  (which may be in the previous/next month for leading/trailing cells).
- `gotoDay` receives `new Date(modelData.year, modelData.month, modelData.date)` —
  **do not recompute from `viewMonth` and month-day-1**. Recomputing produces wrong
  offsets (e.g. Jul 26 instead of Aug 30 for Sep 2026 row 0).

## Keyboard Handling (QML Frontend)

All in `PanelKeyCatcher` inside `omacal-panel.qml`:

| Key | Action (month view) | Action (day view) | Action (add form) |
|-----|---------------------|-------------------|-------------------|
| Left/Right arrows | shiftMonth(±1) | shiftDay(±1) | dismissed |
| Up/Down arrows | shiftMonth(±12) | (ignored) | dismissed |
| Enter | root.close() | root.close() | submitAddEvent() |
| ESC | root.close() | root.close() | dismissAddForm() |
| Backspace | backToMonth() | backToMonth() | dismissAddForm() |
| [ / { | shiftMonth(-1) | shiftMonth(-1) | dismissed |
| ] / } | shiftMonth(+1) | shiftMonth(+1) | dismissed |
| t / T | goToToday() | goToToday() | dismissed |
| w / W | toggleWeekStart() | toggleWeekStart() | dismissed |

Note: `root.opened` is NOT readonly — it must flip to `true` before
`controller.show()` is called, otherwise the panel won't receive input.

## Related Skills

- `omacal-backend` — Python backend, API endpoints, CalDAV sync, DB schema
- `omacal-qml-panel` — QML panel layout, width constraints, keyboard navigation
- `omarchy` — Omarchy desktop customization conventions
- `omarchy-qml-panels` — building debugging QML panels for Omarchy plugins
