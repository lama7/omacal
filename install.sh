#!/usr/bin/env bash
# omacal — install helper. Run from the project root or anywhere.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$PROJECT_ROOT/backend/.venv"
DEST="$HOME/.local/bin"

# Ensure venv exists and is installed
if [ ! -f "$VENV/bin/omacal" ]; then
    echo "==> Creating venv and installing deps..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -e "$PROJECT_ROOT/backend"
fi

# Symlink CLI tools into ~/.local/bin
mkdir -p "$DEST"
for cmd in omacal omacal-api; do
    ln -sf "$VENV/bin/$cmd" "$DEST/$cmd"
    echo "  linked $DEST/$cmd"
done

# Install systemd user service
SYSTEMD_USER="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER"
cp "$PROJECT_ROOT/systemd/omacal.service" "$SYSTEMD_USER/omacal.service"
echo "  installed systemd user service"

# Copy QML frontend files to the Omarchy plugin directory
PLUGIN_DIR="$HOME/.config/omarchy/plugins/gerry.clock"
mkdir -p "$PLUGIN_DIR"
cp "$PROJECT_ROOT/frontend/BarWidget.qml" "$PLUGIN_DIR/BarWidget.qml"
cp "$PROJECT_ROOT/frontend/omacal-panel.qml" "$PLUGIN_DIR/Panel.qml"
cp "$PROJECT_ROOT/frontend/manifest.json" "$PLUGIN_DIR/manifest.json"
cp "$PROJECT_ROOT/frontend/Model.js" "$PLUGIN_DIR/Model.js"
echo "  installed QML frontend"

# Reload systemd and enable
systemctl --user daemon-reload
systemctl --user enable omacal.service 2>/dev/null || true
echo "==> Done. Start with: systemctl --user start omacal"
