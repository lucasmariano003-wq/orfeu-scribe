#!/usr/bin/python3
"""Start the display and session bus with private user directories as well."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile


def main(argv):
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="orfeuscribe-display-") as temporary:
        base = Path(temporary)
        for name in ("home", "run", "config", "data", "cache", "state"):
            (base / name).mkdir(mode=0o700)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(base / "home"),
            "XDG_RUNTIME_DIR": str(base / "run"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_CACHE_HOME": str(base / "cache"),
            "XDG_STATE_HOME": str(base / "state"),
            "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={base}/no-system-bus",
            "GIO_USE_VFS": "local",
            "GTK_USE_PORTAL": "0",
            "GSETTINGS_BACKEND": "memory",
            "ORFEUSCRIBE_SECRET_BACKEND": "memory",
            "ORFEUSCRIBE_UI_PROOF": "1",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = ["xvfb-run", "-a", "-s", "-screen 0 1400x1000x24 -nolisten tcp",
                   "dbus-run-session", "--", sys.executable,
                   str(root / "scripts/capture_screenshots.py"), *argv]
        return subprocess.run(command, cwd=root, env=env).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
