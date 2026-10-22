#!/usr/bin/python3
"""Runs the test suites in an isolated environment.

Every test gets a temporary HOME and XDG directories, in-memory GSettings and
keyring, no display, no session bus and no microphone. The schema and the
translations are compiled into the same temporary tree. The real HOME is only
checked afterwards to prove Orfeu Scribe did not create anything there.

Usage: scripts/run_tests.py [unittest arguments, e.g. tests.test_text -k guard]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UUID = "orfeuscribe@lucasmariano003-wq.github.io"
KEEP = ("PATH", "TERM", "TMPDIR", "GI_TYPELIB_PATH", "LD_LIBRARY_PATH", "GST_PLUGIN_SYSTEM_PATH")


def real_traces():
    home = Path.home()
    return [path for path in (home / ".config/orfeuscribe", home / ".local/share/orfeuscribe",
                              home / ".local/bin/orfeuscribe", home / f".local/share/gnome-shell/extensions/{UUID}")
            if path.exists() or path.is_symlink()]


def isolated_environment(base: Path) -> dict:
    env = {key: os.environ[key] for key in KEEP if key in os.environ}
    runtime = base / "run"
    runtime.mkdir(mode=0o700)
    env.update(HOME=str(base / "home"), XDG_CONFIG_HOME=str(base / "home/.config"),
               XDG_DATA_HOME=str(base / "home/.local/share"), XDG_CACHE_HOME=str(base / "home/.cache"),
               XDG_STATE_HOME=str(base / "home/.local/state"), XDG_RUNTIME_DIR=str(runtime),
               GSETTINGS_BACKEND="memory", ORFEUSCRIBE_SECRET_BACKEND="memory",
               ORFEUSCRIBE_SCHEMA_DIR=str(base / "schemas"), ORFEUSCRIBE_LOCALE_DIR=str(base / "build/locale"),
               ORFEUSCRIBE_TEST_BUILD=str(base / "build"),
               LANG="C.UTF-8", LC_ALL="C.UTF-8", GTK_A11Y="none", NO_AT_BRIDGE="1",
               DBUS_SESSION_BUS_ADDRESS=f"unix:path={base}/no-session-bus",
               DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={base}/no-system-bus",
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
    (base / "home").mkdir()
    return env


def main(argv):
    before = real_traces()
    with tempfile.TemporaryDirectory(prefix="orfeuscribe-tests-") as name:
        base = Path(name)
        env = isolated_environment(base)
        (base / "schemas").mkdir()
        subprocess.run(["glib-compile-schemas", "--strict", "--targetdir", str(base / "schemas"),
                        str(ROOT / "extension" / UUID / "schemas")], check=True)
        subprocess.run([sys.executable, str(ROOT / "scripts/compile_translations.py"), str(base / "build")],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        command = [sys.executable, "-m", "unittest"] + (argv or ["discover", "-s", "tests", "-t", "."])
        status = subprocess.run(command, cwd=ROOT, env=env).returncode
        if not argv and shutil.which("gjs"):
            for script in ("tests/test_release.js",):
                result = subprocess.run(["gjs", "-m", script], cwd=ROOT, env=env, capture_output=True, text=True)
                print(result.stdout.strip() or result.stderr.strip())
                status = status or result.returncode
    after = real_traces()
    if after != before:
        print("FAILED: the tests left files in the real HOME:", *after, sep="\n  ")
        return 1
    print("Real HOME untouched by Orfeu Scribe:", "yes" if not after else f"pre-existing {after}")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
