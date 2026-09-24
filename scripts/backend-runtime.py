#!/usr/bin/env python3
"""Check and update the omacal Python backend from inside the running plugin.

The QML panel calls this over a Quickshell Process to detect when the backend
venv's version has fallen behind the frontend's and to trigger a reinstall.
`omarchy plugin update` only syncs the plugin's files (frontend + backend
source); it does NOT reinstall the backend venv at ~/.local/omacal/venv. This
script is the missing half.

Commands:
  status   -> JSON {"installed","required","needsUpdate","needsInstall",
                    "serviceActive","error"}
  install  -> run install.sh, restart the service, re-check, print JSON

The version the backend SHOULD be running comes from the `backend-version` pin
at the repo root (bumped in lockstep with manifest.json / pyproject.toml /
omacal.__init__). The installed version is read from the live backend package
in the venv, not from the API, so the check works whether or not the service
is up.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Backend install locations — must match install.sh.
VENV = Path.home() / ".local/omacal/venv"
VENV_PYTHON = VENV / "bin" / "python"
CONFIG = Path.home() / ".config/omacal/config.json"
SERVICE = "omacal.service"


def pin():
    return (ROOT / "backend-version").read_text().strip()


def required_version():
    """Version from the pin file; fall back to omacal.__version__ in the repo."""
    try:
        v = pin()
        if v:
            return v
    except OSError:
        pass
    return "unknown"


def read_installed():
    """Read the installed package version via the venv's own interpreter."""
    if not VENV_PYTHON.exists():
        return None
    try:
        out = subprocess.run(
            [str(VENV_PYTHON), "-c", "import omacal; print(omacal.__version__)"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def service_active():
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE],
            capture_output=True, text=True, timeout=10,
        )
        return (out.stdout.strip() == "active"
                or (out.returncode == 0 and out.stdout.strip() != "inactive"))
    except Exception:
        return None


def status():
    installed = read_installed()
    required = required_version()
    active = service_active()
    installed_known = bool(installed)
    required_known = required not in ("", "unknown")
    return {
        "installed": installed,
        "required": required,
        # requested Install unless an installed version is known.
        "needsInstall": not installed_known,
        "needsUpdate": installed_known and required_known and installed != required,
        "serviceActive": active,
    }


def run_install():
    # install.sh resolves its own plugin dir from its location; ROOT IS that dir.
    install_sh = ROOT / "install.sh"
    if not install_sh.exists():
        return {"error": "install.sh not found in plugin folder"}
    res = subprocess.run(
        ["bash", str(install_sh)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600,
    )
    ok = res.returncode == 0
    # Restart so the new version actually serves.
    if ok:
        subprocess.run(["systemctl", "--user", "restart", SERVICE],
                       capture_output=True, timeout=60)
    st = status()
    st["error"] = "" if ok else (res.stderr.strip() or res.stdout.strip() or "install failed")
    return st


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "install":
            result = run_install()
        else:
            result = status()
        json.dump(result, sys.stdout)
        print()
        return 0
    except Exception as e:  # report, never traceback, to the QML caller
        json.dump({"error": f"backend-runtime.py: {e}"}, sys.stdout)
        print()
        return 1


if __name__ == "__main__":
    sys.exit(main())