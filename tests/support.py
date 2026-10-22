"""Test doubles shared by the suites. All data here is synthetic."""
from __future__ import annotations

import io
import tempfile
import time
import wave
from pathlib import Path

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib
    GTK = True
except (ImportError, ValueError):
    GTK = False


def wav(seconds=1.0, value=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(int(value).to_bytes(2, "little", signed=True) * int(16000 * seconds))
    return output.getvalue()


class FakeSettings:
    """The GSettings keys the service and window read, in memory."""

    def __init__(self, **values):
        self.values = {"paused": False, "translation-language": "English",
                       "translation-languages": ["English", "Spanish"], "fixed-mode": "auto",
                       "recording-mode": "toggle", "watcher-enabled": False,
                       "offered-modes": ["dictation", "literal", "message", "email", "prompt"]}
        self.values.update(values)

    def get_boolean(self, key):
        return self.values[key]

    get_string = get_boolean

    def get_strv(self, key):
        return list(self.values.get(key, []))

    def set_string(self, key, value):
        self.values[key] = value

    set_strv = set_boolean = set_string

    def connect(self, *_args):
        return 1

    def disconnect(self, *_args):
        pass


class FakeRecorder:
    def __init__(self, audio=b""):
        self.started = 0
        self.audio = audio
        self.on_pcm = None

    def start(self, microphone=""):
        self.started += 1

    def take_chunk(self):
        audio, self.audio = self.audio, b""
        return audio

    def stop(self):
        return self.take_chunk()

    def cancel(self):
        self.audio = b""


class FakeConnection:
    def __init__(self):
        self.signals = []

    def emit_signal(self, destination, path, interface, name, variant):
        self.signals.append((name, variant.unpack()))

    def names(self):
        return [name for name, _ in self.signals]

    def values(self, wanted):
        return [value for name, value in self.signals if name == wanted]


class FakeChime:
    def __init__(self):
        self.played = []

    def play(self, kind):
        self.played.append(kind)


class FakeProvider:
    """Service double: answers come from a list, calls are recorded."""
    name = "fake"

    def __init__(self, raw="let us meet on wednesday afternoon", replies=None, fail_stt=False, fail_polish=False,
                 can_revise=True):
        self.model = self.last_model = "fake-model"
        self.raw, self.replies = raw, list(replies or [])
        self.fail_stt, self.fail_polish = fail_stt, fail_polish
        self.can_revise = can_revise
        self.languages, self.contexts, self.calls = [], [], []

    def transcribe(self, audio, lang, keyterms=()):
        from orfeuscribe.providers import ProviderError
        self.languages.append(lang)
        if self.fail_stt:
            raise ProviderError("Could not reach the service. Check the connection and the server URL.")
        return self.raw

    def polish(self, raw, mode, context, vocab, examples=()):
        from orfeuscribe.providers import ProviderError
        self.contexts.append(dict(context))
        self.calls.append({"raw": raw, "examples": tuple(examples), "context": dict(context), "vocab": vocab})
        if self.fail_polish:
            raise ProviderError("The model returned no text. Your transcript was kept.")
        if self.replies:
            answer = self.replies.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return raw[:1].upper() + raw[1:] + ("" if raw.rstrip().endswith((".", "?", "!")) else ".")

    def translate(self, raw, mode, context, vocab, language):
        self.calls.append({"translate": raw, "language": language})
        return self.replies.pop(0) if self.replies else f"[{language}] {raw}"


def make_app(test, provider=None, recorder=None, **config):
    """A OrfeuScribeApplication that never registers on D-Bus or touches real settings."""
    from orfeuscribe import service
    from orfeuscribe.config import Config
    from orfeuscribe.history import History
    from orfeuscribe.rescue import RescueStore
    from orfeuscribe.vocabulary import Vocabulary
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    root = Path(temp.name)
    app = service.OrfeuScribeApplication(background=True, config_dir=root, history_path=root / "history.db")
    app.config = Config(root)
    app.config.data.update(config)
    app.settings = FakeSettings()
    app.recorder = recorder or FakeRecorder()
    app.connection = FakeConnection()
    app.chime = FakeChime()
    app.history = History(root / "history.db")
    app.rescue = RescueStore(root / "rescue")
    app.vocab = Vocabulary()
    app.provider = provider or FakeProvider()
    app.last_record = None
    test.addCleanup(lambda: app.state in ("listening", "transcribing", "polishing") and app.cancel())
    return app


def pump(test, app, states, timeout=5.0):
    end = time.monotonic() + timeout
    while app.state not in states and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(.01)
    test.assertIn(app.state, states)
