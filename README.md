# omacal

Calendar popup for the Omarchy desktop. A bar widget replacing the clock label
with a proper calendar: month grid, day view, event add/edit/delete, recurring
events, and a first-run setup flow — pulling from CalDAV servers (Nextcloud,
Fastmail, iCloud, your own, …) or Google Calendar.

## Screenshots

Month view, with the hovered-day preview pane on the left showing that day's
events as you move across the grid:

![Month view](screenshots/month-view-day-preview.png)

Month view showing the preview pane's empty state ("Hover a day to preview"):

![Month view](screenshots/month-view.png)

Day view, showing a day's events with add/edit/delete via click or keyboard:

![Day view](screenshots/day-view.png)

## Features

- **Month grid** with 5 or 6 week rows, colored per-calendar event dots, week
  start toggling (`w`).
- **Day view** of any day's events — click a day, or arrow between days and
  months.
- **Google Calendar** — works alongside CalDAV, including read-only shared and
  holiday calendars.
- **Add / edit / delete events** (keyboard + mouse): summary, location,
  calendar, all-day, start/end time, repeat (daily/weekly/2-weekly/monthly/
  yearly with count or end-date), notes.
- **Recurring events** expanded in the panel; editing one occurrence or the
  whole series, without clobbering the master.
- **All-day and multi-day events** handled correctly.
- **First-run setup** — no manual config file editing to get started.
- **Background sync** every 5 minutes, plus a refresh when the panel opens.
  The calendar list is cached, so a busy server isn't hit with a burst on every
  poll.
