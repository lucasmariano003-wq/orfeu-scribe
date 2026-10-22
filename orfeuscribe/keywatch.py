"""Read-only key watcher (optional, off by default). Standard library only; no telemetry or input log.

Its only output is a closed vocabulary of gestures and states, sent through the
supervisor's private socket. It reads one input_event at a time; the transport
fragment is cleared once decoded. There is no text, history, list of ordinary
keys or debug output. It needs read access to /dev/input/event* (usually
membership in the ``input`` group), which is why it is opt-in.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import resource
import select
import socket
import struct
import sys
import time
from .gestures import GestureGate, MODIFIERS

EVENT = struct.Struct("@llHHi")
LETTERS = (30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50,
           49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44)
GESTURES = frozenset(action + ":" + name for action in ("start", "stop", "cancel", "toggle")
                     for name in ("dictation", "ask", "translation", "paste"))
STATES = frozenset(("ready", "waiting", "unavailable"))


def bitmap(value):
    words = value.split()
    return sum(int(word, 16) << (i * struct.calcsize("L") * 8)
               for i, word in enumerate(reversed(words)))


def discover(text, root="/dev/input"):
    devices = {}
    for block in text.split("\n\n"):
        fields = dict(re.findall(r"^B: (\w+)=(.*)$", block, re.M))
        keys = bitmap(fields.get("KEY", "0"))
        if not bitmap(fields.get("EV", "0")) & 2:
            continue
        keyboard = all(keys & (1 << code) for code in LETTERS)
        # Mouse/touchpad/touchscreen: needed to invalidate a gesture with a click.
        pointer = any(keys & (1 << code) for code in (272, 273, 274, 325, 330))
        if keyboard or pointer:
            handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
            for name in handlers[1].split() if handlers else ():
                if re.fullmatch(r"event\d+", name):
                    devices[str(Path(root) / name)] = keyboard
    return devices


class Stream:
    def __init__(self, fd, keyboard):
        self.fd, self.keyboard = fd, keyboard
        self.fragment = bytearray()
        self.dropped = False

    def read(self):
        chunk = os.read(self.fd, EVENT.size - len(self.fragment))
        if not chunk:
            raise EOFError
        self.fragment.extend(chunk)
        if len(self.fragment) < EVENT.size:
            return None
        _, _, kind, code, value = EVENT.unpack(self.fragment)
        self.fragment.clear()
        return kind, code, value


def held(fd):
    # EVIOCGKEY, read-only. The bitmap is not kept after this call.
    bits = bytearray(96)
    fcntl.ioctl(fd, 0x80000000 | (len(bits) << 16) | (ord("E") << 8) | 0x18, bits, True)
    modifiers = [int(bool(bits[code // 8] & (1 << (code % 8)))) for code in MODIFIERS]
    count = sum(byte.bit_count() for byte in bits) - sum(modifiers)
    bits.clear()
    return modifiers, count


class Watcher:
    def __init__(self, emit, mode="toggle", inventory=None, opener=None, snapshot=held):
        self.emit = emit
        self.gate = GestureGate(emit, mode)
        self.inventory = inventory or (lambda: discover(Path("/proc/bus/input/devices").read_text()))
        self.opener = opener or (lambda path: os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC))
        self.snapshot = snapshot
        self.streams = {}
        self.identities = {}
        self.status = None
        self.blocked = False

    def resync(self):
        self.gate.reset()
        self.blocked = True
        if any(stream.dropped for stream in self.streams.values()):
            return
        try:
            for stream in self.streams.values():
                modifiers, others = self.snapshot(stream.fd)
                if stream.keyboard:
                    self.gate.machine.modifiers = [a + b for a, b in zip(self.gate.machine.modifiers, modifiers)]
                    self.gate.machine.others += others
            self.gate.machine.dirty = self.gate.machine.down
        except OSError:
            return
        self.blocked = False

    def scan(self):
        try:
            wanted = self.inventory()
        except OSError:
            wanted = {}
        changed = False
        failed = False
        for path in list(self.streams):
            try:
                stat = os.stat(path)
                identity = (stat.st_ino, stat.st_rdev)
            except OSError:
                identity = None
            if path not in wanted or identity != self.identities[path]:
                os.close(self.streams.pop(path).fd)
                self.identities.pop(path)
                changed = True
        for path, keyboard in wanted.items():
            if path in self.streams:
                continue
            try:
                fd = self.opener(path)
                stat = os.fstat(fd)
            except OSError:
                failed = True
                continue
            self.streams[path] = Stream(fd, keyboard)
            self.identities[path] = (stat.st_ino, stat.st_rdev)
            changed = True
        if changed or self.blocked:
            self.resync()
        # Failing to read even a mouse means gestures without clicks cannot be promised.
        status = "unavailable" if failed or self.blocked else "ready" if any(s.keyboard for s in self.streams.values()) else "waiting"
        if status != self.status:
            if status != "ready":
                self.gate.reset()
            self.status = status
            self.emit(status)

    def consume(self, stream, now):
        event = stream.read()
        if event is None:
            return
        kind, code, value = event
        if kind == 0 and code == 3:  # SYN_DROPPED
            stream.dropped = True
            self.gate.reset()
            self.blocked = True
        elif stream.dropped:
            if kind == 0 and code == 0:
                stream.dropped = False
                self.resync()
        elif not self.blocked and self.status == "ready":
            if kind == 1 and code >= 256 and value:
                self.gate.pointer(now)
            elif kind == 1 and stream.keyboard:
                self.gate.feed(code, value, now)
            elif kind == 2 and code in (6, 8, 11, 12) and value:
                self.gate.pointer(now)  # scrolling; plain movement does not cancel

    def close(self):
        for stream in self.streams.values():
            os.close(stream.fd)
        self.streams.clear()
        self.identities.clear()
        self.gate.reset()

    def run(self, control):
        scan_at = 0
        previous = time.clock_gettime(time.CLOCK_BOOTTIME)
        try:
            while True:
                now = time.clock_gettime(time.CLOCK_BOOTTIME)
                if now - previous > 2:
                    self.close()  # suspend or delay: reopen without reusing a gesture
                    scan_at = 0
                previous = now
                if now >= scan_at:
                    self.scan()
                    scan_at = now + .75
                ready, _, _ = select.select([control, *(s.fd for s in self.streams.values())], [], [], .025)
                if control in ready:
                    return  # parent EOF or shutdown request
                for path, stream in list(self.streams.items()):
                    if stream.fd in ready:
                        try:
                            self.consume(stream, now)
                        except (OSError, EOFError):
                            os.close(stream.fd)
                            del self.streams[path]
                            self.identities.pop(path)
                            self.resync()
                            scan_at = 0
                if not self.blocked and self.status == "ready":
                    self.gate.tick(now)
        finally:
            self.close()


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    channel = socket.socket(fileno=int(sys.argv[1]))

    def emit(token):
        if token in GESTURES or token in STATES:
            channel.send(token.encode("ascii"))

    try:
        Watcher(emit, sys.argv[2]).run(channel)
    except Exception:  # noqa: BLE001
        # Exceptions never include buffers, codes, paths or tracebacks.
        pass
    finally:
        channel.close()


if __name__ == "__main__":
    main()
