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

## Event Creation (Backend)

### POST /api/events fields

The API accepts `summary` (str), `calendar_id` (int), `start` (ISO datetime),
`end` (optional ISO datetime), `all_day` (optional bool), and `location`
(optional str). If `end` is omitted, the backend defaults to start + 1 hour
(timed) or start + 1 day (all-day).

### CalDAV add_event accepts arbitrary VEVENT properties

`caldav.Calendar.add_event(**kwargs)` → `create_ical(**props)` → `Component.add(prop, value)`.
Any valid iCalendar VEVENT property can be passed as a kwarg: `location`,
`description`, `categories`, `status`, `transparency`, `priority`, `url`,
`sequence`, `class_` (Python keyword workaround for `CLASSIFICATION`).
Properties with hyphens (`last-modified`, `recurrence-id`) cannot be passed as
Python kwargs — use `**`{'recurrence-id': value}`` or the `ical_fragment`
string approach instead.

### Threading a new field end-to-end (API → sync → DB)

1. `api.py`: `data.get("field", default)` in the POST handler, pass to `create_event`
2. `sync.py`: add parameter to `create_event()`, forward to both `caldav.add_event()` and `db.add_event()`
3. `db.py`: add parameter to `add_event()`, insert at the correct column position in the VALUES tuple

### Recurrence-ID

Used to identify a single instance of a recurring event for modifications
(move or cancel one occurrence). The override event must share the same `UID`
as the parent series and set `RECURRENCE-ID` to the original start datetime of
the instance being modified. Only needed when modifying recurring events — not
required when creating new standalone events.

## Add-Event Form Layout (QML)

The add-event form (`addEventForm` Column) contains: Title (full-width TextField),
Location (full-width TextField), Calendar (label + right-justified Dropdown), and
two date/time rows (label + date TextField + [gap] + hour `:` minute Dropdowns).

### Right-justification via Item + anchors

`Row` lays out children sequentially — the rightmost child's position depends on
all preceding child widths, making right-justification impossible. Replace the
Row with `Item { width: dayContent.width; height: controlHeight }` and use
explicit anchors (`anchors.right: parent.right` on the right-aligned item).

### Anchor chain for time controls

Visual order `[hour][:][minute]` left-to-right with the minute dropdown at the
right edge requires a right-to-left anchor chain:
1. Minute Dropdown: `anchors.right: parent.right`
2. Colon Text: `anchors.right: minuteDropdown.left`
3. Hour Dropdown: `anchors.right: colon.left`
A common mistake produces `[colon][hour][minute]` instead — check that the
colon is anchored to the minute dropdown's left, and the hour is anchored to
the colon's left.

### Dynamic width to halve a layout gap

When left item is left-anchored and right group is right-anchored, gap =
`parent.width - left_fixed - right_fixed`. To make the left item absorb half
the gap: `width: base + (parent.width - total_fixed) / 2` where `total_fixed`
= label + left margin + right group total (including internal margins).

### Sliding a field right

Increase `anchors.leftMargin` (offset from anchor target), not `anchors.left`.
The anchor target stays `startLabel.right`; the margin pushes the field rightward
without changing what it's anchored to.

### Pitfall: duplicate anchor declarations

QML uses last-assignment-wins for the same property. If an element ends up with
two `anchors.right` lines (left behind by a partial patch), the last one
silently wins and may create a circular dependency (hour↔colon anchored to each
other). After any anchor edit, grep the element for duplicate `anchors.right` or
`anchors.rightMargin` lines.

## Related Skills

- `omacal-backend` — Python backend, API endpoints, CalDAV sync, DB schema
- `omacal-qml-panel` — QML panel layout, width constraints, keyboard navigation
- `omarchy` — Omarchy desktop customization conventions
- `omarchy-qml-panels` — building debugging QML panels for Omarchy plugins
