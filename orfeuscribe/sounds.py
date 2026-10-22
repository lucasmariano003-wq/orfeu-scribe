"""Discreet start and end sounds, generated here (no package or theme needed)."""
from __future__ import annotations

import math
import struct

RATE = 44100
NOTES = {"start": (660.0, 880.0), "end": (880.0, 587.33)}
NOTE_SECONDS = 0.07
GAP_SECONDS = 0.012
AMPLITUDE = 0.12  # low: a cue for the ear, not an alarm


def tone_pcm(kind, rate=RATE):
    """Two short notes (rising at the start, falling at the end) with a soft envelope."""
    pcm = bytearray()
    length = int(NOTE_SECONDS * rate)
    for index, frequency in enumerate(NOTES[kind]):
        for n in range(length):
            # Raised-cosine envelope: starts and ends at zero, without clicks.
            envelope = 0.5 - 0.5 * math.cos(2 * math.pi * n / (length - 1))
            value = AMPLITUDE * envelope * math.sin(2 * math.pi * frequency * n / rate)
            pcm += struct.pack("<h", int(value * 32767))
        if index == 0:
            pcm += b"\x00\x00" * int(GAP_SECONDS * rate)
    return bytes(pcm)


class Chime:
    def __init__(self, sink="autoaudiosink"):
        self.sink = sink
        self.playing = set()

    def play(self, kind):
        from .audio import Gst
        pipeline = Gst.parse_launch(f"appsrc name=chime format=time ! audioconvert ! audioresample ! {self.sink}")
        source = pipeline.get_by_name("chime")
        source.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,format=S16LE,rate={RATE},channels=1,layout=interleaved"))
        data = tone_pcm(kind)
        buffer = Gst.Buffer.new_allocate(None, len(data), None)
        buffer.fill(0, data)
        buffer.pts = 0
        buffer.duration = int(len(data) / 2 / RATE * Gst.SECOND)
        bus = pipeline.get_bus()
        bus.add_signal_watch()

        def finished(_bus, message):
            if message.type in (Gst.MessageType.EOS, Gst.MessageType.ERROR):
                bus.remove_signal_watch()
                pipeline.set_state(Gst.State.NULL)
                self.playing.discard(pipeline)
        bus.connect("message", finished)
        self.playing.add(pipeline)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            bus.remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
            self.playing.discard(pipeline)
            return False
        source.emit("push-buffer", buffer)
        source.emit("end-of-stream")
        return True
