"""Recording → OpenAI-compatible service (fake, local) → delivery, all isolated.

The recording is a real GStreamer pipeline fed by a synthetic tone instead of
the microphone. The service is tests/fake_openai.py on 127.0.0.1. Delivery is
the D-Bus "Deliver" signal captured by a fake connection: no extension, no
paste and no real session are involved. This proves the wiring, not
compatibility with any real provider.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from orfeuscribe import providers
from orfeuscribe.credentials import MemoryStore
from tests.fake_openai import serve
from tests.support import GTK, make_app, pump, wav

if GTK:
    from gi.repository import GLib, Gst
    from orfeuscribe.audio import Recorder

KEY = "sk-e2e-synthetic-key-0000"
SPOKEN = "let us meet on wednesday afternoon, new paragraph, thank you"
DELIVERED = "Let us meet on wednesday afternoon\n\nThank you."
ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(GTK, "GTK 4, libadwaita and GStreamer are not available")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        if not Gst.ElementFactory.find("audiotestsrc") or not Gst.ElementFactory.find("level"):
            self.skipTest("audiotestsrc or level is missing")
        self.server = serve()
        self.addCleanup(self.server.close)
        self.server.state.text = SPOKEN

    def configured_app(self, revision=True):
        app = make_app(self, provider=None, input_language="en")
        app.config.update(stt_base_url=self.server.url, stt_model="fake-transcribe",
                          revision_model="fake-chat" if revision else "", understand_intent=False)
        app.credentials = MemoryStore({"stt": KEY})
        app.provider = providers.build(app.config, app.credentials)
        recorder = Recorder(app.level, app.fail, app.meter)
        recorder.test_source = ("audiotestsrc wave=sine freq=330 volume=0.4 num-buffers=30 samplesperbuffer=1600 "
                                "! capsfilter caps=audio/x-raw,rate=16000,channels=1")
        app.recorder = recorder
        app.context = {"wm_class": "org.gnome.TextEditor", "title": "Notes"}
        return app

    def record_and_finish(self, app):
        app.start()
        self.assertEqual(app.state, "listening")
        for _ in range(60):
            GLib.MainContext.default().iteration(False)
            if len(app.recorder.samples) >= 32000 * 2:
                break
            GLib.usleep(20000)
        app.recording_start -= 3  # the synthetic source is faster than real time
        app.stop()
        pump(self, app, ("done", "error"), timeout=10)

    def test_recording_to_fake_service_to_isolated_delivery(self):
        app = self.configured_app()
        self.record_and_finish(app)
        self.assertEqual(app.state, "done", app.last_error)
        self.assertEqual(app.connection.values("Deliver"), [(DELIVERED,)])
        names = app.connection.names()
        self.assertLess(names.index("Revertible"), names.index("Deliver"))
        states = [value[0] for value in app.connection.values("StateChanged")]
        self.assertEqual(states[:2], ["listening", "transcribing"])
        self.assertIn("polishing", states)
        upload = dict(self.server.state.transcriptions[0])
        self.assertTrue(upload["file"]["riff"])
        self.assertGreater(upload["file"]["bytes"], 32000, "real PCM from the GStreamer pipeline")
        self.assertEqual(upload["model"], "fake-transcribe")
        self.assertEqual(self.server.state.authorizations[0], "Bearer " + KEY)
        chat = self.server.state.chats[0]
        self.assertEqual(json.loads(chat["messages"][1]["content"])["dictation"],
                         "let us meet on wednesday afternoon⟦VD_PARAGRAPH⟧thank you")
        row = app.history.search()[0]
        self.assertEqual((row["raw"], row["final"], row["provider"], row["model"]),
                         (SPOKEN, DELIVERED, "127.0.0.1", "fake-chat"))
        with closing(sqlite3.connect(app.history.path)) as db:
            dump = json.dumps([list(r) for r in db.execute("SELECT * FROM dictations")])
        self.assertNotIn(KEY, dump)
        self.assertFalse(list(app.config_dir.rglob("*.wav")), "audio is not kept by default")

    def test_transcription_only_uses_local_cleanup(self):
        app = self.configured_app(revision=False)
        self.record_and_finish(app)
        self.assertEqual(app.connection.values("Deliver"), [("Let us meet on wednesday afternoon\n\nThank you",)])
        self.assertEqual(self.server.state.chats, [], "no revision request was made")
        self.assertIn("local cleanup", app.history.search()[0]["model"])

    def test_service_failure_keeps_the_audio_for_recovery(self):
        app = self.configured_app()
        self.server.state.fail["transcriptions"] = [503]
        self.record_and_finish(app)
        self.assertEqual(app.state, "error")
        self.assertIn("HTTP 503", app.last_error)
        self.assertEqual(len(app.rescue.items()), 1)
        app.state = "idle"
        self.assertTrue(app.recover(app.rescue.items()[0]["id"]))
        pump(self, app, ("done",), timeout=10)
        self.assertEqual(app.history.search()[0]["final"], DELIVERED)

    def test_unconfigured_app_sends_nothing(self):
        app = make_app(self, provider=None)
        app.credentials = MemoryStore()
        app.provider = providers.build(app.config, app.credentials)
        app.recorder.audio = wav(2)
        app.start()
        self.assertEqual(app.state, "error")
        self.assertEqual(app.recorder.started, 0, "the microphone was never opened")
        self.assertIn("No speech service is configured", app.connection.values("Error")[0][0])
        self.assertEqual(self.server.state.transcriptions, [])
        self.assertEqual(app.rescue.items(), [])


class CommandLineTests(unittest.TestCase):
    def test_test_audio_command_with_fake_service(self):
        server = serve()
        self.addCleanup(server.close)
        server.state.text = SPOKEN
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            (base / "config/orfeuscribe").mkdir(parents=True)
            (base / "config/orfeuscribe/settings.json").write_text(json.dumps({
                "stt_base_url": server.url, "stt_model": "fake-transcribe", "revision_model": "fake-chat",
                "input_language": "en", "understand_intent": False}))
            audio = base / "sample.wav"
            audio.write_bytes(wav(2, 900))
            env = dict(os.environ, XDG_CONFIG_HOME=str(base / "config"), XDG_DATA_HOME=str(base / "data"),
                       ORFEUSCRIBE_STT_API_KEY=KEY, ORFEUSCRIBE_SECRET_BACKEND="memory")
            run = subprocess.run([sys.executable, "-m", "orfeuscribe", "--test-audio", str(audio), "--json", "--save",
                                  "--history", str(base / "cli.db")], cwd=ROOT, env=env, capture_output=True, text=True,
                                 timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            output = json.loads(run.stdout)
            self.assertEqual(output["final"], DELIVERED)
            self.assertNotIn(KEY, run.stdout + run.stderr)
            self.assertEqual(server.state.authorizations[0], "Bearer " + KEY)
            self.assertFalse((base / "data").exists(), "a separate history was used")
            self.assertTrue((base / "cli.db").exists())


if __name__ == "__main__":
    unittest.main()
