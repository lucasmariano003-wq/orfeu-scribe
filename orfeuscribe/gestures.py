"""Gesture automaton reduced to modifiers and gesture state.

No ordinary key is kept in the state: only their count and the candidate's
name. The start/stop/cancel/toggle contract is the same as gestures.js. The
reader defers toggle until everything is released, and start by 180 ms.
"""

MODIFIERS = (125, 126, 56, 100, 29, 97, 42, 54)
SUPER, SUPER_R, ALT, ALT_R, CTRL, CTRL_R, SHIFT, SHIFT_R = range(8)
MASKS = {"dictation": 1 << SUPER, "translation": (1 << ALT) | (1 << SHIFT),
         "ask": 1 << ALT, "paste": (1 << CTRL) | (1 << SHIFT_R)}


class ModifierGesture:
    def __init__(self, mode="toggle"):
        self.mode = mode
        self.reset()

    def reset(self):
        self.modifiers = [0] * 8
        self.others = 0
        self.candidate = None
        self.dirty = False
        self.fired = False

    @property
    def down(self):
        return bool(any(self.modifiers) or self.others)

    @property
    def mask(self):
        return sum(1 << i for i, count in enumerate(self.modifiers) if count)

    def invalidate(self):
        out = []
        if self.candidate and self.mode == "hold":
            out.append(("cancel", self.candidate))
        self.candidate = None
        self.dirty = self.down
        return out

    def feed(self, code, value):
        if value not in (0, 1):  # autorepeat does not change the state
            return []
        out = []
        modifier = MODIFIERS.index(code) if code in MODIFIERS else None
        if value:
            if modifier is None:
                self.others += 1
            else:
                self.modifiers[modifier] += 1
            exact = None
            if not self.others:
                exact = next((name for name in ("dictation", "translation") if MASKS[name] == self.mask), None)
            elif self.others == 1 and modifier is None:
                if code == 45 and self.mask == MASKS["ask"]:
                    exact = "ask"
                elif code == 47 and self.mask == MASKS["paste"]:
                    exact = "paste"
            prefix = not self.others and any(self.mask & mask == self.mask for mask in MASKS.values())
            if (not prefix and not exact) or self.fired:
                return self.invalidate()
            if not self.dirty and exact:
                self.candidate = exact
                if self.mode == "hold":
                    out.append(("start", exact))
        else:
            if modifier is not None and not self.modifiers[modifier]:
                return []
            if modifier is None and not self.others:
                return []
            if self.candidate and not self.dirty and not self.fired:
                out.append(("stop" if self.mode == "hold" else "toggle", self.candidate))
                self.candidate = None
                self.fired = True
            if modifier is None:
                self.others = max(0, self.others - 1)
            else:
                self.modifiers[modifier] -= 1
            if not self.down:
                self.reset()
        return out


class GestureGate:
    def __init__(self, emit, mode="toggle"):
        self.machine = ModifierGesture(mode)
        self.emit = emit
        self.pending = None
        self.deadline = 0
        self.active = None
        self.released = None

    def actions(self, actions, now):
        for action, name in actions:
            if action == "start":
                self.pending, self.deadline = name, now + .180
            elif action == "cancel":
                self.pending = self.released = None
                if self.active:
                    self.emit("cancel:" + self.active)
                    self.active = None
            elif action == "toggle" or name == "paste":
                self.pending = None
                self.released = "toggle:" + name
            elif action == "stop":
                self.pending = None
                if self.active:
                    self.released = "stop:" + self.active
        if not self.machine.down and self.released:
            self.emit(self.released)
            self.active = None
            self.released = None

    def feed(self, code, value, now):
        # A new press after release began invalidates the pending delivery.
        if value == 1 and self.released:
            if self.active:
                self.emit("cancel:" + self.active)
                self.active = None
            self.released = None
        self.actions(self.machine.feed(code, value), now)

    def pointer(self, now):
        self.pending = self.released = None
        self.actions(self.machine.invalidate(), now)
        if self.active:
            self.emit("cancel:" + self.active)
            self.active = None

    def tick(self, now):
        if self.pending and self.pending != "paste" and now >= self.deadline:
            self.active = self.pending
            self.pending = None
            self.emit("start:" + self.active)

    def reset(self):
        if self.active:
            self.emit("cancel:" + self.active)
        self.pending = self.active = self.released = None
        self.machine.reset()
