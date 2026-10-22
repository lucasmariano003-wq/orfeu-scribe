"""Main window: Dictation, History, Vocabulary and Settings, with native libadwaita widgets."""
from __future__ import annotations

from datetime import datetime
import sqlite3
import threading
from pathlib import Path
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import __version__, languages as langs, modes as modes_module
from .config import ROOT, valid_endpoint, valid_pattern, write_private
from .history import History
from .i18n import N_, _, ngettext
from .ids import APP_ID, APP_NAME, EXTENSION_UUID
from .providers import ProviderError
from .vocabulary import load_vocabulary

# GSettings keys of the extension's shortcuts, in the order shown. None is set by default.
SHORTCUTS = (
    ("toggle-shortcut", N_("Dictate"), N_("Start and finish a dictation in any app.")),
    ("ask-accelerator", N_("Ask"), N_("Ask a question; the answer appears in a card.")),
    ("translate-accelerator", N_("Translate"), N_("Dictate and paste the translation.")),
    ("edit-shortcut", N_("Edit selection or write"), N_("With a selection: “make it shorter”. Without: “write a…”.")),
    ("reply-shortcut", N_("Reply with context"), N_("Only when the option is on under Privacy.")),
    ("raw-shortcut", N_("Revert to transcript"), N_("Up to 10 s after a paste.")),
    ("paste-accelerator", N_("Paste last dictation"), N_("Pastes the last result again.")),
)
STATE_TITLES = {"idle": N_("Ready"), "listening": N_("Listening…"), "transcribing": N_("Transcribing…"),
                "polishing": N_("Revising…"), "done": N_("Done"), "error": N_("Something went wrong")}
FILE_STAGES = {'opening': N_('Opening the file…'), 'transcribing': N_('Transcribing'), 'polishing': N_('Revising'),
               'part_done': N_('Part kept in memory'), 'done': N_('Transcription saved to history'),
               'cancelled': N_('Transcription cancelled'), 'error': N_('Could not finish')}
HINTS = (("auto", N_("Automatic")), ("keywords", N_("Keywords field (gpt-transcribe)")),
         ("prompt", N_("Prompt field (Whisper and compatible servers)")), ("none", N_("Don't send hints")))
# System shortcut schemas checked for conflicts when a shortcut is chosen.
SYSTEM_SHORTCUT_SCHEMAS = ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
                           "org.gnome.mutter.keybindings", "org.gnome.settings-daemon.plugins.media-keys")


def label(text="", css=(), xalign=0.0, wrap=False, selectable=False):
    widget = Gtk.Label(label=text, xalign=xalign, wrap=wrap, selectable=selectable)
    if wrap:
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for name in css:
        widget.add_css_class(name)
    return widget


def button(text, callback, css=(), icon=None, tooltip=None):
    widget = Gtk.Button(label=text) if text else Gtk.Button(icon_name=icon)
    for name in css:
        widget.add_css_class(name)
    if tooltip:
        widget.set_tooltip_text(tooltip)
        widget.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    widget.set_valign(Gtk.Align.CENTER)
    widget.connect("clicked", callback)
    return widget


def plain_row(cls, **kwargs):
    """Rows that show user text must never interpret it as Pango markup."""
    row = cls(**kwargs)
    row.set_use_markup(False)
    return row


def clamp(child, width=720):
    content = Adw.Clamp(maximum_size=width, tightening_threshold=width - 120, child=child)
    content.set_margin_top(24)
    content.set_margin_bottom(32)
    content.set_margin_start(12)
    content.set_margin_end(12)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    scroll.set_child(content)
    return scroll


def clear(container):
    child = container.get_first_child()
    while child:
        following = child.get_next_sibling()
        container.remove(child)
        child = following


def seconds(value):
    return _("{value:.1f} s").format(value=value)


class OrfeuScribeWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=960, default_height=780)
        self.set_size_request(360, 520)
        self.app = app
        self.edit_drafts = {}
        self.rule_widgets = []
        self._syncing = False
        display = self.get_display()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(ROOT / "data/icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_path(str(ROOT / "data/style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        view = Adw.ToolbarView()
        self.toast_overlay.set_child(view)
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.switcher)
        header.pack_end(self._menu_button())
        view.add_top_bar(header)
        self.banner = Adw.Banner(title=_("No speech service is set up yet."), button_label=_("Set Up"))
        self.banner.connect("button-clicked", lambda *_args: self.show_settings())
        view.add_top_bar(self.banner)
        view.set_content(self.stack)
        switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(switcher_bar)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.add_setter(switcher_bar, "reveal", True)
        narrow.add_setter(self.switcher, "visible", False)
        self.add_breakpoint(narrow)

        self.stack.add_titled_with_icon(self._dictation_page(), "dictation", _("Dictation"),
                                        "audio-input-microphone-symbolic")
        self.stack.add_titled_with_icon(self._history_page(), "history", _("History"), "document-open-recent-symbolic")
        self.stack.add_titled_with_icon(self._vocabulary_page(), "vocabulary", _("Vocabulary"),
                                        "accessories-dictionary-symbolic")
        self.stack.add_titled_with_icon(self._settings_page(), "settings", _("Settings"), "emblem-system-symbolic")
        self.stack.connect("notify::visible-child-name", self._navigate)
        self.connect("close-request", self._close)
        app.listeners.append(self.refresh)
        app.settings.connect("changed", lambda *_args: self.refresh())
        self._actions()
        self.refresh()
        self.refresh_suggestions()
        self.check_extension()

    # --- frame -----------------------------------------------------------------
    def _menu_button(self):
        menu = Gio.Menu()
        files = Gio.Menu()
        files.append(_("Transcribe a File…"), "win.transcribe-file")
        files.append(_("Export History…"), "win.export-history")
        files.append(_("Import History…"), "win.import-history")
        menu.append_section(None, files)
        about = Gio.Menu()
        about.append(_("About Orfeu Scribe"), "win.about")
        menu.append_section(None, about)
        widget = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True)
        widget.set_tooltip_text(_("Main Menu"))
        return widget

    def _actions(self):
        for name, callback in (("transcribe-file", self._choose_audio), ("export-history", self._export_dialog),
                               ("import-history", self._choose_import), ("about", self._about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    def _about(self, *_args):
        dialog = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID, version=__version__,
                                 developer_name=_("Orfeu Scribe contributors"),
                                 comments=_("Voice dictation for GNOME with a speech service you choose."),
                                 license_type=Gtk.License.GPL_3_0)
        dialog.add_acknowledgement_section(_("Derived from"), ["Calamus"])
        dialog.present(self)

    def _close(self, *_args):
        self.set_visible(False)
        return True

    def _navigate(self, *_args):
        name = self.stack.get_visible_child_name()
        if name == "history":
            self.refresh_history()
        elif name == "vocabulary":
            self.refresh_suggestions()

    def show_settings(self):
        self.stack.set_visible_child_name("settings")

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=4, use_markup=False))

    def copy(self, text):
        if text:
            self.get_clipboard().set(text)
            self.toast(_("Copied to the clipboard."))

    # --- Dictation ---------------------------------------------------------------
    def _dictation_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.setup_status = Adw.StatusPage(icon_name="network-server-symbolic",
                                           title=_("Set up a speech service"),
                                           description=_("Orfeu Scribe sends your recorded audio to a speech-to-text "
                                                         "service you choose. Nothing is sent until you set one up."))
        self.setup_status.add_css_class("compact")
        setup_button = Gtk.Button(label=_("Open Settings"), halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        setup_button.connect("clicked", lambda *_args: self.show_settings())
        self.setup_status.set_child(setup_button)
        page.append(self.setup_status)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        self.record_button = Gtk.Button(icon_name="audio-input-microphone-symbolic", halign=Gtk.Align.CENTER,
                                        css_classes=["circular", "suggested-action", "record-button"])
        self.record_button.connect("clicked", lambda *_args: self.app.dictate_from_window())
        hero.append(self.record_button)
        self.status_title = label("", ("title-1",), 0.5)
        hero.append(self.status_title)
        self.status_detail = label("", ("dim-label",), 0.5, wrap=True)
        hero.append(self.status_detail)
        self.level = Gtk.LevelBar(min_value=0, max_value=1, value=0, width_request=280, halign=Gtk.Align.CENTER)
        self.level.update_property([Gtk.AccessibleProperty.LABEL], [_("Voice level")])
        hero.append(self.level)
        self.live_label = label("", ("dim-label",), 0.5, wrap=True)
        self.live_label.set_max_width_chars(60)
        hero.append(self.live_label)
        self.cancel_button = Gtk.Button(label=_("Cancel"), halign=Gtk.Align.CENTER, css_classes=["pill"])
        self.cancel_button.connect("clicked", lambda *_args: self.app.cancel())
        hero.append(self.cancel_button)
        shortcut = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        shortcut.append(label(_("Shortcut:"), ("dim-label",)))
        self.dictate_shortcut = Adw.ShortcutLabel(disabled_text=_("not set"))
        shortcut.append(self.dictate_shortcut)
        self.shortcut_button = Gtk.Button(label=_("Choose…"), css_classes=["flat"])
        self.shortcut_button.connect("clicked", lambda *_args: self._capture_shortcut("toggle-shortcut", _("Dictate")))
        shortcut.append(self.shortcut_button)
        hero.append(shortcut)
        page.append(hero)

        self.error_banner = Adw.Banner(revealed=False, button_label=_("Dismiss"))
        self.error_banner.add_css_class("error")
        self.error_banner.connect("button-clicked", self._dismiss_error)
        page.append(self.error_banner)

        self.last_group = Adw.PreferencesGroup(title=_("Last Result"))
        copy = button("", lambda *_args: self.copy(History.final_text(self.app.last_record)) if self.app.last_record else None,
                      ("flat",), "edit-copy-symbolic", _("Copy"))
        self.copy_last = copy
        self.last_group.set_header_suffix(copy)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["card", "result-card"])
        self.last_text = label("", (), wrap=True, selectable=True)
        self.last_text.set_lines(8)
        self.last_text.set_ellipsize(Pango.EllipsizeMode.END)
        card.append(self.last_text)
        self.last_meta = label("", ("dim-label", "caption"), wrap=True)
        card.append(self.last_meta)
        self.last_group.add(card)
        page.append(self.last_group)

        self.extension_group = Adw.PreferencesGroup()
        self.extension_row = Adw.ActionRow(title=_("GNOME Shell extension"), subtitle=_("Checking…"))
        self.extension_row.add_prefix(Gtk.Image(icon_name="application-x-addon-symbolic"))
        self.extension_group.add(self.extension_row)
        page.append(self.extension_group)
        return clamp(page, 640)

    def _dismiss_error(self, *_args):
        self.app.last_error = ""
        self.refresh()

    def set_level(self, value):
        self.level.set_value(value)

    # --- History -----------------------------------------------------------------
    def _history_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        toolbar = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_("Search text or app"), hexpand=True)
        self.search.connect("search-changed", lambda *_args: self.refresh_history())
        toolbar.append(self.search)
        self.history_filter = Gtk.DropDown.new_from_strings([_("All"), _("Dictations"), _("Questions")])
        self.history_filter.update_property([Gtk.AccessibleProperty.LABEL], [_("Filter")])
        self.history_filter.connect("notify::selected", lambda *_args: self.refresh_history())
        toolbar.append(self.history_filter)
        page.append(toolbar)

        self.file_group = Adw.PreferencesGroup(title=_("Transcribing a File"))
        self.file_row = plain_row(Adw.ActionRow, title="", subtitle="")
        self.file_cancel = button(_("Cancel"), lambda *_args: self.app.cancel(), ("flat",))
        self.file_row.add_suffix(self.file_cancel)
        self.file_group.add(self.file_row)
        self.file_meter = Gtk.ProgressBar(margin_top=8)
        self.file_meter.update_property([Gtk.AccessibleProperty.LABEL], [_("Transcription progress")])
        self.file_group.add(self.file_meter)
        page.append(self.file_group)

        self.rescue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(self.rescue_box)
        self.history_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(self.history_box)
        self.history_empty = Adw.StatusPage(icon_name="document-open-recent-symbolic", vexpand=True)
        page.append(self.history_empty)
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect('drop', self._drop_audio)
        page.add_controller(drop)
        return clamp(page, 820)

    def refresh_rescue(self):
        clear(self.rescue_box)
        store = getattr(self.app, "rescue", None)
        items = store.items() if store else []
        if not items:
            return
        from .rescue import TTL, reason_label
        group = Adw.PreferencesGroup(title=_("Waiting for Recovery"),
                                     description=_("The audio of these dictations is kept for 24 hours in a private "
                                                   "folder. Recover transcribes and revises it again."))
        for item in items:
            when = datetime.fromtimestamp(item["created"]).strftime("%x %H:%M")
            until = datetime.fromtimestamp(item["created"] + TTL).strftime("%x %H:%M")
            length = max(0, (item.get("bytes", 0) - 44) / 32000)
            app_name = item.get("context", {}).get("wm_class") or APP_NAME
            row = plain_row(Adw.ActionRow, title=f"{reason_label(item['reason'])} · {when}",
                            subtitle=(item.get("message") or "") + ("\n" if item.get("message") else "") +
                            _("{app} · {length:.0f} s of audio · kept until {until}").format(
                                app=app_name, length=length, until=until))
            row.set_subtitle_lines(3)
            row.add_suffix(button(_("Recover"), lambda _b, i=item["id"]: self._recover(i), ("suggested-action",)))
            row.add_suffix(button("", lambda _b, i=item["id"]: self._discard_rescue(i), ("flat",),
                                  "user-trash-symbolic", _("Discard")))
            group.add(row)
        self.rescue_box.append(group)

    def _recover(self, ident):
        if self.app.recover(ident):
            self.toast(_("Transcribing the kept audio again. The entry will appear in the history."))
        else:
            self.toast(_("Finish the current work first, or the audio has expired."))
        self.refresh_history()

    def _discard_rescue(self, ident):
        self.app.rescue.remove(ident)
        self.refresh_history()

    def refresh_history(self):
        self.refresh_rescue()
        clear(self.history_box)
        rescues = {item.get("history_id"): item["id"] for item in
                   (self.app.rescue.items() if getattr(self.app, "rescue", None) else []) if item.get("history_id")}
        category = ["all", "dictations", "questions"][self.history_filter.get_selected()]
        rows = self.app.history.search(self.search.get_text(), category=category)
        searching = bool(self.search.get_text().strip()) or category != "all"
        self.history_empty.set_visible(not rows)
        self.history_empty.set_title(_("No matching entries") if searching else _("No dictations yet"))
        self.history_empty.set_description(_("Try other words or another filter.") if searching else
                                           _("Each dictation is saved here with its transcript and final text."))
        if not rows:
            return
        group = Adw.PreferencesGroup()
        for row in rows:
            group.add(self._history_row(row, rescues.get(row["id"])))
        self.history_box.append(group)

    def _history_row(self, row, rescue_id=None):
        final = History.final_text(row)
        first_line = (final or row["raw"] or "").strip().splitlines()[0] if (final or row["raw"] or "").strip() else "—"
        when = datetime.fromisoformat(row["created_at"]).astimezone().strftime("%x %H:%M")
        parts = [when, modes_module.title(row["mode"]) + (f" → {row['language']}" if row.get("language") else "")]
        if row.get("app"):
            parts.append(row["app"])
        if row["edited"] is not None:
            parts.append(_("reverted to transcript") if row.get("action") == "raw" else _("edited"))
        expander = plain_row(Adw.ExpanderRow, title=first_line, subtitle=" · ".join(parts))
        expander.set_title_lines(1)
        if row.get("error"):
            expander.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text=row["error"]))
        expander.add_suffix(button("", lambda _b, r=row: self.copy(History.final_text(r) or r["raw"]), ("flat",),
                                   "edit-copy-symbolic", _("Copy")))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12,
                       margin_start=12, margin_end=12)
        editing = row["id"] in self.edit_drafts
        if editing:
            editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8, left_margin=8,
                                  right_margin=8, css_classes=["card"])
            editor.update_property([Gtk.AccessibleProperty.LABEL], [_("Edit text")])
            buffer = editor.get_buffer()
            buffer.set_text(self.edit_drafts[row["id"]])
            buffer.connect("changed", lambda b, ident=row["id"]: self.edit_drafts.__setitem__(
                ident, b.get_text(b.get_start_iter(), b.get_end_iter(), True)))
            scroll = Gtk.ScrolledWindow(min_content_height=140, max_content_height=320, propagate_natural_height=True)
            scroll.set_child(editor)
            body.append(scroll)
            actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
            actions.append(button(_("Cancel"), lambda _b, ident=row["id"]: self._cancel_amendment(ident)))
            actions.append(button(_("Save"), lambda _b, r=row: self._save_amendment(r), ("suggested-action",)))
            body.append(actions)
        else:
            body.append(label(final or _("The revision did not finish. The transcript is kept below."),
                              (), wrap=True, selectable=True))
        if row.get("error"):
            body.append(label(row["error"], ("error",), wrap=True))
        if row["edited"] is not None:
            original = Gtk.Expander(label=_("Original revision"))
            original.set_child(label(row["final"] or "—", ("dim-label",), wrap=True, selectable=True))
            body.append(original)
        raw = Gtk.Expander(label=_("Transcript"))
        raw.set_child(label(row["raw"], ("dim-label",), wrap=True, selectable=True))
        body.append(raw)
        body.append(label(_("{service} · {model} · transcription {stt} · revision {revision}").format(
            service=row["provider"] or "—", model=row["model"] or "—", stt=seconds(row["stt"]),
            revision=seconds(row["polishing"])), ("dim-label", "caption"), wrap=True))
        actions = Adw.WrapBox(child_spacing=8, line_spacing=8)
        actions.append(button(_("Copy Transcript"), lambda _b, r=row: self.copy(r["raw"])))
        edit = button(_("Edit"), lambda _b, r=row: self._start_amendment(r))
        edit.set_sensitive(not editing)
        actions.append(edit)
        available = modes_module.available(self.app.config)
        if row["mode"] in modes_module.MODES:
            choice = Gtk.DropDown.new_from_strings([modes_module.title(mode) for mode in available])
            choice.update_property([Gtk.AccessibleProperty.LABEL], [_("Mode")])
            if row["mode"] in available:
                choice.set_selected(available.index(row["mode"]))
            rewrite = Gtk.Box(spacing=0, css_classes=["linked"])
            rewrite.append(choice)
            rewrite.append(button(_("Rewrite"), lambda _b, r=row, c=choice: self._reprocess(r, available[c.get_selected()])))
            actions.append(rewrite)
        if rescue_id:
            actions.append(button(_("Recover From Audio"), lambda _b, i=rescue_id: self._recover(i)))
        actions.append(button(_("Delete"), lambda _b, r=row: self._delete_dialog(r), ("destructive-action",)))
        body.append(actions)
        expander.add_row(body)
        expander.set_expanded(editing)
        return expander

    def _start_amendment(self, row):
        self.edit_drafts[row['id']] = History.final_text(row) or row['raw']
        self.refresh_history()

    def _cancel_amendment(self, ident):
        self.edit_drafts.pop(ident, None)
        self.refresh_history()

    def _save_amendment(self, row):
        try:
            self.app.history.amend(row['id'], self.edit_drafts[row['id']])
        except ValueError as exc:
            self.toast(str(exc))
            return
        except (OSError, sqlite3.Error):
            self.toast(_("Could not save the edit. Your text is still open."))
            return
        self.edit_drafts.pop(row['id'], None)
        if self.app.last_record and self.app.last_record['id'] == row['id']:
            self.app.last_record = self.app.history.get(row['id'])
        self.refresh_history()
        self.refresh_suggestions()
        self.refresh()
        self.toast(_("Edit saved."))

    def _reprocess(self, row, mode):
        if self.app.reprocess(row, mode):
            self.toast(_("Revising again. A new entry will be saved in the history."))
        else:
            self.toast(_("Finish or cancel the current dictation first."))

    def _delete_dialog(self, row):
        dialog = Adw.AlertDialog(heading=_("Delete this entry?"),
                                 body=_("Deletes the text, any kept audio and its contribution to suggestions. "
                                        "Earlier exports are not affected."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def response(_dialog, choice):
            if choice != "delete":
                return
            try:
                self.app.history.delete(row['id'], row['created_at'])
                self.edit_drafts.pop(row['id'], None)
                self.app.reload_history()
                self.refresh_history()
                self.refresh_suggestions()
                self.toast(_("Entry deleted."))
            except (OSError, sqlite3.Error):
                self.toast(_("Could not delete the entry. Check access to the history."))
        dialog.connect("response", response)
        self.last_dialog = dialog
        dialog.present(self)

    def show_file_progress(self):
        self.stack.set_visible_child_name("history")
        self.refresh()

    def _drop_audio(self, _target, file, _x, _y):
        path = file.get_path()
        return bool(path and self.app.transcribe_file(path))

    def _choose_audio(self, *_args):
        dialog = Gtk.FileDialog(title=_("Transcribe an Audio or Video File"))

        def chosen(source, result):
            try:
                path = source.open_finish(result).get_path()
                if path:
                    self.app.transcribe_file(path)
            except GLib.Error:
                pass
        dialog.open(self, None, chosen)

    def _export_dialog(self, *_args):
        dialog = Adw.AlertDialog(heading=_("Export History"),
                                 body=_("The copy is created with private permissions and can be imported back."))
        controls = Adw.PreferencesGroup()
        scope = Adw.ComboRow(title=_("Entries"), model=Gtk.StringList.new([_("All entries"), _("Current search and filter")]))
        format_choice = Adw.ComboRow(title=_("Format"), model=Gtk.StringList.new(["Markdown (.md)", "CSV (.csv)"]))
        controls.add(scope)
        controls.add(format_choice)
        dialog.set_extra_child(controls)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("export", _("Choose File…"))
        dialog.set_response_appearance("export", Adw.ResponseAppearance.SUGGESTED)

        def response(_dialog, choice):
            if choice != "export":
                return
            suffix = ".md" if format_choice.get_selected() == 0 else ".csv"
            query = self.search.get_text() if scope.get_selected() else ""
            category = ["all", "dictations", "questions"][self.history_filter.get_selected()] if scope.get_selected() else "all"
            picker = Gtk.FileDialog(title=_("Save History Copy"), initial_name=_("Orfeu Scribe history") + suffix)

            def save(source, result):
                try:
                    path = Path(source.save_finish(result).get_path())
                    from .exchange import export_history
                    if path.suffix.lower() != suffix:
                        path = Path(str(path) + suffix)
                    count = export_history(self.app.history, path, query, category)
                    self.toast(ngettext("{count} entry exported.", "{count} entries exported.", count).format(count=count))
                except GLib.Error:
                    pass
                except (OSError, ValueError, sqlite3.Error):
                    self.toast(_("Could not save the copy."))
            picker.save(self, None, save)
        dialog.connect("response", response)
        self.last_dialog = dialog
        dialog.present(self)

    def _choose_import(self, *_args):
        picker = Gtk.FileDialog(title=_("Import a Orfeu Scribe Export (.md or .csv)"))

        def chosen(source, result):
            try:
                path = source.open_finish(result).get_path()
                from .exchange import import_history
                count = import_history(self.app.history, path)
                self.app.reload_history()
                self.refresh_history()
                self.toast(ngettext("{count} entry added.", "{count} entries added.", count).format(count=count))
            except GLib.Error:
                pass
            except (OSError, ValueError, TypeError, sqlite3.Error):
                self.toast(_("Could not import. Choose a file exported by Orfeu Scribe."))
        picker.open(self, None, chosen)

    # --- Vocabulary --------------------------------------------------------------
    def _vocabulary_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.suggestions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(self.suggestions_box)
        group = Adw.PreferencesGroup(title=_("Vocabulary"),
                                     description=_("Names and terms you use, one per line. To fix a recurring "
                                                   "misspelling, write: wrong => right. Terms are sent to the "
                                                   "speech and revision services as spelling hints."))
        self.vocab_editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12, bottom_margin=12,
                                         left_margin=12, right_margin=12, monospace=True)
        self.vocab_editor.update_property([Gtk.AccessibleProperty.LABEL], [_("Vocabulary")])
        try:
            text = self.app.vocabulary_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        self.vocab_editor.get_buffer().set_text(text)
        self.vocab_editor.get_buffer().set_modified(False)
        frame = Gtk.ScrolledWindow(min_content_height=300, css_classes=["card", "vocabulary-sheet"])
        frame.set_child(self.vocab_editor)
        group.add(frame)
        page.append(group)
        actions = Gtk.Box(spacing=12)
        self.vocab_count = label("", ("dim-label",), wrap=True)
        self.vocab_count.set_hexpand(True)
        actions.append(self.vocab_count)
        actions.append(button(_("Save"), self._save_vocabulary, ("suggested-action", "pill")))
        page.append(actions)
        self._vocab_count()
        return clamp(page, 720)

    def _vocab_count(self):
        count = len(self.app.vocab.terms)
        self.vocab_count.set_text(ngettext("{count} term saved.", "{count} terms saved.", count).format(count=count)
                                  if count else _("The vocabulary is empty."))

    def refresh_suggestions(self):
        clear(self.suggestions_box)
        suggestions = self.app.history.pending_suggestions()
        if not suggestions:
            return
        group = Adw.PreferencesGroup(title=_("Suggestions"),
                                     description=_("Swaps that repeated in your edits. They only enter the "
                                                   "vocabulary if you accept them."))
        for item in suggestions:
            row = plain_row(Adw.ActionRow, title=f"{item['wrong']} → {item['right']}",
                            subtitle=ngettext("In {count} edited entry", "In {count} edited entries",
                                              item["count"]).format(count=item["count"]))
            row.add_suffix(button(_("Reject"), lambda _b, ident=item['id']: self._reject_suggestion(ident), ("flat",)))
            row.add_suffix(button(_("Accept"), lambda _b, ident=item['id']: self._accept_suggestion(ident),
                                  ("suggested-action",)))
            group.add(row)
        self.suggestions_box.append(group)

    def _accept_suggestion(self, ident):
        if self.vocab_editor.get_buffer().get_modified():
            self.toast(_("Save your vocabulary changes before accepting a suggestion."))
            return
        try:
            accepted = self.app.history.accept_suggestion(ident, self.app.vocabulary_path)
            self.app.vocab = load_vocabulary(self.app.vocabulary_path)
            self.vocab_editor.get_buffer().set_text(self.app.vocabulary_path.read_text(encoding="utf-8"))
            self.vocab_editor.get_buffer().set_modified(False)
        except (OSError, sqlite3.Error):
            self.toast(_("Could not save. Try again."))
            return
        self.refresh_suggestions()
        self._vocab_count()
        self.toast(_("Spelling added to the vocabulary.") if accepted else _("This suggestion was already resolved."))

    def _reject_suggestion(self, ident):
        try:
            self.app.history.reject_suggestion(ident)
        except sqlite3.Error:
            self.toast(_("Could not save. Try again."))
            return
        self.refresh_suggestions()
        self.toast(_("Suggestion rejected. It will not be offered again."))

    def _save_vocabulary(self, *_args):
        buffer = self.vocab_editor.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        write_private(self.app.vocabulary_path, text.rstrip() + "\n" if text.strip() else "")
        buffer.set_modified(False)
        self.app.vocab = load_vocabulary(self.app.vocabulary_path)
        self._vocab_count()
        self.toast(_("Vocabulary saved."))

    # --- Settings ----------------------------------------------------------------
    def _settings_page(self):
        page = Adw.PreferencesPage()
        page.add(self._service_group())
        page.add(self._revision_group())
        page.add(self._shortcuts_group())
        page.add(self._modes_group())
        page.add(self._language_group())
        page.add(self._recording_group())
        page.add(self._privacy_group())
        page.add(self._notes_group())
        page.add(self._startup_group())
        return page

    def _set_config(self, key, value, reload=False):
        if self._syncing:
            return
        self.app.config[key] = value
        if reload:
            self.app.reload_provider()

    def _switch(self, title, subtitle, key, reload=False):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.app.config.get(key)))
        row.connect("notify::active", lambda widget, *_args: self._set_config(key, widget.get_active(), reload))
        return row

    def _service_group(self):
        group = Adw.PreferencesGroup(title=_("Speech-to-Text Service"),
                                     description=_("Recorded audio is sent to this server for transcription. Any "
                                                   "server with an OpenAI-compatible /audio/transcriptions endpoint "
                                                   "works; http:// is accepted only for this computer."))
        self.stt_url = Adw.EntryRow(title=_("Server URL"), text=self.app.config["stt_base_url"], show_apply_button=True)
        self.stt_url.connect("apply", lambda row: self._save_endpoint("stt_base_url", row))
        group.add(self.stt_url)
        self.stt_model = Adw.EntryRow(title=_("Model"), text=self.app.config["stt_model"], show_apply_button=True)
        self.stt_model.connect("apply", lambda row: self._save_text("stt_model", row))
        self.stt_model.add_suffix(button("", lambda *_args: self._fetch_models("stt"), ("flat",), "view-list-symbolic",
                                         _("List models")))
        group.add(self.stt_model)
        self.stt_key = Adw.PasswordEntryRow(title=_("API key"), show_apply_button=True)
        self.stt_key.connect("apply", lambda row: self._save_key("stt", row))
        group.add(self.stt_key)
        self.stt_key_status = Adw.ActionRow(title=_("Key"), subtitle=_("Checking…"))
        self.stt_key_clear = button(_("Remove"), lambda *_args: self._clear_key("stt"), ("flat",))
        self.stt_key_status.add_suffix(self.stt_key_clear)
        group.add(self.stt_key_status)
        self.hints = Adw.ComboRow(title=_("Vocabulary hints"),
                                  subtitle=_("How your vocabulary and language reach the speech service."),
                                  model=Gtk.StringList.new([_(name) for _value, name in HINTS]))
        self.hints.set_selected([value for value, _name in HINTS].index(self.app.config["stt_hints"]))
        self.hints.connect("notify::selected", lambda row, *_args: self._set_config(
            "stt_hints", HINTS[row.get_selected()][0], True))
        group.add(self.hints)
        preset = Adw.ButtonRow(title=_("Fill In OpenAI Defaults"), start_icon_name="document-edit-symbolic")
        preset.connect("activated", self._openai_preset)
        group.add(preset)
        self._key_status("stt")
        return group

    def _revision_group(self):
        group = Adw.PreferencesGroup(title=_("Text Revision"),
                                     description=_("A language model turns the transcript into clean text, using an "
                                                   "OpenAI-compatible /chat/completions endpoint. Without it, "
                                                   "Orfeu Scribe applies spoken commands, your vocabulary and "
                                                   "capitalization locally."))
        self.revision_switch = self._switch(_("Revise text with a language model"), "", "revision_enabled", True)
        group.add(self.revision_switch)
        self.same_switch = self._switch(_("Use the same server and key"), _("As set for speech-to-text above."),
                                        "revision_same_server", True)
        self.same_switch.connect("notify::active", lambda *_args: self._sync_revision())
        group.add(self.same_switch)
        self.revision_url = Adw.EntryRow(title=_("Server URL"), text=self.app.config["revision_base_url"],
                                         show_apply_button=True)
        self.revision_url.connect("apply", lambda row: self._save_endpoint("revision_base_url", row))
        group.add(self.revision_url)
        self.revision_key = Adw.PasswordEntryRow(title=_("API key"), show_apply_button=True)
        self.revision_key.connect("apply", lambda row: self._save_key("revision", row))
        group.add(self.revision_key)
        self.revision_key_status = Adw.ActionRow(title=_("Key"), subtitle="")
        self.revision_key_clear = button(_("Remove"), lambda *_args: self._clear_key("revision"), ("flat",))
        self.revision_key_status.add_suffix(self.revision_key_clear)
        group.add(self.revision_key_status)
        self.revision_model = Adw.EntryRow(title=_("Model"), text=self.app.config["revision_model"],
                                           show_apply_button=True)
        self.revision_model.connect("apply", lambda row: self._save_text("revision_model", row))
        self.revision_model.add_suffix(button("", lambda *_args: self._fetch_models("revision"), ("flat",),
                                              "view-list-symbolic", _("List models")))
        group.add(self.revision_model)
        group.add(self._switch(_("Understand intent"),
                               _("Organizes ideas, applies asides such as “make it more formal” and keeps only your "
                                 "final decision. Revised text, Message, Email and AI instruction modes."),
                               "understand_intent"))
        self._key_status("revision")
        self._sync_revision()
        return group

    def _sync_revision(self):
        enabled = self.app.config["revision_enabled"]
        separate = enabled and not self.app.config["revision_same_server"]
        self.same_switch.set_sensitive(enabled)
        for widget in (self.revision_url, self.revision_key, self.revision_key_status):
            widget.set_visible(separate)
        self.revision_model.set_sensitive(enabled)

    def _openai_preset(self, *_args):
        self.stt_url.set_text("https://api.openai.com/v1")
        self._save_endpoint("stt_base_url", self.stt_url)
        if not self.stt_model.get_text().strip():
            # Recommended in OpenAI's speech-to-text guide (checked October 2026).
            self.stt_model.set_text("gpt-transcribe")
            self._save_text("stt_model", self.stt_model)
        self.toast(_("OpenAI address filled in. Add your API key and choose a revision model."))

    def _save_endpoint(self, key, row):
        text = row.get_text().strip()
        if text and not valid_endpoint(text):
            self.toast(_("Use an https:// address (http:// only for this computer), without user, query or fragment."))
            return
        self.app.config[key] = text.rstrip("/")
        self.app.reload_provider()
        self.toast(_("Server saved."))

    def _save_text(self, key, row):
        self.app.config[key] = row.get_text().strip()
        self.app.reload_provider()
        self.toast(_("Model saved."))

    def _save_key(self, slot, row):
        value = row.get_text().strip()
        if not value:
            return
        row.set_text("")

        def work():
            try:
                self.app.credentials.store(slot, value)
                GLib.idle_add(self._key_saved, slot, "")
            except Exception as exc:  # noqa: BLE001 — SecretError carries a safe message
                GLib.idle_add(self._key_saved, slot, str(exc) or _("Could not save the key."))
        threading.Thread(target=work, daemon=True).start()

    def _key_saved(self, slot, error):
        self.toast(error or _("Key saved in the keyring."))
        self.app.reload_provider()
        self._key_status(slot)
        return GLib.SOURCE_REMOVE

    def _clear_key(self, slot):
        try:
            self.app.credentials.clear(slot)
            self.toast(_("Key removed from the keyring."))
        except Exception as exc:  # noqa: BLE001
            self.toast(str(exc))
        self._key_status(slot)

    def _key_status(self, slot):
        from .credentials import ENVIRONMENT, from_environment
        row = self.stt_key_status if slot == "stt" else self.revision_key_status
        clear_button = self.stt_key_clear if slot == "stt" else self.revision_key_clear
        if from_environment(slot):
            row.set_subtitle(_("Taken from the {name} environment variable.").format(name=ENVIRONMENT[slot]))
            clear_button.set_visible(False)
            return

        def work():
            try:
                found = bool(self.app.credentials.lookup(slot))
                text = _("Saved in the keyring.") if found else _("Not set. Local servers may not need one.")
            except Exception as exc:  # noqa: BLE001 — SecretError carries a safe message
                found, text = False, str(exc)

            def show():
                row.set_subtitle(text)
                clear_button.set_visible(found)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(show)
        threading.Thread(target=work, daemon=True).start()

    def _fetch_models(self, which):
        service = self.app.provider
        if which == "stt":
            client = service.transcriber if getattr(service, "configured", False) else None
        else:
            client = getattr(service, "reviser", None)
        if client is None:
            self.toast(_("Save the server URL and a model first."))
            return
        self.toast(_("Asking the server for its models…"))

        def work():
            try:
                GLib.idle_add(self._models_ready, which, client.list_models(), "")
            except ProviderError as exc:
                GLib.idle_add(self._models_ready, which, [], str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _models_ready(self, which, models, error):
        if error:
            self.toast(error)
            return GLib.SOURCE_REMOVE
        dialog = Adw.AlertDialog(heading=_("Choose a Model"), body=_("Models listed by the server."))
        choice = Gtk.DropDown.new_from_strings(models)
        dialog.set_extra_child(choice)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("choose", _("Choose"))
        dialog.set_response_appearance("choose", Adw.ResponseAppearance.SUGGESTED)

        def selected(_dialog, response):
            if response == "choose":
                row = self.stt_model if which == "stt" else self.revision_model
                row.set_text(models[choice.get_selected()])
                self._save_text("stt_model" if which == "stt" else "revision_model", row)
        dialog.connect("response", selected)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def _shortcuts_group(self):
        group = Adw.PreferencesGroup(title=_("Shortcuts"),
                                     description=_("Shortcuts work in any app through the GNOME Shell extension. "
                                                   "None is set by default, so Orfeu Scribe never takes a combination "
                                                   "that something else already uses."))
        self.shortcut_labels = {}
        for key, title, hint in SHORTCUTS:
            row = Adw.ActionRow(title=_(title), subtitle=_(hint))
            shortcut = Adw.ShortcutLabel(disabled_text=_("Not set"), valign=Gtk.Align.CENTER)
            self.shortcut_labels[key] = shortcut
            row.add_suffix(shortcut)
            row.add_suffix(button("", lambda _b, k=key, t=title: self._capture_shortcut(k, _(t)), ("flat",),
                                  "document-edit-symbolic", _("Set shortcut")))
            row.add_suffix(button("", lambda _b, k=key: self._set_shortcut(k, ""), ("flat",), "edit-clear-symbolic",
                                  _("Clear shortcut")))
            group.add(row)
        behavior = Adw.ComboRow(title=_("Shortcut behavior"),
                                model=Gtk.StringList.new([_("Press to start, press again to finish"),
                                                          _("Hold while speaking")]))
        behavior.set_selected(1 if self.app.settings.get_string("recording-mode") == "hold" else 0)
        behavior.connect("notify::selected", lambda row, *_args: self.app.settings.set_string(
            "recording-mode", ["toggle", "hold"][row.get_selected()]))
        group.add(behavior)
        physical = Adw.ExpanderRow(title=_("Physical key gestures"), subtitle=_("Advanced · off by default"))
        self.watcher_switch = Adw.SwitchRow(title=_("Watch modifier gestures"),
                                            active=self.app.settings.get_boolean("watcher-enabled"))
        self.watcher_switch.connect("notify::active", lambda row, *_args: self.app.settings.set_boolean(
            "watcher-enabled", row.get_active()))
        physical.add_row(self.watcher_switch)
        info = Adw.ActionRow(title=_("What it does"),
                             subtitle=_("Reads keyboard devices directly to recognize four gestures: Left Super alone "
                                        "(dictate), Left Alt+X (ask), Left Alt+Left Shift (translate) and "
                                        "Ctrl+Right Shift+V (paste last). Requires read access to /dev/input, which on "
                                        "most systems means joining the “input” group — a broad permission. No key is "
                                        "recorded or sent."))
        info.set_subtitle_lines(0)
        physical.add_row(info)
        self.watcher_status = Adw.ActionRow(title=_("Status"))
        physical.add_row(self.watcher_status)
        group.add(physical)
        return group

    def _capture_shortcut(self, key, title):
        dialog = Adw.AlertDialog(heading=_("Set Shortcut: {name}").format(name=title),
                                 body=_("Press the new key combination, with Super, Ctrl or Alt. "
                                        "Escape cancels; Backspace clears."))
        status = label("", ("dim-label",), 0.5, wrap=True)
        dialog.set_extra_child(status)
        dialog.add_response("cancel", _("Cancel"))
        controller = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)

        def pressed(_controller, keyval, _keycode, state):
            mods = state & Gtk.accelerator_get_default_mod_mask()
            if keyval == Gdk.KEY_Escape and not mods:
                dialog.close()
                return True
            if keyval == Gdk.KEY_BackSpace and not mods:
                self._set_shortcut(key, "")
                dialog.close()
                return True
            if keyval in (Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Control_L,
                          Gdk.KEY_Control_R, Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
                          Gdk.KEY_ISO_Level3_Shift):
                return False
            keyval = Gdk.keyval_to_lower(keyval)
            if not mods & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
                status.set_text(_("Use at least one of Super, Ctrl or Alt."))
                return True
            if not Gtk.accelerator_valid(keyval, mods):
                status.set_text(_("That combination cannot be used."))
                return True
            accelerator = Gtk.accelerator_name(keyval, mods)
            conflict = self._shortcut_conflict(accelerator, key)
            if conflict:
                status.set_text(_("Already used by {name}. Choose another combination.").format(name=conflict))
                return True
            self._set_shortcut(key, accelerator)
            dialog.close()
            return True
        controller.connect("key-pressed", pressed)
        dialog.add_controller(controller)
        self.last_dialog = dialog
        dialog.present(self)

    def _shortcut_conflict(self, accelerator, key):
        parsed = Gtk.accelerator_parse(accelerator)
        wanted = (parsed[-2], parsed[-1])
        for other, title, _hint in SHORTCUTS:
            if other != key and any(Gtk.accelerator_parse(value)[-2:] == wanted
                                    for value in self.app.settings.get_strv(other)):
                return f"{APP_NAME}: {_(title)}"
        source = Gio.SettingsSchemaSource.get_default()
        for schema_id in SYSTEM_SHORTCUT_SCHEMAS:
            schema = source.lookup(schema_id, True) if source else None
            if schema is None:
                continue
            settings = Gio.Settings.new_full(schema, None, None)
            for name in schema.list_keys():
                if schema.get_key(name).get_value_type().dup_string() != "as":
                    continue
                if any(Gtk.accelerator_parse(value)[-2:] == wanted for value in settings.get_strv(name)):
                    return _("GNOME ({name})").format(name=name)
        return None

    def _set_shortcut(self, key, accelerator):
        self.app.settings.set_strv(key, [accelerator] if accelerator else [])
        self.toast(_("Shortcut saved.") if accelerator else _("Shortcut cleared."))

    def _modes_group(self):
        group = Adw.PreferencesGroup(title=_("Modes"),
                                     description=_("In automatic mode, the app in focus picks the mode using the "
                                                   "rules below."))
        self.fixed_choice = Adw.ComboRow(title=_("Mode"))
        self._fill_modes()
        self.fixed_choice.connect("notify::selected", self._choose_fixed_mode)
        group.add(self.fixed_choice)
        for mode in modes_module.SPECIALIZED:
            row = Adw.SwitchRow(title=_("Offer “{name}”").format(name=modes_module.title(mode)),
                                subtitle=modes_module.description(mode),
                                active=mode in self.app.config["specialized_modes"])
            row.connect("notify::active", lambda widget, *_args, m=mode: self._toggle_specialized(m, widget.get_active()))
            group.add(row)
        rules = Adw.ExpanderRow(title=_("Mode by app"),
                                subtitle=_("The first expression that matches the window class or title wins."))
        self.rules_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12,
                                 margin_start=12, margin_end=12)
        for rule in self.app.config["rules"]:
            self._add_rule(rule)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        actions.append(button(_("Add Rule"), lambda *_args: self._add_rule({"pattern": "", "mode": "dictation"})))
        actions.append(button(_("Save Rules"), self._save_rules, ("suggested-action",)))
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        container.append(self.rules_box)
        container.append(actions)
        actions.set_margin_bottom(12)
        actions.set_margin_end(12)
        rules.add_row(container)
        group.add(rules)
        return group

    def _fill_modes(self):
        self._syncing = True
        available = modes_module.available(self.app.config)
        self.fixed_options = ["auto", *available]
        self.fixed_choice.set_model(Gtk.StringList.new([_("Automatic"), *map(modes_module.title, available)]))
        fixed = self.app.settings.get_string("fixed-mode")
        self.fixed_choice.set_selected(self.fixed_options.index(fixed) if fixed in self.fixed_options else 0)
        self._syncing = False

    def _choose_fixed_mode(self, row, *_args):
        if not self._syncing:
            self.app.settings.set_string("fixed-mode", self.fixed_options[row.get_selected()])

    def _toggle_specialized(self, mode, active):
        enabled = set(self.app.config["specialized_modes"])
        enabled.add(mode) if active else enabled.discard(mode)
        self.app.config["specialized_modes"] = [m for m in modes_module.SPECIALIZED if m in enabled]
        # The panel menu of the extension lists the same modes.
        self.app.settings.set_strv("offered-modes", list(modes_module.available(self.app.config)))
        if not active and self.app.settings.get_string("fixed-mode") == mode:
            self.app.settings.set_string("fixed-mode", "auto")
        self._fill_modes()

    def _add_rule(self, rule):
        row = Gtk.Box(spacing=8)
        pattern = Gtk.Entry(text=rule.get("pattern", ""), hexpand=True, placeholder_text=_("e.g. slack|discord"))
        pattern.update_property([Gtk.AccessibleProperty.LABEL], [_("Window expression")])
        modes = modes_module.MODES
        choice = Gtk.DropDown.new_from_strings([modes_module.title(mode) for mode in modes])
        choice.set_selected(modes.index(rule["mode"]) if rule.get("mode") in modes else 0)
        choice.update_property([Gtk.AccessibleProperty.LABEL], [_("Mode")])
        row.append(pattern)
        row.append(choice)
        row.append(button("", lambda *_args: self._remove_rule(row), ("flat",), "user-trash-symbolic", _("Remove rule")))
        self.rule_widgets.append((row, pattern, choice))
        self.rules_box.append(row)

    def _remove_rule(self, row):
        self.rule_widgets = [item for item in self.rule_widgets if item[0] is not row]
        self.rules_box.remove(row)

    def _save_rules(self, *_args):
        rules = []
        for _row, pattern, choice in self.rule_widgets:
            text = pattern.get_text().strip()
            if not valid_pattern(text):
                self.toast(_("Fill in or remove empty rules, and check brackets and parentheses."))
                return
            rules.append({"pattern": text, "mode": modes_module.MODES[choice.get_selected()]})
        self.app.config["rules"] = rules
        self.toast(_("Rules saved."))

    def _language_group(self):
        group = Adw.PreferencesGroup(title=_("Languages"),
                                     description=_("Saying “in Spanish:” (or “em espanhol:”, “en español:”) at the "
                                                   "start switches the language of one dictation."))
        codes = list(langs.LANGUAGES)
        spoken = Adw.ComboRow(title=_("Spoken language"), model=Gtk.StringList.new([langs.label(c) for c in codes]))
        spoken.set_selected(codes.index(self.app.config["input_language"]))
        spoken.connect("notify::selected", lambda row, *_args: self._set_config("input_language", codes[row.get_selected()]))
        group.add(spoken)
        self.languages_entry = Adw.EntryRow(title=_("Translation languages, separated by semicolons"),
                                            text="; ".join(self.app.settings.get_strv("translation-languages")),
                                            show_apply_button=True)
        self.languages_entry.connect("apply", self._save_languages)
        group.add(self.languages_entry)
        self.language_choice = Adw.ComboRow(title=_("Translate into"))
        self._fill_translation_languages()
        self.language_choice.connect("notify::selected", self._choose_language)
        group.add(self.language_choice)
        rules = Adw.ExpanderRow(title=_("Language by app"), subtitle=_("One rule per line: expression => code"))
        self.language_rules_editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True, top_margin=8,
                                                  bottom_margin=8, left_margin=8, right_margin=8)
        self.language_rules_editor.update_property([Gtk.AccessibleProperty.LABEL], [_("Language by app")])
        self.language_rules_editor.get_buffer().set_text(
            "\n".join(r["pattern"] + " => " + r["language"] for r in self.app.config["language_rules"]))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        box.append(label(_("Codes: {codes}").format(codes=", ".join(codes)), ("dim-label", "caption"), wrap=True))
        editor = Gtk.ScrolledWindow(min_content_height=100, css_classes=["card"])
        editor.set_child(self.language_rules_editor)
        box.append(editor)
        box.append(button(_("Save"), self._save_language_rules, ("suggested-action",)))
        box.get_last_child().set_halign(Gtk.Align.END)
        rules.add_row(box)
        group.add(rules)
        return group

    def _fill_translation_languages(self):
        self._syncing = True
        languages = self.app.settings.get_strv("translation-languages")
        current = self.app.settings.get_string("translation-language")
        self.language_choice.set_model(Gtk.StringList.new(languages))
        self.language_choice.set_selected(languages.index(current) if current in languages else 0)
        self._syncing = False

    def _save_languages(self, row):
        languages = list(dict.fromkeys(x.strip() for x in row.get_text().split(";") if x.strip()))
        if not languages or any(len(x) > 64 for x in languages) or len(languages) > 12:
            self.toast(_("Keep between one and twelve languages, with short names."))
            return
        current = self.app.settings.get_string("translation-language")
        self.app.settings.set_strv("translation-languages", languages)
        self.app.settings.set_string("translation-language", current if current in languages else languages[0])
        self._fill_translation_languages()
        self.toast(_("Translation languages saved."))

    def _choose_language(self, row, *_args):
        if self._syncing:
            return
        languages = self.app.settings.get_strv("translation-languages")
        if row.get_selected() < len(languages):
            self.app.settings.set_string("translation-language", languages[row.get_selected()])

    def _save_language_rules(self, *_args):
        buffer = self.language_rules_editor.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        rules = []
        try:
            for line in text.splitlines():
                if not line.strip():
                    continue
                pattern, language = (part.strip() for part in line.rsplit("=>", 1))
                if not valid_pattern(pattern) or language not in langs.LANGUAGES:
                    raise ValueError()
                rules.append(dict(pattern=pattern, language=language))
        except ValueError:
            self.toast(_("Check the expressions and the language codes."))
            return
        self.app.config["language_rules"] = rules
        self.toast(_("Language rules saved."))

    def _recording_group(self):
        group = Adw.PreferencesGroup(title=_("Recording"))
        from .audio import microphones
        self.mic_names = [_("System default"), *[name for name, _device in microphones()]]
        current = self.app.config["microphone"]
        if current and current not in self.mic_names:
            self.mic_names.append(current)
        mic = Adw.ComboRow(title=_("Microphone"), model=Gtk.StringList.new(self.mic_names))
        mic.set_selected(self.mic_names.index(current) if current else 0)
        mic.connect("notify::selected", lambda row, *_args: self._set_config(
            "microphone", "" if row.get_selected() == 0 else self.mic_names[row.get_selected()]))
        group.add(mic)
        group.add(self._switch(_("Start and end sounds"), _("Two short notes when the microphone opens and closes."), "sounds"))
        group.add(self._switch(_("Stop on silence"), _("After you start speaking, finish at the chosen pause."),
                               "silence_stop"))
        pause = Adw.SpinRow.new_with_range(1, 30, 0.5)
        pause.set_title(_("Pause length (seconds)"))
        pause.set_value(self.app.config["silence_seconds"])
        pause.connect("notify::value", lambda row, *_args: self._set_config("silence_seconds", row.get_value()))
        group.add(pause)
        group.add(self._switch(_("Deliver in parts"),
                               _("In plain dictation, pastes at pauses after 20 s and keeps listening. Takes "
                                 "precedence over Stop on silence."), "pause_delivery"))
        return group

    def _privacy_group(self):
        group = Adw.PreferencesGroup(title=_("Privacy and History"),
                                     description=_("The first three options send more of your data to the service. "
                                                   "They stay off until you turn them on."))
        group.add(self._switch(_("Learn from history"),
                               _("Sends up to five earlier texts (2,500 characters in total) with each revision as "
                                 "style examples. Never in literal mode."), "learn_from_history"))
        group.add(self._switch(_("Reply with conversation context"),
                               _("With its shortcut, reads up to 3,000 characters of the visible conversation through "
                                 "accessibility and sends them as support. The conversation is not stored."),
                               "reply_context"))
        group.add(self._switch(_("Live text"),
                               _("After 15 s of speech, transcribes new audio every 6 s to show progress in the pill. "
                                 "Multiplies the calls to the speech service."), "live_text"))
        self.retention = Adw.SpinRow.new_with_range(0, 36500, 1)
        self.retention.set_title(_("Keep history for (days)"))
        self.retention.set_subtitle(_("0 keeps it until you delete it."))
        self.retention.set_value(self.app.config["history_days"])
        self.retention.add_suffix(button(_("Apply"), self._retention_dialog, ("flat",)))
        group.add(self.retention)
        keep = Adw.SpinRow.new_with_range(0, 100, 1)
        keep.set_title(_("Keep audio of the last dictations"))
        keep.set_subtitle(_("0 keeps no audio in the history."))
        keep.set_value(self.app.config["audio_keep"])
        keep.connect("notify::value", self._audio_keep)
        group.add(keep)
        rescue = Adw.ActionRow(title=_("Recovery"),
                               subtitle=_("The audio of failed or cancelled dictations is kept for 24 hours in a "
                                          "private folder so you can recover them, then deleted."))
        rescue.set_subtitle_lines(0)
        group.add(rescue)
        return group

    def _retention_dialog(self, *_args):
        days = int(self.retention.get_value())
        if not days:
            self.app.config["history_days"] = 0
            self.toast(_("The history will be kept until you delete it."))
            return
        dialog = Adw.AlertDialog(heading=_("Apply history retention?"),
                                 body=ngettext("Entries older than {days} day will be deleted now and from then on, "
                                               "with their audio.",
                                               "Entries older than {days} days will be deleted now and from then on, "
                                               "with their audio.", days).format(days=days))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("apply", _("Delete Older Entries"))
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.DESTRUCTIVE)

        def response(_dialog, choice):
            if choice == "apply":
                try:
                    self.app.config["history_days"] = days
                    count = self.app.history.prune_days(days)
                    self.app.reload_history()
                    self.refresh_history()
                    self.refresh_suggestions()
                    self.toast(ngettext("{count} old entry deleted.", "{count} old entries deleted.",
                                        count).format(count=count))
                except (OSError, sqlite3.Error):
                    self.toast(_("Could not apply the retention."))
        dialog.connect("response", response)
        self.last_dialog = dialog
        dialog.present(self)

    def _audio_keep(self, row, *_args):
        value = int(row.get_value())
        self.app.config["audio_keep"] = value
        self.app.history.prune_audio(value)

    def _notes_group(self):
        group = Adw.PreferencesGroup(title=_("Notes Folder"),
                                     description=_("Optional. When a folder is set, dictations that start with "
                                                   "“note:” are appended to a daily Markdown file there instead of "
                                                   "being pasted."))
        self.notes_row = plain_row(Adw.ActionRow, title=_("Folder"))
        self.notes_row.add_suffix(button(_("Choose…"), self._choose_notes_folder, ("flat",)))
        self.notes_row.add_suffix(button("", lambda *_args: self._set_notes_folder(""), ("flat",),
                                         "edit-clear-symbolic", _("Turn off")))
        group.add(self.notes_row)
        folders = Adw.EntryRow(title=_("Allowed subfolders, separated by semicolons"),
                               text="; ".join(self.app.config["note_subfolders"]), show_apply_button=True)
        folders.connect("apply", self._save_note_folders)
        group.add(folders)
        group.add(self._switch(_("Link vocabulary terms"),
                               _("Wraps terms that already have a page in the folder in [[wiki links]]."),
                               "notes_wikilinks"))
        self._sync_notes()
        return group

    def _sync_notes(self):
        folder = self.app.config["notes_folder"]
        self.notes_row.set_subtitle(folder or _("Off"))

    def _choose_notes_folder(self, *_args):
        dialog = Gtk.FileDialog(title=_("Choose a Notes Folder"))

        def chosen(source, result):
            try:
                folder = source.select_folder_finish(result)
                if folder and folder.get_path():
                    self._set_notes_folder(folder.get_path())
            except GLib.Error:
                pass
        dialog.select_folder(self, None, chosen)

    def _set_notes_folder(self, path):
        self.app.config["notes_folder"] = path
        self._sync_notes()

    def _save_note_folders(self, row):
        from .notes import valid_folder
        folders = list(dict.fromkeys(x.strip() for x in row.get_text().split(";") if x.strip()))
        if not folders or not all(valid_folder(x) for x in folders):
            self.toast(_("Use folder names without slashes, line breaks or “..”."))
            return
        self.app.config["note_subfolders"] = folders
        self.toast(_("Subfolders saved."))

    def _startup_group(self):
        group = Adw.PreferencesGroup(title=_("Startup"))
        row = Adw.SwitchRow(title=_("Start at login"),
                            subtitle=_("The extension also starts Orfeu Scribe in the background when GNOME starts."),
                            active=self.app.config["autostart"])
        row.connect("notify::active", self._autostart)
        group.add(row)
        return group

    def _autostart(self, row, *_args):
        value = row.get_active()
        self.app.config["autostart"] = value
        from .desktop import set_autostart
        set_autostart(value)

    # --- state -------------------------------------------------------------------
    def refresh(self):
        from .watcher import label as watcher_label
        app = self.app
        configured = getattr(app.provider, "configured", False)
        self.banner.set_revealed(not configured)
        self.setup_status.set_visible(not configured)
        if hasattr(self, "watcher_status"):
            status = getattr(getattr(app, "watcher", None), "status", "extension")
            self.watcher_status.set_subtitle(watcher_label(status))
        progress = app.file_progress
        self.file_group.set_visible(bool(progress))
        if progress:
            self.file_row.set_title(progress["name"])
            self.file_row.set_subtitle(_(FILE_STAGES.get(progress["stage"], FILE_STAGES["opening"])) +
                                       (" · " + _("part {number}").format(number=progress["number"])
                                        if progress.get("number") else ""))
            self.file_meter.set_fraction(min(1, progress["position"] / progress["duration"]) if progress.get("duration") else 0)
            self.file_cancel.set_visible(progress["stage"] not in ("done", "cancelled", "error"))
        busy = app.state in ("transcribing", "polishing")
        listening = app.state == "listening"
        paused = app.settings.get_boolean("paused")
        self.status_title.set_text(_("Paused") if paused else _(STATE_TITLES[app.state]))
        fixed = app.settings.get_string("fixed-mode")
        mode = fixed if fixed != "auto" else app.mode
        detail = [modes_module.title(mode), _("automatic") if fixed == "auto" else _("fixed"),
                  langs.label(app.config["input_language"])]
        self.status_detail.set_text(" · ".join(detail))
        self.record_button.set_sensitive(configured and not busy and not paused)
        self.record_button.set_icon_name("media-playback-stop-symbolic" if listening else "audio-input-microphone-symbolic")
        tooltip = _("Finish dictation") if listening else _("Start dictation")
        self.record_button.set_tooltip_text(tooltip)
        self.record_button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        for css, on in (("suggested-action", not listening), ("destructive-action", listening)):
            self.record_button.add_css_class(css) if on else self.record_button.remove_css_class(css)
        self.cancel_button.set_visible(listening or busy)
        self.level.set_visible(listening)
        if not listening:
            self.level.set_value(0)
        from .live import tail
        self.live_label.set_text(tail(app.live_text, 180) if listening and app.live_text else "")
        self.live_label.set_visible(bool(listening and app.live_text))
        self.error_banner.set_title(app.last_error or "")
        self.error_banner.set_revealed(bool(app.last_error))
        dictate = app.settings.get_strv("toggle-shortcut")
        self.dictate_shortcut.set_accelerator(dictate[0] if dictate else "")
        self.shortcut_button.set_visible(not dictate)
        for key, shortcut in getattr(self, "shortcut_labels", {}).items():
            values = app.settings.get_strv(key)
            shortcut.set_accelerator(values[0] if values else "")
        row = app.last_record
        self.last_group.set_visible(bool(row))
        if row:
            self.last_text.set_text(History.final_text(row) or row["raw"])
            self.last_meta.set_text(" · ".join(filter(None, [
                modes_module.title(row["mode"]), row.get("app") or "",
                _("transcription {value}").format(value=seconds(row["stt"])),
                _("revision {value}").format(value=seconds(row["polishing"])), row["model"]])))
            self.copy_last.set_sensitive(bool(History.final_text(row)))
        if self.stack.get_visible_child_name() == "history" and not self.edit_drafts:
            self.refresh_history()

    def check_extension(self):
        def done(connection, result):
            try:
                info = connection.call_finish(result).unpack()[0]
                state = info.get("state", 0)
                if state == 1:
                    self.extension_row.set_subtitle(_("Active. Shortcuts and pasting work in any app."))
                elif info:
                    self.extension_row.set_subtitle(_("Installed but not enabled. Enable it in the Extensions app."))
                else:
                    self.extension_row.set_subtitle(_("Not installed. Without it, dictate here and copy the result."))
            except GLib.Error:
                self.extension_row.set_subtitle(_("Needs a GNOME session to paste text at the cursor."))
        self.app.connection.call("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell.Extensions",
                                 "GetExtensionInfo", GLib.Variant("(s)", (EXTENSION_UUID,)), None,
                                 Gio.DBusCallFlags.NONE, 3000, None, done)
