#!/bin/bash
# Screenshots of the real window, with synthetic data, in a private Xvfb display and D-Bus session.
# Nothing touches the running desktop, the real keyring, settings or microphone.
# Requires: xvfb-run (xorg-server-xvfb), dbus-run-session (dbus).
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec /usr/bin/python3 "$root/scripts/run_ui_proof.py" "$@"
