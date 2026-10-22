"""Synthetic, isolated window setup shared by smoke_window.py and capture_screenshots.py."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UUID = "orfeuscribe@lucasmariano003-wq.github.io"


def isolated_environment(base: Path, language: str, display: dict) -> dict:
    """Temporary HOME and runtime dir, no session bus, in-memory settings and keyring."""
    (base / "run").mkdir(mode=0o700, exist_ok=True)
    (base / "home").mkdir(exist_ok=True)
    (base / "schemas").mkdir(exist_ok=True)
    subprocess.run(["glib-compile-schemas", "--strict", "--targetdir", str(base / "schemas"),
                    str(ROOT / "extension" / UUID / "schemas")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/compile_translations.py"), str(base / "build")],
                   check=True, capture_output=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(base / "home"),
           "XDG_RUNTIME_DIR": str(base / "run"), "XDG_CONFIG_HOME": str(base / "home/.config"),
           "XDG_DATA_HOME": str(base / "home/.local/share"), "XDG_CACHE_HOME": str(base / "home/.cache"),
           "GSETTINGS_BACKEND": "memory", "ORFEUSCRIBE_SECRET_BACKEND": "memory",
           "GIO_USE_VFS": "local", "GTK_USE_PORTAL": "0",
           "ORFEUSCRIBE_SCHEMA_DIR": str(base / "schemas"), "ORFEUSCRIBE_LOCALE_DIR": str(base / "build/locale"),
           "GTK_A11Y": "none", "NO_AT_BRIDGE": "1", "GTK_IM_MODULE": "gtk-im-context-simple",
           "G_DEBUG": "fatal-criticals", "LANG": "C.UTF-8", "LANGUAGE": language, "PYTHONDONTWRITEBYTECODE": "1"}
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={base}/no-bus")
    env.update(display)
    return env


def build(language: str):
    """Returns (app, window, ids) with synthetic history, rescue and settings. Call inside the isolated child."""
    sys.path.insert(0, str(ROOT))
    from unittest import mock
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, Gtk
    from orfeuscribe import i18n
    i18n.reload(languages=[language])
    import orfeuscribe.audio
    patcher = mock.patch.object(orfeuscribe.audio, "microphones", return_value=[("Synthetic microphone", None)])
    patcher.start()
    from tests.support import FakeConnection, FakeProvider, FakeSettings, wav
    from orfeuscribe import service
    from orfeuscribe.config import Config
    from orfeuscribe.credentials import MemoryStore
    from orfeuscribe.history import History
    from orfeuscribe.rescue import RescueStore
    from orfeuscribe.vocabulary import parse_vocabulary
    if not Gtk.init_check():
        return None, None, None
    home = Path(os.environ["HOME"])

    class Isolated(service.OrfeuScribeApplication):
        def do_startup(self):
            # Only the toolkit's startup: no settings, D-Bus name, recorder or watcher.
            Adw.Application.do_startup(self)
    app = Isolated(background=True, config_dir=home / "config", history_path=home / "data/history.db")
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    app.config = Config(home / "config")
    app.config.update(stt_base_url="https://speech.example.org/v1", stt_model="example-transcribe",
                      revision_model="example-chat", input_language="en" if language == "en" else "pt")
    app.settings = FakeSettings(**{"toggle-shortcut": ["<Super><Alt>d"], "raw-shortcut": ["<Super><Alt>z"]})
    app.connection = FakeConnection()
    app.connection.call = lambda *args: None
    app.credentials = MemoryStore()
    app.history = History(home / "data/history.db")
    app.rescue = RescueStore(home / "data/rescue")
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config/vocabulary.txt").write_text("Zephyrine Labs\nKestrel Park\nzefirin => Zephyrine\n")
    app.vocab = parse_vocabulary((home / "config/vocabulary.txt").read_text())
    app.provider = FakeProvider()
    app.provider.configured = True
    portuguese = language.startswith("pt")
    entries = [
        ("org.gnome.TextEditor", "email", "oi ana nova linha a reunião fica para quarta às três" if portuguese else
         "hi ana new line the meeting moves to wednesday at three",
         "Oi, Ana,\nA reunião fica para quarta, às três." if portuguese else "Hi Ana,\nThe meeting moves to Wednesday at three."),
        ("Slack", "message", "pode mandar o relatório amanhã cedo" if portuguese else "can you send the report early tomorrow",
         "Pode mandar o relatório amanhã cedo?" if portuguese else "Can you send the report early tomorrow?"),
        ("firefox", "prompt", "resume esse artigo em cinco tópicos sem inventar números" if portuguese else
         "summarize this article in five bullet points without inventing numbers",
         "Resuma este artigo em cinco tópicos, sem inventar números." if portuguese else
         "Summarize this article in five bullet points, without inventing numbers."),
    ]
    ids = []
    for app_name, mode, raw, final in entries:
        ids.append(app.history.add(app=app_name, title="", mode=mode, raw=raw, final=final, provider="speech.example.org",
                                   model="example-chat", recording=6, stt=0.9, polishing=1.2, total=8.1,
                                   input_language="pt" if portuguese else "en"))
    for n in range(2):
        ident = app.history.add(app="gedit", title="", mode="dictation", raw=f"zefirin {n}", final=f"Zefirin {n}.",
                                provider="speech.example.org", model="example-chat", recording=1, stt=1, polishing=1, total=3)
        app.history.amend(ident, f"Zephyrine {n}.")
    app.rescue.save(wav(3), reason="error", message="Could not reach the service. Check the connection and the server URL.",
                    context={"wm_class": "org.gnome.TextEditor"})
    app.last_record = app.history.get(ids[0])
    from orfeuscribe.window import OrfeuScribeWindow
    window = OrfeuScribeWindow(app)
    return app, window, ids