- **Reminders** (CalDAV only) with a dedicated section — see [Reminders](#reminders).
- **Calendar management** — a calendar-select dialog to show/hide calendars, add or
  subscribe to calendars, edit owned calendars' name/color, delete them, and
  pick colors — see [Calendars](#calendars).

## Calendars

The calendar-select dialog lists every known calendar with a toggle row each.
Calendars belonging to a **disabled account** (see
[Account settings](#account-settings)) are hidden, both here and in the
add-event Calendar dropdown — enable the account to use them.

- **Show / hide** — toggled-off calendars stop rendering dots and listing
  events in the month/day views.
- **Add (`+`)** — a mini form creates a new named calendar on your server, or
  **subscribes** to an existing one: paste the calendar's URL and the submit
  button reads "Subscribe" instead of "Create". With more than one account
  configured, a **Create calendar on:** picker chooses which account the
  calendar is created on (CalDAV or Google).
- **Color** — pick from preset swatches ("Select calendar color:"); the color
  is saved to the server and comes back on the next sync.
- **Edit** — the pencil button on a row (or `e` on the row under the cursor)
  opens a mini form to rename and/or recolour the calendar. Both fields are
  optional — leave one alone to keep it. Only calendars you own can be edited;
  shared and subscribed rows never show the button. Changes are saved to the
  server and come back on the next sync.
- **Delete / unsubscribe** — right-click or `d` on a row. Calendars you own are
  deleted from the server; shared or subscribed calendars are only
  unsubscribed — the server's data is left untouched.
- **Keyboard** — arrows / `Tab` / `Shift+Tab` move the cursor between rows
  (the **Done** button highlights when the cursor lands on it), `Enter`
  selects, `d` deletes, `e` edits the row under the cursor.

## Account settings

The **≡** button opens Account Settings. Each row is one account with an
enable/disable toggle:

- Disabling an account hides its calendars and stops syncing; nothing is
  deleted, and re-enabling is instant (no re-sync).
- The **+** button adds an account — Nextcloud, Google, or any CalDAV server.
- The pencil opens a read-only detail view (name, provider, server, username,
  calendar count, status). Accounts can carry an optional name so several on
  the same server are easy to tell apart.

## Google calendars

Google Calendar works alongside CalDAV. Add it from **Account Settings → + →
Google**; the dialog walks through the one-time Google Cloud setup and then
opens the browser to sign in. Read-only calendars (shared, public, holidays)
appear too, and can't be edited or used as an event destination.

## Reminders

Reminders are to-do items on a CalDAV calendar — the same add/edit/delete flow
as events, toggled via the **Event / Reminder** buttons in the form.

> **Reminders are CalDAV-only.** Google calendars don't support them: the
> Reminder toggle is blocked on a Google event.

- **Creating** — pick **Reminder** in the add form. A new reminder defaults to
  **All Day** and is routed to the task calendar (a to-do-capable one); the
  calendar dropdown is hidden because there's only that target. Editing an
  existing reminder keeps its own all-day state and calendar.
- **Alarm (heads-up)** — shown only in reminder mode. Timed reminders fire an
  offset before their due time: *5 min / 15 min / 30 min / 1 hour / 2 hours /
  1 day before* (default **1 hour** before due). All-day reminders fire at a
  fixed time on the due date (hour + minute, default **09:00**).
- **Display** — reminders render as an **outlined (hollow) dot** so they read
  distinctly from filled event dots, in the month grid, day view, and preview
  pane.
- **Sync & completion** — reminders sync like events and disappear once marked
  complete or cancelled. A recurring reminder notifies once per occurrence.

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

## Install

Three steps: install the plugin, install its backend, and start the service.

```bash
# 1. Clone + enable the plugin (frontend only)
omarchy plugin add <repo-url> --enable

# 2. Install the Python backend (calendar sync + local API). Run from the
#    plugin folder.
cd ~/.config/omarchy/plugins/omacal
./install.sh
#
#    install.sh also asks whether to replace the stock omarchy.clock widget
#    with omacal in the bar (Y/n). Answer yes to swap them; answer no to keep
#    both and edit ~/.config/omarchy/shell.json yourself.

# 3. Start the service (once; it is enabled to run on login)
systemctl --user start omacal
```

That's it. On first use the clock shows a **"Provider Setup Required"** notice;
**Continue** opens Account Settings, where **Add Provider** connects an account:

- **Nextcloud** — browser sign-in; approve in the browser and you're done (no
  client registration or admin rights needed).
- **Google** — your own Google Cloud client; the dialog walks through the
  one-time setup, then opens the browser to sign in.
- **CalDAV** — any server from address + username + password (a bare
  `https://cloud.example.com` works; the CalDAV path is found automatically).

Passwords live in the system keyring, never in a file.

**Uninstall:**

```bash
cd ~/.config/omarchy/plugins/omacal
./install.sh --uninstall        # removes the backend and its service
omarchy plugin remove omacal    # removes the plugin itself
```

## What each piece does

| Piece | Installed by | Where |
|-------|-------------|-------|
| Frontend (bar widget + panel) | `omarchy plugin add` | `~/.config/omarchy/plugins/omacal/` |
| Bar swap (omacal replaces `omarchy.clock`) | `install.sh` (opt-in Y/n) | `~/.config/omarchy/shell.json` |
| Python backend (sync service) | `install.sh` | venv `~/.local/omacal/venv/`; local API on `127.0.0.1:9876` |
| Background service | `install.sh` | `~/.config/systemd/user/omacal.service` |
| Account config | first-run setup | `~/.config/omacal/config.json` (no passwords — those stay in the keyring) |
| Local event cache | backend | `~/.local/share/omacal/calendar.db` |

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
│   ├── AccountSettingsDialog.qml  #   accounts (toggle / add provider / detail)
│   ├── CalendarToggleDialog.qml   #   show-hide, add, subscribe, edit, delete
│   ├── AddEventForm.qml  #   add/edit event form
│   ├── Model.js          #   date math + clock format ring (shared)
│   └── …                 #   DateEntry, FormLabel, KeypadTextField, …
├── backend/              # Python package: sync service + local API
├── systemd/              # omacal.service (backend unit)
└── install.sh            # backend installer (run from the plugin folder)
```

## License

MIT — see [LICENSE](LICENSE).
