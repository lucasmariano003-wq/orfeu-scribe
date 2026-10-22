"""Live text: provisional text appears while the user speaks; nothing is pasted.

Off by default, because it multiplies calls to the speech service. After ~15 s
of speech, new audio is transcribed every ~6 s (one call at a time) and the end
of the provisional text appears in the pill. The final revision is unchanged:
live text only shows that the speech is being heard. A provider may also offer
``open_stream`` for true streaming; the OpenAI-compatible adapter does not.
"""
from __future__ import annotations

import json
import threading
import time

LIVE_AFTER = 15.0      # seconds of speech before starting (short dictations cost nothing extra)
PERIOD = 6.0           # interval between partial transcriptions
FRAME = 3200           # 100 ms of 16 kHz mono 16-bit PCM
BYTES_PER_SECOND = 32000
CATCH_UP = 4.0         # backlog is sent at up to 4x real time
KEEP_TEXT = 4000
RECONNECTS = 3


def tail(text, size=64):
    """The end of the text, starting at a whole word."""
    text = " ".join(text.split())
    if len(text) <= size:
        return text
    cut = text[-size:]
    space = cut.find(" ")
    return "…" + (cut[space + 1:] if 0 <= space < size // 3 else cut)


class LiveText:
    def __init__(self, provider, language, keyterms, on_text, *, after=LIVE_AFTER,
                 period=PERIOD, clock=time.monotonic):
        self.provider = provider
        self.language = language
        self.keyterms = tuple(keyterms)
        self.on_text = on_text
        self.after = after
        self.period = period
        self.clock = clock
        self.mode = "stream" if callable(getattr(provider, "open_stream", None)) else "periodic"
        self.lock = threading.Condition()
        self.pending = bytearray()   # PCM not yet sent or transcribed
        self.received = 0            # PCM bytes received in total
        self.finals = []
        self.current = ""
        self.closed = False
        self.calls = 0
        self.failures = 0
        self.ws = None
        self.thread = threading.Thread(target=self._run, name="orfeuscribe-live", daemon=True)
        self.thread.start()

    # --- input (GStreamer thread) ------------------------------------------
    def feed(self, pcm):
        with self.lock:
            if self.closed:
                return
            self.pending.extend(pcm)
            self.received += len(pcm)
            # Without network for a long time, keep only the last minute for live text.
            if len(self.pending) > 60 * BYTES_PER_SECOND and self.mode == "stream":
                del self.pending[:len(self.pending) - 60 * BYTES_PER_SECOND]
            self.lock.notify_all()

    def close(self):
        with self.lock:
            self.closed = True
            self.lock.notify_all()
        ws, self.ws = self.ws, None
        if ws:
            ws.close()

    @property
    def text(self):
        return " ".join(part for part in (*self.finals, self.current) if part).strip()

    def _publish(self):
        text = self.text
        if len(text) > KEEP_TEXT:
            self.finals = [text[-KEEP_TEXT:]]
            self.current = ""
            text = self.finals[0]
        if text and not self.closed:
            self.on_text(text)

    # --- work --------------------------------------------------------------
    def _wait_started(self):
        with self.lock:
            while not self.closed and self.received < self.after * BYTES_PER_SECOND:
                self.lock.wait(0.25)
            return not self.closed

    def _run(self):
        if not self._wait_started():
            return
        if self.mode == "stream":
            self._stream()
        if self.mode == "periodic":
            self._periodic()

    def _take(self, size=None):
        with self.lock:
            size = len(self.pending) if size is None else min(size, len(self.pending))
            data = bytes(self.pending[:size])
            del self.pending[:size]
            return data

    def _stream(self):
        """Streaming protocol of the provider's ``open_stream`` (transcript.* events)."""
        from .providers import ProviderError
        attempts = 0
        while not self.closed:
            try:
                self.ws = self.provider.open_stream(self.language, self.keyterms)
                first = json.loads(self.ws.receive(timeout=10) or "{}")
                if first.get("type") != "transcript.created":
                    raise ProviderError("stream did not start")
            except (ProviderError, OSError, ValueError, AttributeError) as exc:
                if self.ws:
                    self.ws.close()
                    self.ws = None
                # A definitive refusal: do not insist, use periodic parts.
                if getattr(exc, "status", None) in (400, 401, 403, 404, 405):
                    self.mode = "periodic"
                    return
                attempts += 1
                if attempts >= RECONNECTS or self.closed:
                    self.mode = "periodic"
                    return
                time.sleep(min(4, attempts))
                continue
            reader = threading.Thread(target=self._read, args=(self.ws,), daemon=True)
            reader.start()
            opened = time.monotonic()
            ok = self._send_loop(self.ws)
            if self.closed:
                return
            if not ok:
                # A session that lasted means a good network: the next drop restarts the count.
                attempts = 0 if time.monotonic() - opened > 30 else attempts
                attempts += 1
                if self.current:
                    self.finals.append(self.current)
                    self.current = ""
                if attempts >= RECONNECTS:
                    self.mode = "periodic"
                    return

    def _send_loop(self, ws):
        """Sends the backlog quickly (up to 4x) and then in real time."""
        while not self.closed:
            with self.lock:
                while not self.closed and len(self.pending) < FRAME:
                    self.lock.wait(0.25)
                backlog = len(self.pending) / BYTES_PER_SECOND
            if self.closed:
                return True
            frame = self._take(FRAME)
            try:
                ws.send_binary(frame)
            except Exception:  # noqa: BLE001 — network dropped: reconnect or go periodic
                return False
            if backlog > 0.3:
                time.sleep(0.1 / CATCH_UP)
            if getattr(ws, "closed", False):
                return False
        return True

    def _read(self, ws):
        while not self.closed:
            try:
                message = ws.receive(timeout=120)
            except Exception:  # noqa: BLE001
                ws.close()
                return
            if message is None:
                return
            try:
                event = json.loads(message)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "transcript.partial" and isinstance(event.get("text"), str):
                self.current = event["text"].strip()
                if event.get("speech_final"):
                    self.finals.append(self.current)
                    self.current = ""
                self._publish()
            elif kind in ("transcript.done", "error"):
                ws.close()
                return

    def _periodic(self):
        """New parts only, never overlapping calls: just the audio since the last cut."""
        from .audio import wav_from_pcm
        from .providers import ProviderError
        next_call = self.clock()
        while not self.closed:
            with self.lock:
                while not self.closed and (self.clock() < next_call or len(self.pending) < BYTES_PER_SECOND):
                    self.lock.wait(0.25)
            if self.closed:
                return
            piece = self._take()
            started = self.clock()
            self.calls += 1
            try:
                text = self.provider.transcribe(wav_from_pcm(piece), self.language, self.keyterms)
                if text.strip():
                    self.finals.append(text.strip())
                    self._publish()
            except ProviderError:
                self.failures += 1
            next_call = max(started + self.period, self.clock())
