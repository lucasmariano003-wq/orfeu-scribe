"""Microphone checks, sounds, silence, files, live text and cursor fitting — no real microphone is opened."""
import math
import struct
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from orfeuscribe.audio import MicWatch, SilenceGate
from orfeuscribe.context import fit_to_cursor
from orfeuscribe.live import LiveText, tail
from orfeuscribe.pipeline import Cancelled
from orfeuscribe.sounds import AMPLITUDE, GAP_SECONDS, NOTE_SECONDS, RATE, tone_pcm
from orfeuscribe.vocabulary import parse_vocabulary
from tests.support import GTK, FakeProvider

if GTK:
    from gi.repository import GLib, Gst
    from orfeuscribe.audio import Recorder
    from orfeuscribe.media import audio_chunks, transcribe_file
    from orfeuscribe.sounds import Chime

SECOND = b"\x10\x00" * 16000


def frames(watch, rms, peak, start, seconds):
    found, t = [], start
    while t < start + seconds:
        warning = watch.feed(rms, peak, t)
        if warning:
            found.append((round(t, 2), warning))
        t += .05
    return found


def wait(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.02)
    return False


class MicWatchTests(unittest.TestCase):
    def test_total_silence_in_the_first_two_seconds(self):
        found = frames(MicWatch(0.0), -120.0, -120.0, 0.0, 5.0)
        self.assertEqual([w for _, w in found], ["silent"])
        self.assertTrue(2.0 <= found[0][0] < 2.1)

    def test_normal_voice_and_room_noise_do_not_warn(self):
        watch = MicWatch(0.0)
        self.assertEqual(frames(watch, -62.0, -50.0, 0.0, 1.0) + frames(watch, -24.0, -8.0, 1.0, 9.0), [])
        self.assertNotIn("silent", [w for _, w in frames(MicWatch(0.0), -70.0, -60.0, 0.0, 3.0)])

    def test_clipping_once_and_quiet_voice(self):
        found = frames(MicWatch(0.0), -6.0, 0.0, 0.0, 3.0)
        self.assertEqual([w for _, w in found], ["loud"])
        watch = MicWatch(0.0)
        self.assertIsNone(watch.feed(-20.0, 0.0, .1))
        self.assertEqual(frames(watch, -22.0, -6.0, .15, 5.0), [])
        found = frames(MicWatch(0.0), -58.0, -45.0, 0.0, 8.0)
        self.assertEqual([w for _, w in found], ["low"])
        self.assertEqual(set(MicWatch.MESSAGES), {"silent", "loud", "low"})
        self.assertIn("microphone", MicWatch.message("silent"))


class SoundAndSilenceTests(unittest.TestCase):
    def test_two_soft_notes_without_clicks(self):
        for kind in ("start", "end"):
            samples = struct.unpack(f"<{len(tone_pcm(kind)) // 2}h", tone_pcm(kind))
            self.assertAlmostEqual(len(samples) / RATE, 2 * NOTE_SECONDS + GAP_SECONDS, places=2)
            self.assertEqual((samples[0], samples[-1]), (0, 0))
            self.assertLessEqual(max(abs(x) for x in samples), AMPLITUDE * 32767 + 1)

    def test_silence_needs_voice_first_and_parts_need_twenty_seconds(self):
        gate = SilenceGate(auto_stop=True, now=0)
        self.assertIsNone(gate.feed(0, 100))
        gate.feed(.2, 101)
        self.assertIsNone(gate.feed(0, 103.9))
        self.assertEqual(gate.feed(0, 104), "stop")
        gate = SilenceGate(parts=True, now=0)
        gate.feed(.3, 1)
        self.assertIsNone(gate.feed(0, 4))
        self.assertEqual(gate.feed(0, 20), "part")
        gate = SilenceGate()
        gate.feed(.4, 1)
        self.assertIsNone(gate.feed(0, 500))


class FitTests(unittest.TestCase):
    def test_english(self):
        self.assertEqual(fit_to_cursor("And then we went home.", "We went to the park", "", language="en"),
                         " and then we went home.")
        self.assertEqual(fit_to_cursor("Also the parents.", "I invited the kids ", "and the teachers", language="en"),
                         "also the parents ")
        self.assertEqual(fit_to_cursor("I think so.", "Well", "", language="en"), " I think so.")
        self.assertEqual(fit_to_cursor("Peter arrived early.", "yesterday", "", language="en"), " Peter arrived early.")

    def test_portuguese_and_unknown_languages(self):
        self.assertEqual(fit_to_cursor("E depois voltamos.", "Fomos ao parque", "", language="pt"), " e depois voltamos.")
        self.assertEqual(fit_to_cursor("Zephyrine escreveu.", "como", "", ("Zephyrine",), "pt"), " Zephyrine escreveu.")
        self.assertEqual(fit_to_cursor("Haus und Garten.", "Wir sehen", "", language="de"), " Haus und Garten.")

    def test_sentence_start_openers_and_no_atspi(self):
        self.assertEqual(fit_to_cursor("we start at eight.", "Done. ", ""), "We start at eight.")
        self.assertEqual(fit_to_cursor("we start.", "Line one\n", ""), "We start.")
        self.assertEqual(fit_to_cursor("That is", "said (", "", language="en"), "that is")
        self.assertEqual(fit_to_cursor(", and more", "word", ""), ", and more")
        self.assertEqual(fit_to_cursor("And then.", None, None), "And then.")


