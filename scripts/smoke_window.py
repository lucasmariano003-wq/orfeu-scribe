#!/usr/bin/python3
"""Builds the main window and exercises its refresh paths WITHOUT showing it.

This is a construction smoke test, not a visual proof: GTK 4 cannot initialize
without a display connection, so it opens one, but it never maps a surface and
nothing appears on screen. Everything else is isolated (see ui_fixture.py):
temporary HOME and XDG_RUNTIME_DIR (no audio server, dconf or accessibility
bus), no session bus, in-memory GSettings and keyring, and a fake microphone
list. GTK criticals are fatal.

Usage: smoke_window.py [--language pt_BR]
For screenshots in an isolated Xvfb, see scripts/capture_screenshots.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from ui_fixture import build, isolated_environment  # noqa: E402

EXPECTED = {"en": ["Dictation", "History", "Vocabulary", "Settings"],
            "pt_BR": ["Ditado", "Histórico", "Vocabulário", "Configurações"]}


def child(language):
    app, window, ids = build(language)
    if window is None:
        print("SKIP: no display connection available")
        return 0
    for name in ("dictation", "history", "vocabulary", "settings"):
        window.stack.set_visible_child_name(name)
    for state in ("idle", "listening", "transcribing", "polishing", "done", "error"):
        app.state = state
        app.last_error = "Synthetic error" if state == "error" else ""
        app.live_text = "a provisional sentence" if state == "listening" else ""
        window.refresh()
    app.file_progress = {"name": "talk.ogg", "stage": "transcribing", "number": 2, "position": 30, "duration": 60}
    window.refresh()
    window.edit_drafts[ids[0]] = "Edited text."
    window.refresh_history()
    window._toggle_specialized("outline", True)
    window._toggle_specialized("outline", False)
    window._sync_revision()
    assert not window.get_mapped(), "the window must never be shown"
    titles = [window.stack.get_page(window.stack.get_child_by_name(name)).get_title()
              for name in ("dictation", "history", "vocabulary", "settings")]
    assert titles == EXPECTED.get(language, titles), titles
    print(f"OK: window built and refreshed in {language} without being shown; tabs: {', '.join(titles)}")
    window.destroy()
    return 0


def main(argv):
    if "--child" in argv:
        return child(argv[argv.index("--child") + 1])
    language = argv[argv.index("--language") + 1] if "--language" in argv else "en"
    display = {}
    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    if wayland:
        # The socket lives in the real runtime dir; the child gets an absolute path and a private runtime dir.
        if not wayland.startswith("/"):
            wayland = os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), wayland)
        display = {"WAYLAND_DISPLAY": wayland, "GDK_BACKEND": "wayland"}
    elif os.environ.get("DISPLAY"):
        display = {"DISPLAY": os.environ["DISPLAY"], "GDK_BACKEND": "x11"}
    with tempfile.TemporaryDirectory(prefix="orfeuscribe-smoke-") as name:
        env = isolated_environment(Path(name), language, display)
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={name}/no-bus"
        return subprocess.run([sys.executable, __file__, "--child", language], cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
