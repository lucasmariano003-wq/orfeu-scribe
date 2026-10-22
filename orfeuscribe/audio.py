"""GStreamer recording: PCM in memory, mono, 16 kHz; no temporary files."""
from __future__ import annotations

import io
import math
import threading
import wave
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .i18n import N_, _

Gst.init(None)
MAX_BYTES = 16000 * 2 * 600


def microphones():
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Audio/Source", None)
    if not monitor.start():
        return []
    try:
        return [(device.get_display_name(), device) for device in monitor.get_devices()]
    finally:
        monitor.stop()


class Recorder:
    def __init__(self, level, error, meter=None):
        self.on_level = level
        self.on_error = error
        # (rms dBFS, peak dBFS) every 50 ms, for the microphone check.
        self.on_meter = meter
        # Raw PCM for live text (GStreamer thread).
        self.on_pcm = None
        # Only in an isolated proof session: a synthetic source instead of the microphone.
        self.test_source = None
        self.pipeline = None
        self.bus = None
        self.bus_id = None
        self.samples = bytearray()
        self.lock = threading.Lock()

    def start(self, microphone=""):
        self.samples = bytearray()
        self.pipeline = Gst.Pipeline.new("orfeuscribe-recording")
        source = None
        if self.test_source:
            source = Gst.parse_bin_from_description(self.test_source, True)
        elif microphone:
            for name, device in microphones():
                if name == microphone:
                    source = device.create_element("microphone")
                    break
            if source is None:
                raise RuntimeError(_("The chosen microphone is not available. Pick another one in Settings."))
        if source is None:
            source = Gst.ElementFactory.make("autoaudiosrc", "microphone")
        convert = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        caps = Gst.ElementFactory.make("capsfilter", None)
        caps.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=16000,channels=1,layout=interleaved"))
        meter = Gst.ElementFactory.make("level", None)
        meter.set_property("interval", 50 * Gst.MSECOND)
        sink = Gst.ElementFactory.make("appsink", "memory")
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.connect("new-sample", self._sample)
        elements = [source, convert, resample, caps, meter, sink]
        if any(element is None for element in elements):
            raise RuntimeError(_("A GStreamer recording component is missing."))
        for element in elements:
            self.pipeline.add(element)
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise RuntimeError(_("Could not connect the microphone to the recorder."))
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus_id = self.bus.connect("message", self._message)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.cancel()
            raise RuntimeError(_("Could not start the microphone."))

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        chunk = buffer.extract_dup(0, buffer.get_size())
        with self.lock:
            if len(self.samples) + len(chunk) > MAX_BYTES:
                return Gst.FlowReturn.EOS
            self.samples.extend(chunk)
        callback = self.on_pcm
        if callback:
            callback(chunk)
        return Gst.FlowReturn.OK

    def _message(self, bus, message):
        if message.type == Gst.MessageType.ERROR:
            self.on_error(_("Could not read the microphone. Check the audio input in Settings."))
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure and structure.get_name() == "level":
                rms = structure.get_value("rms")
                peak = structure.get_value("peak")
                value = float(rms[0]) if rms else -120
                if not math.isfinite(value):
                    value = -120.0
                self.on_level(min(1.0, math.pow(10, value / 20) * 3))
                if self.on_meter:
                    top = float(peak[0]) if peak else -120.0
                    self.on_meter(value, top if math.isfinite(top) else -120.0)

    def take_chunk(self):
        with self.lock:
            pcm = bytes(self.samples)
            self.samples.clear()
        return wav_from_pcm(pcm)

    def stop(self):
        if self.pipeline is None:
            return b""
        self.pipeline.send_event(Gst.Event.new_eos())
        self.bus.timed_pop_filtered(Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        self._close()
        with self.lock:
            pcm = bytes(self.samples)
            self.samples.clear()
        return wav_from_pcm(pcm)

    def _close(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self.bus:
            if self.bus_id:
                self.bus.disconnect(self.bus_id)
            self.bus.remove_signal_watch()
        self.pipeline = self.bus = self.bus_id = None

    def cancel(self):
        self._close()
        with self.lock:
            self.samples.clear()


def wav_from_pcm(pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(pcm)
    return output.getvalue()


class SilenceGate:
    """Pauses only after voice; brief noise does not restart the clock forever."""
    def __init__(self, seconds=3.0, auto_stop=False, parts=False, now=0.0, minimum_part=20.0):
        self.seconds, self.auto_stop, self.parts = seconds, auto_stop, parts
        self.minimum_part = minimum_part
        self.started = now
        self.last_voice = now
        self.heard_voice = False

    def feed(self, value, now):
        if value >= 0.035:
            self.last_voice = now
            self.heard_voice = True
            return None
        if not self.heard_voice or now - self.last_voice < self.seconds:
            return None
        if self.parts:
            if now - self.started < self.minimum_part:
                return None
            self.heard_voice = False
            self.started = now
            return 'part'
        if self.auto_stop:
            self.heard_voice = False
            return 'stop'
        return None


class MicWatch:
    """Microphone check: total silence at the start, clipping, or a voice too quiet.

    Each warning appears once per dictation. Thresholds are dBFS from GStreamer's
    level element (one reading per 50 ms): a muted or unplugged microphone stays
    below -85 dB; speech into a microphone sits between -35 and -12 dB; a peak at
    -0.5 dB is clipping.
    """
    SILENT_DB = -85.0
    SILENT_AFTER = 2.0
    CLIP_DB = -0.5
    CLIP_FRAMES = 4
    LOW_DB = -50.0
    LOW_AFTER = 6.0
    MESSAGES = {"silent": N_("The microphone is not picking anything up. Check the input in Settings."),
                "loud": N_("The audio is clipping: move a little away from the microphone."),
                "low": N_("The audio is very quiet: move closer to the microphone.")}

    def __init__(self, now=0.0):
        self.started = now
        self.loudest = -200.0
        self.clips = 0
        self.warned = set()

    @classmethod
    def message(cls, warning):
        return _(cls.MESSAGES[warning])

    def feed(self, rms_db, peak_db, now):
        self.loudest = max(self.loudest, rms_db)
        elapsed = now - self.started
        if peak_db >= self.CLIP_DB:
            self.clips += 1
        if self.clips >= self.CLIP_FRAMES and "loud" not in self.warned:
            self.warned.add("loud")
            return "loud"
        if elapsed >= self.SILENT_AFTER and "silent" not in self.warned and self.loudest < self.SILENT_DB:
            self.warned.add("silent")
            return "silent"
        if (elapsed >= self.LOW_AFTER and not self.warned & {"low", "silent"}
                and self.SILENT_DB <= self.loudest < self.LOW_DB):
            self.warned.add("low")
            return "low"
        return None
