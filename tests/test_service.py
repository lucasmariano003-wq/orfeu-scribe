"""Service behaviour with doubles: no D-Bus registration, no microphone, no real settings."""
import json
import subprocess
import time
import unittest
from unittest.mock import patch

from orfeuscribe.pipeline import Result, literal_local
from orfeuscribe.vocabulary import parse_vocabulary
from tests.support import GTK, FakeProvider, make_app, pump, wav

if GTK:
    from orfeuscribe import service
    from orfeuscribe.audio import MicWatch


def result(**kwargs):
    values = dict(raw="uh, I read zefirin, full stop. later", final="I read Zephyrine. Later.", mode="dictation",
                  provider="fake", model="m", recording=0, stt=0, polishing=0, total=0)
    values.update(kwargs)
    return Result(**values)


@unittest.skipUnless(GTK, "GTK 4, libadwaita and GStreamer are not available")
class RequestTests(unittest.TestCase):
    def test_plain_dictation_never_waits_for_atspi(self):
        app = make_app(self)
        app.request = {"operation": "dictation", "selection": "selected text", "pid": 42}
        app.request_at = time.monotonic()

        def slow(*args, **kwargs):
            time.sleep(1.5)
            raise subprocess.TimeoutExpired(args[0], 1.5)
        with patch("orfeuscribe.selection.subprocess.run", side_effect=slow), \
             patch("orfeuscribe.context.read", return_value=None):
            started = time.monotonic()
            app.start()
            self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual((app.state, app.recorder.started), ("listening", 1))
        self.assertIsNone(app.selection_snapshot)

    def test_question_confirms_the_selection(self):
        app = make_app(self)
        app.request = {"operation": "ask", "selection": "text", "pid": 42}
        app.request_at = time.monotonic()
        with patch("orfeuscribe.selection.snapshot", return_value={"pid": 42}) as snapshot:
            app.start()
        snapshot.assert_called_once_with(42, "text", "ask")
        self.assertEqual(app.recording_request["snapshot"], {"pid": 42})

    def test_orphan_request_expires_and_fresh_one_is_honoured(self):
        app = make_app(self)
        app.request = {"operation": "ask", "selection": "old", "pid": 42}
        app.request_at = time.monotonic() - service.REQUEST_TTL - 1
        app.start()
        self.assertEqual((app.recording_request["operation"], app.recording_request["selection"]), ("dictation", ""))
        app.cancel()
        app.request = {"operation": "translation", "selection": "", "pid": 0}
        app.request_at = time.monotonic()
        app.start()
        self.assertEqual(app.recording_request["operation"], "translation")

    def test_window_button_is_always_a_plain_dictation(self):
        app = make_app(self)
        app.request = {"operation": "ask", "selection": "left behind", "pid": 42}
        app.request_at = time.monotonic()
        app.dictate_from_window()
        self.assertEqual((app.state, app.recording_request["operation"]), ("listening", "dictation"))
        self.assertEqual(app.recording_context["wm_class"], service.APP_ID)

    def test_reply_is_off_by_default(self):
        app = make_app(self)
        app.request = {"operation": "reply", "selection": "", "pid": 0}
        app.request_at = time.monotonic()
        app.start()
        self.assertEqual(app.recording_request["operation"], "dictation")
        self.assertTrue(any("Reply with context is off" in value[0] for value in app.connection.values("MicWarning")))

    def test_terminal_and_own_window_do_not_read_the_field(self):
        app = make_app(self)
        for wm_class in ("org.gnome.Ptyxis", service.APP_ID):
            app.context = {"wm_class": wm_class, "title": "x"}
            app.request = {"operation": "dictation", "selection": "", "pid": 99}
            app.request_at = time.monotonic()
            with patch("orfeuscribe.context.read") as read:
                app.start()
                app.cancel()
                read.assert_not_called()

    def test_specialized_mode_needs_to_be_offered(self):
        app = make_app(self, rules=[{"pattern": "studio", "mode": "script"}])
        app.context = {"wm_class": "studio", "title": ""}
        app.start()
        self.assertEqual(app.mode, "dictation")
        app.cancel()
        app.config.data["specialized_modes"] = ["script"]
        app.start()
        self.assertEqual(app.mode, "script")