class LiveTests(unittest.TestCase):
    def test_tail_starts_at_a_word(self):
        self.assertEqual(tail("short"), "short")
        text = "love is patient, love is kind, it does not envy, it does not boast, it is not proud"
        result = tail(text, 30)
        self.assertTrue(result.startswith("…") and text.endswith(result[1:]) and not result[1:].startswith(" "))

    def test_periodic_parts_without_overlapping_calls(self):
        class Provider:
            def __init__(self):
                self.active = self.most = 0
                self.sizes = []

            def transcribe(self, audio, lang, keyterms):
                self.active += 1
                self.most = max(self.most, self.active)
                self.sizes.append(len(audio) - 44)
                time.sleep(.25)
                self.active -= 1
                return f"part{len(self.sizes)}"

        provider, texts = Provider(), []
        live = LiveText(provider, "en", (), texts.append, after=1.0, period=.1)
        self.addCleanup(live.close)
        self.assertEqual(live.mode, "periodic", "no open_stream: periodic parts")
        feeder = threading.Thread(target=lambda: [live.feed(SECOND[:3200]) or time.sleep(.01) for _ in range(40)])
        feeder.start()
        feeder.join()
        self.assertTrue(wait(lambda: len(live.pending) < 32000 and len(provider.sizes) >= 2 and not provider.active, 6))
        live.close()
        self.assertEqual(provider.most, 1)
        self.assertEqual(sum(provider.sizes) + len(live.pending), 40 * 3200)
        self.assertTrue(texts[-1].startswith("part1 part2"))

    def test_below_threshold_nothing_is_sent(self):
        provider = FakeProvider()
        live = LiveText(provider, "en", (), lambda text: None, after=15.0)
        live.feed(SECOND)
        time.sleep(.3)
        live.close()
        self.assertEqual(provider.languages, [])


@unittest.skipUnless(GTK, "GTK 4, libadwaita and GStreamer are not available")
class GStreamerTests(unittest.TestCase):
    def setUp(self):
        missing = [name for name in ("audiotestsrc", "level", "appsink", "audioconvert", "audioresample", "uridecodebin")
                   if not Gst.ElementFactory.find(name)]
        if missing:
            self.skipTest("missing GStreamer elements: " + ", ".join(missing))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_real_level_messages_for_muted_and_clipping_sources(self):
        def meter_from(description):
            measures = []
            recorder = Recorder(lambda value: None, lambda message: None, lambda rms, peak: measures.append((rms, peak)))
            pipeline = Gst.parse_launch(f"{description} ! audioconvert ! level interval=50000000 ! fakesink sync=false")
            bus = pipeline.get_bus()
            pipeline.set_state(Gst.State.PLAYING)
            while True:
                message = bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.ELEMENT | Gst.MessageType.EOS |
                                                 Gst.MessageType.ERROR)
                if message is None or message.type != Gst.MessageType.ELEMENT:
                    break
                recorder._message(bus, message)
            pipeline.set_state(Gst.State.NULL)
            return measures
        mute = meter_from("audiotestsrc volume=0 num-buffers=60 samplesperbuffer=800 ! audio/x-raw,rate=16000,channels=1")
        self.assertTrue(mute and all(rms <= -120 and math.isfinite(rms) for rms, _ in mute))
        watch = MicWatch(0.0)
        self.assertIn("silent", [watch.feed(rms, peak, i * .05) for i, (rms, peak) in enumerate(mute)])
        loud = meter_from("audiotestsrc wave=square volume=1.0 num-buffers=20 samplesperbuffer=800 ! "
                          "audio/x-raw,rate=16000,channels=1")
        watch = MicWatch(0.0)
        self.assertIn("loud", [watch.feed(rms, peak, i * .05) for i, (rms, peak) in enumerate(loud)])

    def test_chime_plays_into_a_fake_sink_and_releases(self):
        chime = Chime(sink="fakesink sync=true")
        self.assertTrue(chime.play("start"))
        end = time.monotonic() + 3
        while chime.playing and time.monotonic() < end:
            GLib.MainContext.default().iteration(False)
            time.sleep(.01)
        self.assertEqual(chime.playing, set())

    def test_file_chunks_exact_and_cancel(self):
        path = self.root / "audio.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            handle.writeframes(b"\0\0" * 72000)
        chunks = list(audio_chunks(path, chunk_seconds=2))
        self.assertEqual([r[1] for r in chunks], [0, 2, 4])
        self.assertEqual([r[2] for r in chunks], [2, 2, .5])
        with self.assertRaises(Cancelled):
            list(audio_chunks(path, lambda: True))
        with self.assertRaises(ValueError):
            list(audio_chunks(self.root / "notes.txt"))

    def test_file_parts_have_headings_and_no_destinations(self):
        provider = FakeProvider(raw="note in Ideas: a sentence")

        def generator(*_args):
            yield from [(b"wav", 0, 2, 4), (b"wav", 2, 2, 4)]
        with patch("orfeuscribe.media.audio_chunks", side_effect=generator):
            result = transcribe_file(provider, "a.wav", "dictation", parse_vocabulary(""))
        self.assertEqual(result.action, "file")
        self.assertIn("00:00:02 — Part 2", result.final)
        self.assertIn("Note in Ideas", result.final)
        self.assertEqual(provider.contexts[1]["previous_tail"], "Note in Ideas: a sentence.")


if __name__ == "__main__":
    unittest.main()
