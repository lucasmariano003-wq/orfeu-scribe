"""Synthetic data only. Failures never include input events or codes."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest

from orfeuscribe.gestures import GestureGate, MODIFIERS, ModifierGesture
from orfeuscribe.keywatch import EVENT, GESTURES, STATES, LETTERS, Stream, Watcher, discover


def inventory(path, keyboard=True):
    bits = sum(1 << i for i in LETTERS) if keyboard else 1 << 272
    words = []
    while bits:
        words.insert(0, format(bits & ((1 << 64) - 1), 'x'))
        bits >>= 64
    return f'H: Handlers=kbd {path.name}\nB: EV=3\nB: KEY={" ".join(words)}\n\n'


class KeywatchTests(unittest.TestCase):
    def check(self, condition):
        self.assertTrue(bool(condition), "Watcher contract not satisfied")

    def stroke(self, keys, mode="toggle", gap=.01):
        output = []
        gate = GestureGate(output.append, mode)
        now = 0
        for key in keys:
            gate.feed(key, 1, now)
            now += gap
            gate.tick(now)
        gate.tick(now + .2)
        for key in reversed(keys):
            gate.feed(key, 0, now + .3)
        return output

    def test_four_gestures_and_hold(self):
        for name, keys in (("dictation", [125]), ("ask", [56, 45]),
                           ("translation", [56, 42]), ("paste", [29, 54, 47])):
            self.check(self.stroke(keys) == ["toggle:" + name])
            expected = ["toggle:paste"] if name == "paste" else ["start:" + name, "stop:" + name]
            self.check(self.stroke(keys, "hold") == expected)

    def test_negative_combinations_and_sides(self):
        for keys in ([125, 38], [125, 30], [125, 48], [56, 15], [56, 42, 30],
                     [30], [126], [100, 45], [97, 42, 47], [29, 42, 47], [45, 56], [30, 125]):
            self.check(self.stroke(keys) == [])
            self.check(self.stroke(keys, "hold") == [])

    def test_hold_cancels_without_stop_after_delay(self):
        out = []
        g = GestureGate(out.append, "hold")
        g.feed(125, 1, 0)
        g.tick(.2)
        g.feed(30, 1, .3)
        g.feed(30, 0, .4)
        g.feed(125, 0, .5)
        self.check(out == ["start:dictation", "cancel:dictation"])

    def test_click_scroll_and_repeat(self):
        for mode in ("toggle", "hold"):
            out = []
            g = GestureGate(out.append, mode)
            g.feed(125, 1, 0)
            g.feed(125, 2, .01)
            g.pointer(.02)
            g.feed(125, 0, .3)
            self.check(out == [])
            g.feed(125, 1, 1)
            g.feed(125, 2, 1.01)
            g.tick(1.2)
            g.feed(125, 0, 1.3)
            self.check(out == (["toggle:dictation"] if mode == "toggle" else ["start:dictation", "stop:dictation"]))

    def test_interrupted_partial_release_cancels_hold(self):
        for pointer in (False, True):
            out = []
            g = GestureGate(out.append, 'hold')
            g.feed(56, 1, 0)
            g.feed(42, 1, .01)
            g.tick(.3)
            g.feed(42, 0, .4)
            if pointer:
                g.pointer(.5)
            else:
                g.feed(30, 1, .5)
                g.feed(30, 0, .6)
            g.feed(56, 0, .7)
            self.check(out == ['start:translation', 'cancel:translation'])

    def test_reset_cancels_hold_and_discards_pending_toggle(self):
        for mode in ('toggle', 'hold'):
            out = []
            g = GestureGate(out.append, mode)
            g.feed(125, 1, 0)
            g.tick(.3)
            g.reset()
            g.feed(125, 0, .5)
            self.check(out == ([] if mode == 'toggle' else ['start:dictation', 'cancel:dictation']))

    def test_release_all_before_delivery_and_no_new_press(self):
        out = []
        g = GestureGate(out.append)
        for key in (29, 54, 47):
            g.feed(key, 1, 0)
        g.feed(47, 0, 1)
        self.check(not out)
        g.feed(30, 1, 1)
        for key in (30, 54, 29):
            g.feed(key, 0, 2)
        self.check(not out)

    def test_only_modifiers_and_counts_remain(self):
        g = ModifierGesture()
        for code in range(1, 255):
            if code not in MODIFIERS:
                g.feed(code, 1)
        self.check(set(vars(g)) == {"mode", "modifiers", "others", "candidate", "dirty", "fired"})
        self.check(g.candidate is None and g.dirty and g.modifiers == [0] * 8)
        self.check(all(type(value) in (str, int, bool, list, type(None)) for value in vars(g).values()))

    def test_discovery_keyboard_pointer_and_non_keyboard(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            text = inventory(p / "event123") + inventory(p / "event456", False)
            text += 'H: Handlers=kbd event7\nB: EV=3\nB: KEY=1\n\n'
            self.check(discover(text, root) == {str(p / "event123"): True, str(p / "event456"): False})

    def test_partial_pipe_and_file(self):
        raw = EVENT.pack(0, 0, 1, 125, 1)
        read, write = os.pipe()
        try:
            stream = Stream(read, True)
            os.write(write, raw[:7])
            self.check(stream.read() is None)
            os.write(write, raw[7:])
            self.check(stream.read() == (1, 125, 1) and not stream.fragment)
        finally:
            os.close(read)
            os.close(write)
        with tempfile.TemporaryFile() as file:
            file.write(raw)
            file.seek(0)
            self.check(Stream(file.fileno(), True).read() == (1, 125, 1))

    def test_hotplug_removal_reconnect_and_permission(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "event123"
            wanted = {}
            out = []
            watcher = Watcher(out.append, inventory=lambda: wanted, snapshot=lambda _: ([0]*8, 0))
            try:
                watcher.scan()
                self.check(watcher.status == "waiting")
                path.touch()
                wanted[str(path)] = True
                watcher.scan()
                fd = watcher.streams[str(path)].fd
                self.check(watcher.status == "ready")
                watcher.gate.feed(125, 1, 0)
                path.unlink()
                watcher.scan()
                with self.assertRaises(OSError):
                    os.fstat(fd)
                self.check(not watcher.gate.machine.down)
                path.touch()
                watcher.scan()
                self.check(watcher.status == "ready")
                watcher.close()
                watcher.opener = lambda _: (_ for _ in ()).throw(PermissionError())
                watcher.scan()
                self.check(watcher.status == "unavailable" and not watcher.streams)
            finally:
                watcher.close()

    def test_initial_hold_and_dropped_sync(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "event123"
            path.write_bytes(b''.join(EVENT.pack(0, 0, *event) for event in
                ((1, 125, 0), (0, 3, 0), (1, 125, 1), (0, 0, 0), (1, 125, 1), (1, 125, 0))))
            snapshots = iter([([1]+[0]*7, 0), ([0]*8, 0)])
            out = []
            w = Watcher(out.append, inventory=lambda: {str(path): True}, snapshot=lambda _: next(snapshots))
            try:
                w.scan()
                stream = w.streams[str(path)]
                for _ in range(6):
                    w.consume(stream, 0)
                self.check(out == ["ready", "toggle:dictation"])
            finally:
                w.close()

    def test_no_input_on_any_output(self):
        out = []
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / "event123"
                # A long typing sequence is discarded, with no derived text.
                with path.open('wb') as file:
                    for code in range(1, 256):
                        for value in (1, 2, 0):
                            file.write(EVENT.pack(0, 0, 1, code, value))
                w = Watcher(out.append, inventory=lambda: {str(path): True}, snapshot=lambda _: ([0]*8, 0))
                try:
                    w.scan()
                    stream = w.streams[str(path)]
                    for _ in range(255 * 3):
                        w.consume(stream, 0)
                finally:
                    w.close()
        self.check(not stdout.getvalue() and not stderr.getvalue())
        self.check(set(out) <= GESTURES | STATES)


if __name__ == "__main__":
    unittest.main()
