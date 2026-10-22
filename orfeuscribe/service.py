"""Single-instance application and session D-Bus service."""
from __future__ import annotations

import json
import os
import threading
import sqlite3
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from . import providers
from .audio import MicWatch, Recorder, SilenceGate
from .config import CONFIG_DIR, DATA_DIR, Config
from .history import FILENAME as HISTORY_FILE, History
from .i18n import _
from .ids import APP_ID, BUS_NAME, BUS_PATH
from .modes import MODES, available as available_modes
from .pipeline import Cancelled, VoiceCancelled, run_pipeline
from .providers import ProviderError
from .settings import shell_settings
from .text import choose_mode
from .vocabulary import FILENAME as VOCABULARY_FILE, load_vocabulary

DEFAULT_REQUEST = {"operation": "dictation", "selection": "", "pid": 0}
# The extension calls SetRequest and then Start. An older request was orphaned
# (the key was released halfway) and must not turn the next window dictation
# into a question about an old selection.
REQUEST_TTL = 10.0
# "Revert to transcript" applies right after delivery: the extension offers it
# for 10 s; the service accepts it with room for the paste and the D-Bus round trip.
REVERT_TTL = 60.0
TERMINALS = ("gnome-terminal", "kgx", "console", "ptyxis", "kitty", "alacritty", "wezterm", "foot")
CONTEXT_OPERATIONS = ("dictation", "translation", "reply")
OPERATIONS = ("dictation", "ask", "translation", "edit", "write", "reply")
REVERTIBLE_MODES = frozenset(MODES + ("translation",))
OWN_WINDOW = APP_ID.lower()


class FieldContext:
    """AT-SPI read in parallel with listening: text before the cursor or the visible conversation."""

    def __init__(self, pid, want):
        self.value = None
        self.done = threading.Event()
        self.want = want
        threading.Thread(target=self._read, args=(pid, want), name="orfeuscribe-context", daemon=True).start()

    def _read(self, pid, want):
        try:
            from .context import read
            self.value = read(pid, want)
        finally:
            self.done.set()

    def get(self, timeout=0.0):
        self.done.wait(timeout)
        return self.value if self.done.is_set() else None


BUS_XML = f"""<node><interface name="{BUS_NAME}">
  <method name="Start"/><method name="Stop"/><method name="Toggle"/><method name="Cancel"/>
  <method name="Open"/><method name="Quit"/>
  <method name="SetContext"><arg name="wm_class" type="s" direction="in"/><arg name="title" type="s" direction="in"/></method>
  <method name="SetRequest"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="i" direction="in"/></method>
  <method name="PasteLast"/>
  <method name="Recover"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <signal name="Rescued"><arg type="s"/></signal>
  <signal name="MicWarning"><arg type="s"/></signal>
  <signal name="Partial"><arg type="s"/></signal>
  <method name="RevertToRaw"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <signal name="Revertible"><arg type="s"/></signal>
  <method name="WatcherReady"><arg type="b" direction="in"/></method>
  <method name="GetWatcherStatus"><arg type="s" direction="out"/></method>
  <method name="BeginShortcutProof"><arg type="b" direction="out"/></method>
  <method name="EndShortcutProof"/>
  <method name="ConfirmGesture"><arg type="s" direction="in"/></method>
  <signal name="Gesture"><arg type="s"/></signal>
  <signal name="ProbeGesture"><arg type="s"/></signal>
  <signal name="GestureProved"><arg type="s"/></signal>
  <signal name="Card"><arg type="s"/></signal>
  <signal name="Operation"><arg type="s"/><arg type="s"/></signal>
  <signal name="Notice"><arg type="s"/></signal>
  <signal name="Replace"><arg type="s"/><arg type="s"/></signal>
  <method name="GetStatus"><arg name="state" type="s" direction="out"/><arg name="mode" type="s" direction="out"/></method>
  <signal name="StateChanged"><arg name="state" type="s"/></signal>
  <signal name="Level"><arg name="level" type="d"/></signal>
  <signal name="CancelDelivery"/>
  <signal name="Deliver"><arg name="text" type="s"/></signal>
  <signal name="Error"><arg name="message" type="s"/></signal>
</interface></node>"""


