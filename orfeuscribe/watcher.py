"""Supervision of the optional key watcher; only closed names cross the boundary."""
from pathlib import Path
import socket
import subprocess
import time
from gi.repository import Gio, GLib

from .i18n import N_, _
from .keywatch import GESTURES, STATES

_LABELS = {"disabled": N_("Off · no device opened"), "extension": N_("Waiting for the GNOME Shell extension"),
           "paused": N_("Paused · no device opened"), "starting": N_("Starting…"),
           "ready": N_("Active"), "waiting": N_("Waiting for a keyboard"),
           "unavailable": N_("No read access to input devices"),
           "failed": N_("Interrupted · trying again")}


def label(status):
    return _(_LABELS.get(status, status))


class WatcherSupervisor:
    def __init__(self, app):
        self.app = app
        self.process = self.channel = None
        self.source = 0
        self.status = "extension"
        self.lease = 0
        self.allowed = False
        self.mode = None
        self.active = False
        self.timer = GLib.timeout_add(500, self.reconcile)
        self.changed = app.settings.connect("changed", lambda *_args: self.reconcile())
        self.sleeping = False
        self.sleep_signal = 0
        try:
            self.system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self.sleep_signal = self.system.signal_subscribe(
                "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self._sleep)
        except GLib.Error:
            self.system = None  # isolated sessions without a system bus

    def _sleep(self, _connection, _sender, _path, _interface, _signal, parameters, *_args):
        self.sleeping = parameters.unpack()[0]
        self.lease = 0
        self.reconcile()

    def ready(self, allowed):
        self.lease = time.monotonic() + 7
        self.allowed = allowed
        self.reconcile()

    def set_status(self, value):
        if value != self.status:
            self.status = value
            self.app.notify()

    def stop(self):
        if self.source:
            GLib.source_remove(self.source)
            self.source = 0
        if self.channel:
            self.channel.close()
            self.channel = None
        if self.process:
            try:
                self.process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
        if self.active:
            self.app.cancel()
        self.active = False

    def reconcile(self):
        reason = None
        if not self.app.settings.get_boolean("watcher-enabled"):
            reason = "disabled"
        elif self.app.settings.get_boolean("paused") or self.sleeping:
            reason = "paused"
        elif time.monotonic() > self.lease:
            reason = "extension"
        elif not self.allowed:
            reason = "paused"
        if reason:
            self.stop()
            self.set_status(reason)
            return GLib.SOURCE_CONTINUE
        mode = self.app.settings.get_string("recording-mode")
        if self.process and (self.process.poll() is not None or self.mode != mode):
            self.stop()
        if not self.process:
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                self.process = subprocess.Popen(["/usr/bin/python3", "-m", "orfeuscribe.keywatch", str(child.fileno()), mode],
                                                cwd=Path(__file__).resolve().parent.parent, pass_fds=(child.fileno(),),
                                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, env={"LANG": "C.UTF-8"})
            except OSError:
                parent.close()
                self.set_status("failed")
                return GLib.SOURCE_CONTINUE
            finally:
                child.close()
            self.channel = parent
            self.mode = mode
            self.set_status("starting")
            self.source = GLib.io_add_watch(parent.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.receive)
        return GLib.SOURCE_CONTINUE

    def receive(self, _fd, condition):
        try:
            token = self.channel.recv(64).decode("ascii")
        except (OSError, UnicodeError):
            token = ""
        if not token:
            self.source = 0
            self.stop()
            self.set_status("failed")
            return GLib.SOURCE_REMOVE
        if token in STATES:
            self.set_status(token)
        elif token in GESTURES and self.allowed and time.monotonic() <= self.lease:
            self.active = token.startswith("start:") or (self.active and token.startswith("toggle:"))
            self.app.physical_gesture(token)
        return GLib.SOURCE_CONTINUE

    def close(self):
        GLib.source_remove(self.timer)
        self.app.settings.disconnect(self.changed)
        if self.system and self.sleep_signal:
            self.system.signal_unsubscribe(self.sleep_signal)
        self.stop()
