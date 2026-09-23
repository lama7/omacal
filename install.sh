#!/usr/bin/env bash
# omacal — backend installer for the omacal Omarchy plugin.
#
# Run this from the plugin folder (the cloned repo), i.e.:
#   omarchy plugin add <repo-url> --enable
#   cd ~/.config/omarchy/plugins/omacal && ./install.sh
#
# It installs the Python backend for the plugin and leaves the QML frontend in
# place (that is this repo — the shell loads it from here). Run it again to
# reinstall/repair; pass --uninstall to remove the backend service.
set -euo pipefail

# This script lives at the repo root, which IS the plugin folder when cloned by
# `omarchy plugin add`. ${BASH_SOURCE[0]} (not $0) survives a PATH symlink.
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PLUGIN_DIR/backend"

# Backend install locations — outside the plugin folder, because the plugin
# folder is owned by `omarchy plugin update` and any venv inside it would be
# wiped or left stale on the next update.
VENV_DIR="$HOME/.local/omacal/venv"
DEST="$HOME/.local/bin"
SYSTEMD_USER="$HOME/.config/systemd/user"
SERVICE="omacal.service"

UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --uninstall|-u) UNINSTALL=1 ;;
    -h|--help)
      echo "Usage: install.sh [--uninstall]"
      echo "Installs (default) or removes the omacal Python backend."
      echo "On install it also offers to replace the stock omarchy.clock in the bar."
      exit 0
      ;;
    *) echo "install.sh: unknown option: $arg" >&2; exit 1 ;;
  esac
done

replace_stock_clock() {
  local shell_json="$HOME/.config/omarchy/shell.json"
  if [[ ! -f "$shell_json" ]]; then
    echo "==> No $shell_json found — skipping bar replacement."
    return 0
  fi

  # Only act if the stock clock is actually present in the bar.
  if ! grep -q '"omarchy.clock"' "$shell_json"; then
    echo "==> omarchy.clock not present in $shell_json — nothing to replace."
    return 0
  fi

  echo
  read -r -p "Replace stock omarchy.clock with omacal in the bar? [Y/n] " -n 1 reply
  echo
  case "${reply:-Y}" in
    Y|y) ;;
    *) echo "==> Leaving omarchy.clock in place. Keep omacal by editing shell.json yourself."; return 0 ;;
  esac

  if ! command -v python3 >/dev/null 2>&1; then
    echo "installation requires python3 — skipping bar replacement" >&2
    return 0
  fi

  python3 - "$shell_json" <<'PY'
import json, sys, shutil
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)

# Find omarchy.clock in the center layout.
center = data.get("bar", {}).get("layout", {}).get("center", [])
idx = next((i for i, w in enumerate(center)
            if isinstance(w, dict) and w.get("id") == "omarchy.clock"), None)
if idx is None:
    raise SystemExit(0)  # not present; nothing to do

# Reveal the omacal widget at the same position, only if not already there
# (plugin add --enable may have already appended omacal elsewhere).
omacal_pos = next((i for i, w in enumerate(center)
                   if isinstance(w, dict) and w.get("id") == "omacal"), None)
clock_widget = center[idx]

if omacal_pos is None:
    # Replace the stock clock entry with omacal in place.
    if "format" in clock_widget or "weekStartDay" in clock_widget:
        # Carry over the user's clock formatting onto the omacal widget.
        center[idx] = {"id": "omacal", **{k: v for k, v in clock_widget.items()
                                          if k not in ("id", "formatAlt", "verticalFormat")}}
    else:
        center[idx] = {"id": "omacal"}
else:
    # omacal already present (appended by plugin add). Drop the stock clock;
    # keep omacal's existing entry. Preserve order by replacing the clock's
    # slot with omacal if omacal sits at a later index (moves it to clock's slot).
    if omacal_pos > idx:
        clock_props = {k: v for k, v in clock_widget.items() if k != "id"}
        omacal_widget = center[omacal_pos]
        if clock_props and not any(k in omacal_widget for k in ("format", "weekStartDay")):
            omacal_widget = {"id": "omacal", **clock_props}
        center[idx] = omacal_widget
        center.pop(omacal_pos)
    else:
        center.pop(idx)

# Point the anchor at omacal if it pointed at the stock clock.
if data.get("bar", {}).get("centerAnchor") == "omarchy.clock":
    data["bar"]["centerAnchor"] = "omacal"

# Back up then write.
bak = path + ".postinstall.bak"
shutil.copy2(path, bak)
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"==> Replaced omarchy.clock with omacal in {path} (backup: {bak})")
PY
}

uninstall_backend() {
  echo "==> Stopping and disabling omacal.service..."
  systemctl --user stop omacal.service 2>/dev/null || true
  systemctl --user disable omacal.service 2>/dev/null || true

  echo "==> Removing symlinks..."
  for cmd in omacal omacal-api; do
    rm -f "$DEST/$cmd"
  done

  echo "==> Removing systemd unit $SYSTEMD_USER/$SERVICE..."
  rm -f "$SYSTEMD_USER/$SERVICE"

  echo "==> Removing venv $VENV_DIR..."
  rm -rf "$VENV_DIR"

  systemctl --user daemon-reload
  echo "==> Backend uninstalled."
  echo "     Remove the plugin itself with: omarchy plugin remove omacal"
  exit 0
}

if (( UNINSTALL )); then
  uninstall_backend
fi

echo "==> Ensuring backend source is present..."
[[ -f "$BACKEND_DIR/pyproject.toml" ]] || {
  echo "install.sh: backend/pyproject.toml not found — run this from the omacal plugin folder" >&2
  exit 1
}

echo "==> Creating venv at $VENV_DIR..."
python3 -m venv "$VENV_DIR"

echo "==> Installing backend and dependencies (locked + hash-verified)..."
# Install every dependency from the committed, pinned, hash-locked
# requirements.lock with --require-hashes. There is no live `pip upgrade` and
# no unverified resolution: pip only accepts the exact versions/hashes this
# commit pins, so a later install cannot fetch different dependency bytes.
"$VENV_DIR/bin/pip" install --require-hashes --quiet -r "$BACKEND_DIR/requirements.lock"
# Install the local package with --no-deps (its deps are already installed
# above) and --no-build-isolation (use the pinned setuptools/wheel from the
# lock), so building it does not fetch the build backend from the index either.
"$VENV_DIR/bin/pip" install --quiet --no-deps --no-build-isolation "$BACKEND_DIR"

echo "==> Linking CLI tools into $DEST..."
mkdir -p "$DEST"
for cmd in omacal omacal-api; do
  ln -sf "$VENV_DIR/bin/$cmd" "$DEST/$cmd"
  echo "  linked $DEST/$cmd"
done

echo "==> Installing systemd user service..."
mkdir -p "$SYSTEMD_USER"
cp "$PLUGIN_DIR/systemd/omacal.service" "$SYSTEMD_USER/$SERVICE"
systemctl --user daemon-reload
systemctl --user enable omacal.service

replace_stock_clock

echo "==> Done."
echo "     QML frontend is already in place at $PLUGIN_DIR (the shell loads it)."
echo "     Start the daemon with: systemctl --user start omacal"
echo "     First run shows a 'Setup needed' popup — enter your CalDAV server once."