class OrfeuScribeApplication(Adw.Application):
    def __init__(self, background=False, config_dir=CONFIG_DIR, history_path=DATA_DIR / HISTORY_FILE):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.background = background
        self.window = None
        self.state = "idle"
        self.context = {"wm_class": "", "title": ""}
        self.recording_context = dict(self.context)
        self.mode = "dictation"
        self.request = dict(DEFAULT_REQUEST)
        self.request_at = 0.0
        self.selection_snapshot = None
        self.generation = 0
        self.last_result = None
        self.last_error = ""
        self.recording_timer = 0
        self.idle_timer = 0
        self.listeners = []
        self.config_dir = config_dir
        self.history_path = history_path
        self.segmented = False
        self.segment_queue = []
        self.segment_active = False
        self.segment_tail = ''
        self.segment_count = 0
        self.file_progress = None
        self.revertible = None
        # The audio of the dictation being processed: it goes to rescue if transcription or revision fails.
        self.work_audio = None
        self.recovering = None
        self.last_rescue = None
        self.mic_watch = None
        self.chime = None
        self.inhibit_cookie = 0
        self.inhibit_timer = 0
        self.live = None
        self.live_text = ""
        self.field_context = None
        self.credentials = None

    @property
    def vocabulary_path(self):
        return self.config_dir / VOCABULARY_FILE

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold()
        self.config = Config(self.config_dir)
        self.history = History(self.history_path)
        self.history.prune_days(self.config['history_days'])
        from .rescue import RescueStore
        self.rescue = RescueStore(self.history_path.parent / "rescue")
        self._prune_rescue()
        self.retention_timer = GLib.timeout_add_seconds(3600, self._retention)
        self.settings = shell_settings()
        self.settings.connect("changed::paused", self._paused)
        self.vocab = load_vocabulary(self.vocabulary_path)
        from .credentials import default_store
        self.credentials = default_store()
        self.reload_provider()
        self.recorder = Recorder(self.level, self.fail, self.meter)
        if os.environ.get("ORFEUSCRIBE_PROOF") == "1":
            # Only in an isolated proof session: a synthetic source instead of the microphone.
            self.recorder.test_source = os.environ.get("ORFEUSCRIBE_TEST_SOURCE") or None
        from .sounds import Chime
        self.chime = Chime()
        self.connection = self.get_dbus_connection()
        self.registration = self.connection.register_object(BUS_PATH, Gio.DBusNodeInfo.new_for_xml(BUS_XML).interfaces[0],
                                                            self._method, None, None)
        self.bus_owner = Gio.bus_own_name_on_connection(self.connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
        latest = self.history.search(limit=1)
        self.last_record = latest[0] if latest else None
        self.proof_owner = None
        self.proof_until = 0
        self.proof_signal = self.connection.signal_subscribe('org.freedesktop.DBus', 'org.freedesktop.DBus',
            'NameOwnerChanged', '/org/freedesktop/DBus', None, Gio.DBusSignalFlags.NONE, self._proof_owner_changed)
        from .watcher import WatcherSupervisor
        self.watcher = WatcherSupervisor(self)

    def reload_provider(self):
        """Rebuilds the service from the settings; keys are read again at request time."""
        self.provider = providers.build(self.config, self.credentials)
        self.notify()

    def do_command_line(self, command_line):
        arguments = command_line.get_arguments()
        if '--transcribe' in arguments:
            self.activate()
            path = arguments[arguments.index('--transcribe') + 1]
            mode = arguments[arguments.index('--mode') + 1] if '--mode' in arguments else 'dictation'
            language = arguments[arguments.index('--language') + 1] if '--language' in arguments else None
            self.transcribe_file(path, mode, language)
            return 0
        if "--background" not in arguments:
            self.activate()
        return 0

    def do_activate(self):
        if self.window is None:
            from .window import OrfeuScribeWindow
            self.window = OrfeuScribeWindow(self)
        self.window.present()

    def _method(self, connection, sender, path, interface, method, parameters, invocation):
        if method == "GetWatcherStatus":
            invocation.return_value(GLib.Variant("(s)", (self.watcher.status,)))
            return
        if method in ("WatcherReady", "ConfirmGesture"):
            # Only the session's Shell may open or renew this door.
            try:
                owner = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                    "GetNameOwner", GLib.Variant("(s)", ("org.gnome.Shell",)), None, Gio.DBusCallFlags.NONE, 1000,
                    None).unpack()[0]
            except GLib.Error:
                owner = None
            if sender != owner:
                invocation.return_dbus_error(BUS_NAME + ".AccessDenied", "Only the GNOME Shell extension.")
                return
            if method == "WatcherReady":
                self.watcher.ready(parameters.unpack()[0])
            elif self.proof_owner and time.monotonic() < self.proof_until:
                from .keywatch import GESTURES
                token = parameters.unpack()[0]
                if token in GESTURES:
                    self.connection.emit_signal(self.proof_owner, BUS_PATH, BUS_NAME, "GestureProved",
                                                GLib.Variant("(s)", (token,)))
            invocation.return_value(None)
            return
        if method == "BeginShortcutProof":
            allowed = self.watcher.status == "ready" and self.state == "idle" and time.monotonic() >= self.proof_until
            if allowed:
                self.proof_owner, self.proof_until = sender, time.monotonic() + 120
            invocation.return_value(GLib.Variant("(b)", (allowed,)))
            return
        if method == "EndShortcutProof":
            if sender == self.proof_owner:
                self.proof_owner, self.proof_until = None, 0
            invocation.return_value(None)
            return
        if method == "GetStatus":
            invocation.return_value(GLib.Variant("(ss)", (self.state, self.mode)))
            return
        if method == "SetRequest":
            operation, selection, pid = parameters.unpack()
            if operation == "edit" and not selection.strip():
                operation = "write"
            self.request = {"operation": operation if operation in OPERATIONS else "dictation",
                            "selection": selection[:24000], "pid": pid}
            self.request_at = time.monotonic()
        elif method == "RevertToRaw":
            invocation.return_value(GLib.Variant("(b)", (self.revert_to_raw(parameters.unpack()[0]),)))
            return
        elif method == "Recover":
            invocation.return_value(GLib.Variant("(b)", (self.recover(parameters.unpack()[0]),)))
            return
        elif method == "PasteLast":
            self.emit_bus("Deliver", "(s)", self.history.last_dictation())
        elif method == "SetContext":
            wm_class, title = parameters.unpack()
            self.context = {"wm_class": wm_class[:512], "title": title[:512]}
        else:
            {"Start": self.start, "Stop": self.stop, "Toggle": self.toggle,
             "Cancel": self.cancel, "Open": self.activate, "Quit": self.quit}[method]()
        invocation.return_value(None)

    def emit_bus(self, signal, signature, *values):
        self.connection.emit_signal(None, BUS_PATH, BUS_NAME, signal, GLib.Variant(signature, values))

    def physical_gesture(self, token):
        probing = self.proof_owner and time.monotonic() < self.proof_until
        self.emit_bus("ProbeGesture" if probing else "Gesture", "(s)", token)

    def _proof_owner_changed(self, _connection, _sender, _path, _interface, _signal, parameters):
        name, _old, new = parameters.unpack()
        if name == self.proof_owner and not new:
            self.proof_owner, self.proof_until = None, 0

    def notify(self):
        for callback in self.listeners:
            callback()

    def set_state(self, state):
        if self.idle_timer:
            GLib.source_remove(self.idle_timer)
            self.idle_timer = 0
        previous, self.state = self.state, state
        if previous == "listening" and state != "listening":
            self._chime("end")
            self._stop_live()
        if state not in ("listening", "transcribing", "polishing"):
            self._release_inhibit()
        self.emit_bus("StateChanged", "(s)", state)
        self.notify()

    def level(self, value):
        if self.state == "listening":
            self.emit_bus("Level", "(d)", value)
            if self.window:
                self.window.set_level(value)
            gate = getattr(self, 'silence_gate', None)
            action = gate.feed(value, time.monotonic()) if gate else None
            if action == 'stop':
                self.stop()
            elif action == 'part':
                self._queue_segment(self.recorder.take_chunk())

    def meter(self, rms_db, peak_db):
        """Microphone check: warns in the pill without interrupting the recording."""
        if self.state != "listening" or self.mic_watch is None:
            return
        warning = self.mic_watch.feed(rms_db, peak_db, time.monotonic())
        if warning:
            message = MicWatch.message(warning)
            self.emit_bus("MicWarning", "(s)", message)
            if self.window and self.window.get_visible():
                self.window.toast(message)

    def _chime(self, kind):
        if self.chime and self.config.get("sounds", False):
            try:
                self.chime.play(kind)
            except (GLib.Error, RuntimeError, AttributeError):
                pass

    # Long dictation: the computer does not suspend in the middle of speech or revision.
    INHIBIT_AFTER = 20

    def _schedule_inhibit(self):
        self._release_inhibit()
        self.inhibit_timer = GLib.timeout_add_seconds(self.INHIBIT_AFTER, self._inhibit_now)

    def _inhibit_now(self):
        self.inhibit_timer = 0
        if self.state in ("listening", "transcribing", "polishing") and not self.inhibit_cookie:
            try:
                from gi.repository import Gtk
                self.inhibit_cookie = self.inhibit(None, Gtk.ApplicationInhibitFlags.SUSPEND |
                                                   Gtk.ApplicationInhibitFlags.IDLE, _("Dictation in progress")) or 0
            except (GLib.Error, TypeError):
                self.inhibit_cookie = 0
        return GLib.SOURCE_REMOVE

    def _release_inhibit(self):
        if self.inhibit_timer:
            GLib.source_remove(self.inhibit_timer)
            self.inhibit_timer = 0
        if self.inhibit_cookie:
            self.uninhibit(self.inhibit_cookie)
            self.inhibit_cookie = 0

    def _start_live(self):
        """Live text: after ~15 s of speech, the end of the provisional text in the pill."""
        self._stop_live()
        request = getattr(self, "recording_request", {})
        if not self.config.get("live_text", False) or request.get("operation") not in ("dictation", "translation"):
            return
        from .live import LiveText
        generation = self.generation
        self.live = LiveText(self.provider, request.get("input_language", "en"), self.vocab.terms,
                             lambda text: GLib.idle_add(self._partial, generation, text))
        self.recorder.on_pcm = self.live.feed

    def _stop_live(self):
        live, self.live = self.live, None
        if live:
            live.close()
        if getattr(self.recorder, "on_pcm", None):
            self.recorder.on_pcm = None
        self.live_text = ""

    def _partial(self, generation, text):
        if generation == self.generation and self.state == "listening" and self.live:
            from .live import tail
            self.live_text = text
            self.emit_bus("Partial", "(s)", tail(text, 64))
            self.notify()
        return GLib.SOURCE_REMOVE

    def _paused(self, *args):
        if self.settings.get_boolean("paused"):
            self.cancel()
        self.notify()

    def _languages(self):
        return self.config.get("input_language") or "en"

    def start(self):
        if self.settings.get_boolean("paused") or self.state in ("listening", "transcribing", "polishing"):
            return
        if not getattr(self.provider, "configured", True):
            # Nothing could be sent anywhere: do not open the microphone at all.
            self.fail(_("No speech service is configured. Open Settings → Service."))
            return
        self.generation += 1
        self.last_error = ""
        self.file_progress = None
        if time.monotonic() - self.request_at > REQUEST_TTL:
            self.request = dict(DEFAULT_REQUEST)
        self.recording_request = dict(self.request)
        self.request = dict(DEFAULT_REQUEST)
        if self.recording_request["operation"] == "reply" and not self.config.get("reply_context", False):
            # Off by default: the shortcut becomes a plain dictation and the pill says so.
            self.recording_request["operation"] = "dictation"
            self.emit_bus("MicWarning", "(s)", _("Reply with context is off in Settings: plain dictation."))
        self.recording_context = dict(self.context)
        self.recording_context["selection"] = self.recording_request["selection"]
        self.recording_request["language"] = self.settings.get_string("translation-language")
        from .selection import snapshot
        self.selection_snapshot = snapshot(self.recording_request["pid"], self.recording_request["selection"],
                                           self.recording_request["operation"])
        self.recording_request["snapshot"] = self.selection_snapshot
        self.emit_bus("Operation", "(ss)", self.recording_request["operation"], self.recording_request["language"])
        self.mode = choose_mode(**self.context, rules=self.config["rules"], fixed=self.settings.get_string("fixed-mode"),
                                available=available_modes(self.config))
        self.field_context = None
        operation, pid = self.recording_request["operation"], self.recording_request.get("pid")
        wm_class = self.context.get("wm_class", "").lower()
        if (operation in CONTEXT_OPERATIONS and pid and wm_class != OWN_WINDOW
                and not any(name in wm_class for name in TERMINALS)):
            # No waiting: listening starts now; the result arrives before delivery.
            self.field_context = FieldContext(pid, "conversation" if operation == "reply" else "caret")
        self.recording_request["field_context"] = self.field_context
        self.vocab = load_vocabulary(self.vocabulary_path)
        from .languages import choose_language
        self.recording_request['input_language'] = choose_language(self.recording_context,
            self.config.get('language_rules', []), self._languages())
        self.recording_start = time.monotonic()
        self.segmented = bool(self.config.get('pause_delivery', False) and self.recording_request['operation'] == 'dictation')
        self.segment_queue, self.segment_active, self.segment_tail, self.segment_count = [], False, '', 0
        self.segment_start = self.recording_start
        self.silence_gate = SilenceGate(self.config.get('silence_seconds', 3),
            self.config.get('silence_stop', False), self.segmented, self.recording_start)
        self.mic_watch = MicWatch(self.recording_start)
        try:
            self.recorder.start(self.config["microphone"])
            self.set_state("listening")
            self._chime("start")
            self._start_live()
            self._schedule_inhibit()
            self.recording_timer = GLib.timeout_add_seconds(600, self._time_limit)
        except RuntimeError as exc:
            self.recorder.cancel()
            self.fail(str(exc))

    def _time_limit(self):
        self.recording_timer = 0
        self.stop()
        return GLib.SOURCE_REMOVE

    def _clear_recording_timer(self):
        if self.recording_timer:
            GLib.source_remove(self.recording_timer)
            self.recording_timer = 0

    def stop(self):
        if self.state != "listening":
            return
        self._clear_recording_timer()
        recording = time.monotonic() - self.recording_start
        audio = self.recorder.stop()
        if self.segmented:
            self.set_state('transcribing')
            if len(audio) >= 3244:
                self._queue_segment(audio)
            elif not self.segment_active and not self.segment_queue:
                self.set_state('done')
                self.idle_timer = GLib.timeout_add(1800, self._settle)
            return
        if len(audio) < 3244:
            self.fail(_("The dictation was too short. Speak a little longer and try again."))
            return
        if self.recording_request["operation"] == "translation":
            self.recording_request["language"] = self.settings.get_string("translation-language")
            self.emit_bus("Operation", "(ss)", "translation", self.recording_request["language"])
        self._work(audio, recording, self.recording_context, self.mode, request=self.recording_request)

    def toggle(self):
        self.stop() if self.state == "listening" else self.start()

    def dictate_from_window(self):
        # The window button is always a plain dictation: it never inherits a request
        # (question, selection, PID) the extension may have left behind.
        self.context = {"wm_class": APP_ID, "title": "Dictation"}
        if self.state != "listening":
            self.request = dict(DEFAULT_REQUEST)
        self.toggle()

    def cancel(self, reason="cancelled"):
        # No dictation is lost: the audio being recorded or processed goes to rescue.
        if not self.recovering:
            self._rescue_all(reason, "")
        self.work_audio = None
        self.recovering = None
        self.generation += 1
        self.last_error = ""
        self._clear_recording_timer()
        self.recorder.cancel()
        self.segment_queue = []
        self.segmented = False
        if self.file_progress:
            self.file_progress['stage'] = 'cancelled'
        self.emit_bus('CancelDelivery', '()')
        self.set_state("idle")

    def fail(self, message, rescued=None):
        if rescued:
            pass
        elif self.recovering:
            # The audio is already in rescue: just record the new problem.
            self.rescue.update(self.recovering, message=message[:300])
            rescued = self.recovering
        else:
            rescued = self._rescue_all("error", message)
        self.work_audio = None
        self.recovering = None
        self.generation += 1
        self._clear_recording_timer()
        self.recorder.cancel()
        self.segment_queue = []
        self.segmented = False
        self.last_error = message
        self.emit_bus('CancelDelivery', '()')
        self.set_state("error")
        if rescued:
            # Before the error: the pill can offer Recover together with the message.
            self.emit_bus("Rescued", "(s)", json.dumps({"id": rescued, "reason": "error"}))
        self.emit_bus("Error", "(s)", message)

    def _rescue_audio(self, audio, reason, message, context, mode, request, recording, history_id=None):
        store = getattr(self, "rescue", None)
        if store is None or not audio:
            return None
        try:
            ident = store.save(audio, reason=reason, message=message, context=context, mode=mode,
                               request=request, recording=recording, history_id=history_id)
        except (OSError, ValueError):
            return None
        if ident:
            self.last_rescue = ident
            self.notify()
        return ident

    def _rescue_all(self, reason, message):
        first = None
        if self.state == "listening":
            take = getattr(self.recorder, "take_chunk", None)
            audio = take() if take else b""
            first = self._rescue_audio(audio, reason, message, self.recording_context, self.mode,
                                       getattr(self, "recording_request", {}),
                                       time.monotonic() - getattr(self, "recording_start", time.monotonic()))
        if self.state in ("listening", "transcribing", "polishing"):
            first = self._rescue_work(reason, message) or first
        return first

    def _rescue_work(self, reason, message, history_id=None):
        """The audio of the current work and of the parts still queued."""
        first = None
        pending = [self.work_audio] if self.work_audio else []
        pending += [dict(audio=audio, recording=recording, context=self.recording_context, mode=self.mode,
                         request=getattr(self, "recording_request", {}))
                    for audio, recording in getattr(self, "segment_queue", [])]
        for item in pending:
            ident = self._rescue_audio(item["audio"], reason, message, item["context"], item["mode"],
                                       item["request"], item["recording"], history_id)
            first = first or ident
        return first

    def _prune_rescue(self):
        try:
            self.rescue.prune()
        except OSError:
            pass

    def recover(self, ident):
        """Transcribes and revises the kept audio again; the entry goes to the history."""
        if self.state in ("listening", "transcribing", "polishing"):
            return False
        item = self.rescue.get(ident) if isinstance(ident, str) else None
        if item is None:
            return False
        try:
            audio = self.rescue.audio(ident)
        except (OSError, ValueError):
            return False
        self.generation += 1
        self.last_error = ""
        self.file_progress = None
        self.mode = item.get("mode") or "dictation"
        self.vocab = load_vocabulary(self.vocabulary_path)
        request = dict(item.get("request") or {})
        self._work(audio, item.get("recording", 0.0), item.get("context") or {"wm_class": "", "title": ""},
                   self.mode, request=request, recover=ident)
        return True

    def reprocess(self, row, mode):
        if self.state in ("listening", "transcribing", "polishing"):
            return False
        self.generation += 1
        self.mode = mode
        self.vocab = load_vocabulary(self.vocabulary_path)
        self._work(b"", 0.0, {"wm_class": row["app"], "title": row["title"]}, mode, row["raw"], row["id"],
                   request={"input_language": row.get("input_language", self._languages())})
        return True

    def _work(self, audio, recording, context, mode, raw=None, exclude_history_id=None, request=None, segment=False,
              recover=None):
        request = dict(request or {})
        generation = self.generation
        self.recovering = recover
        self.work_audio = (dict(audio=audio, recording=recording, context=dict(context), mode=mode, request=request)
                           if audio and raw is None and not recover else None)
        if not segment:
            self.set_state("polishing" if raw is not None else "transcribing")
        vocab = self.vocab
        provider = self.provider
        config = self.config

        def state(value):
            if not segment:
                GLib.idle_add(self._worker_state, generation, value)
        field = request.pop("field_context", None) if not recover else None
        if recover:
            request.pop("field_context", None)

        def worker():
            try:
                conversation = ""
                if field is not None and field.want == "conversation":
                    conversation = (field.get(3.0) or {}).get("conversation", "")
                result = run_pipeline(provider, audio, mode, context, vocab, recording,
                                      state, lambda: self.generation != generation, raw,
                                      history=self.history, learn_from_history=config.get("learn_from_history", False),
                                      understand_intent=config.get("understand_intent", True),
                                      exclude_history_id=exclude_history_id,
                                      operation=request.get("operation", "dictation"), language=request.get("language", ""),
                                      selection=request.get("selection", ""), editable=bool(request.get("snapshot")),
                                      input_language=request.get("input_language", self._languages()),
                                      default_language=self._languages(),
                                      allow_destinations=not segment and not recover, allow_voice_cancel=not recover,
                                      conversation=conversation, notes=bool(config.get("notes_folder")))
                if field is not None and field.want == "caret" and not segment and result.action == "deliver" and not result.error:
                    # Dictating mid-sentence: spacing and capitals from the text before the cursor.
                    from .context import fit_to_cursor
                    around = field.get(3.0)
                    if around:
                        # Only the paste changes; the history keeps the revised text.
                        result.cursor_around = around
                        result.cursor_text = fit_to_cursor(result.final, around.get("before"), around.get("after", ""),
                                                           vocab.terms, result.input_language)
                # Only a question about the selection, classified by the model, depends on
                # the AT-SPI confirmation; Edit was asked for explicitly.
                if result.action == "replace" and request.get("operation") == "ask":
                    from .selection import snapshot
                    if snapshot(request.get("pid"), request.get("selection")) != request.get("snapshot"):
                        result.action = "card"
                GLib.idle_add(self._complete, generation, result, audio, context, raw is None and not recover, segment)
            except VoiceCancelled:
                GLib.idle_add(self._worker_cancelled, generation, "voice-cancel")
            except Cancelled:
                GLib.idle_add(self._worker_cancelled, generation)
            except ProviderError as exc:
                GLib.idle_add(self._worker_error, generation, str(exc))
            except Exception:  # noqa: BLE001
                GLib.idle_add(self._worker_error, generation, _("Could not finish the dictation. Try again."))
        threading.Thread(target=worker, name="orfeuscribe-dictation", daemon=True).start()

    def _worker_state(self, generation, state):
        if generation == self.generation:
            self.set_state(state)
        return GLib.SOURCE_REMOVE

    def _worker_cancelled(self, generation, reason="cancelled"):
        if generation == self.generation:
            self.cancel(reason)
        return GLib.SOURCE_REMOVE

    def _worker_error(self, generation, message):
        if generation == self.generation:
            self.fail(message)
        return GLib.SOURCE_REMOVE

    def _complete(self, generation, result, audio, context, deliver, segment=False):
        if generation != self.generation:
            return GLib.SOURCE_REMOVE
        if deliver and result.action == "note" and not result.error:
            from .notes import append_folder_note
            try:
                append_folder_note(result.final, context, result.note_folder, self.config["note_subfolders"], self.vocab,
                                   self.config["notes_folder"], wikilinks=self.config.get("notes_wikilinks", False))
            except (OSError, ValueError) as exc:
                result.error = str(exc) if isinstance(exc, ValueError) else _("Could not add the note. The text is in the history.")
        values = result.as_dict()
        recovered, work = self.recovering, self.work_audio
        if not segment:
            self.recovering = self.work_audio = None
        try:
            ident = self.history.add(app=context["wm_class"], title=context["title"], **values)
            if audio:
                self.history.retain_audio(ident, audio, self.config["audio_keep"])
            self.history.prune_days(self.config.get("history_days", 0))
            self.last_record = self.history.get(ident)
            self.last_result = result
        except (OSError, sqlite3.Error):
            self.fail(_("Could not save the dictation in the history. Check the free disk space."))
            return GLib.SOURCE_REMOVE
        if result.error:
            # The revision failed: the transcript is in the entry, and the audio goes to
            # rescue so transcription and revision can be redone.
            rescued = recovered
            if recovered:
                self.rescue.update(recovered, history_id=ident, message=result.error[:300])
            elif work:
                rescued = self._rescue_audio(work["audio"], "error", result.error, work["context"], work["mode"],
                                             work["request"], work["recording"], ident)
            self.work_audio = None
            self.fail(result.error, rescued)
        else:
            if recovered:
                self.rescue.remove(recovered)
                self.emit_bus("Notice", "(s)", _("Recovered: the entry is in the history"))
            elif segment:
                self.work_audio = None
            if not segment:
                self.set_state("done")
            if deliver:
                if result.action == "note":
                    self.emit_bus("Notice", "(s)", _("Added to your notes"))
                elif result.action == "card":
                    self.emit_bus("Card", "(s)", json.dumps({"text": result.final, "web": result.web}, ensure_ascii=False))
                elif result.action == "replace":
                    self.emit_bus("Replace", "(ss)", result.final, context.get("selection", ""))
                elif context.get('wm_class') == APP_ID:
                    self.emit_bus('Notice', '(s)', _('Saved to history'))
                else:
                    prefix = '\n\n' if segment and self.segment_count else ''
                    delivered = getattr(result, 'cursor_text', None) or result.final
                    self._offer_raw(ident, result, prefix, delivered)
                    self.emit_bus("Deliver", "(s)", prefix + delivered)
            if segment:
                self.segment_tail = result.final[-1200:]
                self.recording_request["input_language"] = result.input_language
                self.segment_count += 1
                self.segment_active = False
                # Let the paste and clipboard restore finish before the next part.
                GLib.timeout_add(700, self._next_segment, generation)
                self.notify()
            else:
                self.idle_timer = GLib.timeout_add(1800, self._settle)
        return GLib.SOURCE_REMOVE

    def _offer_raw(self, ident, result, prefix='', delivered=None):
        """Before delivery, tells the extension what the raw speech of that entry is."""
        self.revertible = None
        if result.mode not in REVERTIBLE_MODES or result.action != 'deliver':
            return
        from .pipeline import literal_local
        mode = result.mode if result.mode != 'translation' else 'dictation'
        raw = literal_local(result.raw, mode, self.vocab, (result.input_language, self._languages()),
                            notes=bool(self.config.get("notes_folder")))
        if not raw.strip():
            return
        around = getattr(result, 'cursor_around', None)
        if around:
            from .context import fit_to_cursor
            raw = fit_to_cursor(raw, around.get('before'), around.get('after', ''), self.vocab.terms, result.input_language)
        self.revertible = {'id': ident, 'raw': raw, 'at': time.monotonic()}
        self.emit_bus('Revertible', '(s)', json.dumps({'id': ident, 'raw': prefix + raw,
                                                       'final': prefix + (delivered or result.final)}, ensure_ascii=False))

    def revert_to_raw(self, ident):
        """The extension replaced the pasted text with the transcript: record it as an edit."""
        offer = self.revertible
        if not offer or offer['id'] != ident or time.monotonic() - offer['at'] > REVERT_TTL:
            return False
        self.revertible = None
        try:
            self.history.revert_to_raw(ident, offer['raw'])
        except (OSError, ValueError, sqlite3.Error):
            return False
        if self.last_record and self.last_record.get('id') == ident:
            self.last_record = self.history.get(ident)
        self.notify()
        return True

    def _queue_segment(self, audio):
        now = time.monotonic()
        self.segment_queue.append((audio, now - self.segment_start))
        self.segment_start = now
        # If the network cannot keep up with the speech, stop recording and drain the queue.
        if len(self.segment_queue) >= 3 and self.state == 'listening':
            self.stop()
            return
        self._next_segment(self.generation)

    def _next_segment(self, generation):
        if generation != self.generation or self.segment_active or not self.segmented:
            return GLib.SOURCE_REMOVE
        if self.segment_queue:
            audio, recording = self.segment_queue.pop(0)
            self.segment_active = True
            context = dict(self.recording_context, previous_tail=self.segment_tail)
            self._work(audio, recording, context, self.mode, request=self.recording_request, segment=True)
        elif self.state != 'listening':
            self.segmented = False
            self.set_state('done')
            self.idle_timer = GLib.timeout_add(1800, self._settle)
        return GLib.SOURCE_REMOVE

    def _retention(self):
        try:
            self.history.prune_days(self.config['history_days'])
            self._prune_rescue()
            self.reload_history()
        except (OSError, sqlite3.Error):
            self.last_error = _('Could not apply the history retention. Check the free disk space.')
        return GLib.SOURCE_CONTINUE

    def reload_history(self):
        rows = self.history.search(limit=1)
        self.last_record = rows[0] if rows else None
        self.notify()

    def transcribe_file(self, path, mode=None, input_language=None):
        if self.state in ('listening', 'transcribing', 'polishing'):
            if self.window:
                self.window.toast(_('Finish or cancel the current work first.'))
            return False
        from pathlib import Path
        from .media import EXTENSIONS, transcribe_file
        path = Path(path)
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            if self.window:
                self.window.toast(_('Choose an audio or video file (MP4, MKV, WebM, WAV, MP3, M4A, OGG or FLAC).'))
            return False
        self.generation += 1
        generation = self.generation
        self.last_error = ''
        modes = available_modes(self.config)
        self.mode = mode if mode in modes else choose_mode('File', path.name, rules=self.config['rules'],
            fixed=self.settings.get_string('fixed-mode'), available=modes)
        from .languages import choose_language
        language = input_language or choose_language({'wm_class': 'File', 'title': path.name},
            self.config['language_rules'], self._languages())
        self.file_progress = {'name': path.name, 'stage': 'opening', 'number': 0, 'position': 0, 'duration': 0}
        self.set_state('transcribing')
        if self.window:
            self.window.show_file_progress()
        self.vocab = load_vocabulary(self.vocabulary_path)
        provider = self.provider

        def progress(stage, number, position, duration):
            GLib.idle_add(self._file_update, generation, stage, number, position, duration)

        def worker():
            try:
                result = transcribe_file(provider, path, self.mode, self.vocab,
                    input_language=language, history=self.history,
                    learn_from_history=self.config['learn_from_history'],
                    understand_intent=self.config['understand_intent'],
                    cancelled=lambda: generation != self.generation, progress=progress)
                GLib.idle_add(self._file_done, generation, result, path.name)
            except Cancelled:
                pass
            except ProviderError as exc:
                GLib.idle_add(self._file_error, generation, str(exc))
            except Exception:  # noqa: BLE001
                GLib.idle_add(self._file_error, generation)
        threading.Thread(target=worker, name='orfeuscribe-file', daemon=True).start()
        return True

    def _file_update(self, generation, stage, number, position, duration):
        if generation == self.generation:
            self.file_progress.update(stage=stage, number=number, position=position, duration=duration)
            self.notify()
        return GLib.SOURCE_REMOVE

    def _file_error(self, generation, message=None):
        if generation == self.generation:
            self.file_progress['stage'] = 'error'
            self.fail(message or _('Could not transcribe the file. Check the format and the connection, then try again.'))
        return GLib.SOURCE_REMOVE

    def _file_done(self, generation, result, name):
        if generation == self.generation:
            self.file_progress['stage'] = 'done'
            self._complete(generation, result, b'', {'wm_class': 'File', 'title': name}, False)
        return GLib.SOURCE_REMOVE

    def _settle(self):
        self.idle_timer = 0
        self.set_state("idle")
        return GLib.SOURCE_REMOVE

    def do_shutdown(self):
        GLib.source_remove(self.retention_timer)
        self.watcher.close()
        self.connection.signal_unsubscribe(self.proof_signal)
        self.cancel()
        self.connection.unregister_object(self.registration)
        Gio.bus_unown_name(self.bus_owner)
        Adw.Application.do_shutdown(self)