@unittest.skipUnless(GTK, "GTK 4, libadwaita and GStreamer are not available")
class DeliveryTests(unittest.TestCase):
    def test_revert_offer_precedes_paste_and_is_recorded_once(self):
        app = make_app(self)
        app.vocab = parse_vocabulary("zefirin => Zephyrine")
        app._complete(app.generation, result(), b"", {"wm_class": "gedit", "title": "Notes"}, True)
        names = app.connection.names()
        self.assertLess(names.index("Revertible"), names.index("Deliver"))
        offer = json.loads(app.connection.values("Revertible")[0][0])
        self.assertEqual(offer["raw"], literal_local(result().raw, "dictation", app.vocab))
        self.assertEqual(offer["raw"], "Uh, I read Zephyrine. Later")
        self.assertFalse(app.revert_to_raw(offer["id"] + 1))
        self.assertTrue(app.revert_to_raw(offer["id"]))
        self.assertFalse(app.revert_to_raw(offer["id"]))
        self.assertEqual(app.history.get(offer["id"])["action"], "raw")

    def test_revert_offer_expires(self):
        app = make_app(self)
        app._complete(app.generation, result(), b"", {"wm_class": "gedit", "title": "Notes"}, True)
        app.revertible["at"] = time.monotonic() - service.REVERT_TTL - 1
        self.assertFalse(app.revert_to_raw(app.revertible["id"]))

    def test_cards_and_own_window_do_not_paste(self):
        app = make_app(self)
        app._complete(app.generation, result(mode="question", action="card"), b"", {"wm_class": "gedit", "title": "x"}, True)
        app.dictate_from_window()
        app._complete(app.generation, result(), b"", app.recording_context, True)
        self.assertNotIn("Revertible", app.connection.names())
        self.assertNotIn("Deliver", app.connection.names())
        self.assertIn(("Notice", ("Saved to history",)), app.connection.signals)
        self.assertEqual(len(app.connection.values("Card")), 1)

    def test_cancelled_generation_cannot_deliver_or_write(self):
        app = make_app(self)
        app.start()
        generation = app.generation
        app.cancel()
        app._complete(generation, result(), b"", app.recording_context, True, True)
        self.assertEqual(app.history.search(), [])
        self.assertNotIn("Deliver", app.connection.names())
        self.assertIn("CancelDelivery", app.connection.names())

    def test_parts_keep_order_while_listening(self):
        app = make_app(self, pause_delivery=True)
        app.context = {"wm_class": "editor", "title": "target"}
        app.start()
        generation = app.generation
        for i in range(2):
            app._complete(generation, result(raw=str(i), final=f"Part {i}"), b"", app.recording_context, True, True)
            self.assertEqual(app.state, "listening")
        self.assertEqual([v[0] for v in app.connection.values("Deliver")], ["Part 0", "\n\nPart 1"])
        self.assertEqual(app.segment_tail, "Part 1")

    def test_silence_stops_without_changing_the_target(self):
        app = make_app(self, silence_stop=True, silence_seconds=2)
        app.start()
        with patch.object(app, "stop") as stop:
            with patch("orfeuscribe.service.time.monotonic", return_value=100):
                app.level(.2)
            with patch("orfeuscribe.service.time.monotonic", return_value=102):
                app.level(0)
            stop.assert_called_once()

    def test_note_goes_to_the_folder_when_configured(self):
        app = make_app(self)
        folder = app.config_dir / "notes"
        folder.mkdir()
        app.config.data.update(notes_folder=str(folder), note_subfolders=["Inbox"])
        app._complete(app.generation, result(mode="note", action="note", final="Buy a lamp."), b"",
                      {"wm_class": "gedit", "title": "x"}, True)
        files = list((folder / "Inbox").glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("Buy a lamp.", files[0].read_text())
        self.assertIn(("Notice", ("Added to your notes",)), app.connection.signals)

    def test_file_completion_writes_without_note_or_paste(self):
        app = make_app(self)
        app.file_progress = {"stage": "opening"}
        app._file_done(app.generation, result(action="file"), "talk.ogg")
        self.assertEqual(app.file_progress["stage"], "done")
        self.assertEqual(app.history.search()[0]["action"], "file")
        self.assertNotIn("Deliver", app.connection.names())


@unittest.skipUnless(GTK, "GTK 4, libadwaita and GStreamer are not available")
class RescueServiceTests(unittest.TestCase):
    def dictate(self, app):
        app.recording_context = {"wm_class": "gedit", "title": "Notes"}
        app.recording_request = {"operation": "dictation", "input_language": "en"}
        app.mode = "dictation"
        app.state = "transcribing"
        app._work(wav(2), 2.0, app.recording_context, "dictation", request=app.recording_request)

    def test_network_failure_keeps_audio_and_recover_saves_the_entry(self):
        app = make_app(self, FakeProvider(fail_stt=True))
        self.dictate(app)
        pump(self, app, ("error",))
        items = app.rescue.items()
        self.assertEqual([(i["reason"], i["context"]["wm_class"]) for i in items], [("error", "gedit")])
        names = app.connection.names()
        self.assertLess(names.index("Rescued"), names.index("Error"))
        self.assertEqual(app.history.search(), [])
        app.provider = FakeProvider()
        app.connection.signals.clear()
        self.assertTrue(app.recover(items[0]["id"]))
        pump(self, app, ("done",))
        self.assertEqual([row["final"] for row in app.history.search()], ["Let us meet on wednesday afternoon."])
        self.assertEqual(app.rescue.items(), [])
        self.assertNotIn("Deliver", app.connection.names(), "recovering never pastes: the focus has moved on")

    def test_revision_failure_links_audio_to_the_entry(self):
        app = make_app(self, FakeProvider(fail_polish=True))
        self.dictate(app)
        pump(self, app, ("error",))
        row = app.history.search()[0]
        self.assertTrue(row["error"])
        self.assertEqual(app.rescue.items()[0]["history_id"], row["id"])

    def test_cancelling_keeps_audio_without_an_offer(self):
        app = make_app(self)
        self.dictate(app)
        app.cancel()
        self.assertEqual([i["reason"] for i in app.rescue.items()], ["cancelled"])
        self.assertNotIn("Rescued", app.connection.names())

    def test_spoken_cancel_keeps_audio_and_recover_keeps_the_words(self):
        app = make_app(self, FakeProvider(raw="this is no good, cancel dictation"))
        self.dictate(app)
        pump(self, app, ("idle",))
        items = app.rescue.items()
        self.assertEqual([i["reason"] for i in items], ["voice-cancel"])
        self.assertTrue(app.recover(items[0]["id"]))
        pump(self, app, ("done",))
        self.assertIn("cancel dictation", app.history.search()[0]["final"].lower())


@unittest.skipUnless(GTK, "GTK 4, libadwaita and GStreamer are not available")
class SessionComfortTests(unittest.TestCase):
    def test_mic_warning_once_sounds_and_inhibit(self):
        app = make_app(self)
        inhibits = []
        app.inhibit = lambda window, flags, reason: inhibits.append(reason) or 7
        app.uninhibit = lambda cookie: inhibits.append(("released", cookie))
        app.start()
        app.mic_watch = MicWatch(time.monotonic() - 3)
        app.meter(-120.0, -120.0)
        app.meter(-120.0, -120.0)
        self.assertEqual(app.connection.values("MicWarning"), [(MicWatch.message("silent"),)])
        app._inhibit_now()
        self.assertEqual(inhibits, ["Dictation in progress"])
        app.set_state("done")
        self.assertEqual(inhibits[-1], ("released", 7))
        app.cancel()
        self.assertEqual(app.chime.played, ["start", "end"])
        quiet = make_app(self, sounds=False)
        quiet.start()
        quiet.cancel()
        self.assertEqual(quiet.chime.played, [])

    def test_live_text_is_opt_in_and_never_pasted(self):
        app = make_app(self)
        app.start()
        self.assertIsNone(app.live, "off by default")
        app.cancel()
        app.config.data["live_text"] = True
        app.start()
        self.assertIsNotNone(app.live)
        app._partial(app.generation, "word " * 40 + "end of sentence")
        partial = app.connection.values("Partial")
        self.assertTrue(partial[0][0].endswith("end of sentence"))
        self.assertNotIn("Deliver", app.connection.names())
        app.cancel()
        self.assertIsNone(app.recorder.on_pcm)


if __name__ == "__main__":
    unittest.main()
