// SPDX-License-Identifier: GPL-3.0-or-later
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';
import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const BUS = 'io.github.lucasmariano003_wq.OrfeuScribe.Service';
const PATH = '/io/github/lucasmariano003_wq/OrfeuScribe/Service';
const XML = `<node><interface name="${BUS}">
<method name="Start"/><method name="Stop"/><method name="Toggle"/><method name="Cancel"/><method name="Open"/>
<method name="SetRequest"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="i" direction="in"/></method>
<method name="PasteLast"/>
<method name="Recover"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
<signal name="Rescued"><arg type="s"/></signal>
<signal name="MicWarning"><arg type="s"/></signal>
<signal name="Partial"><arg type="s"/></signal>
<method name="RevertToRaw"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<signal name="Revertible"><arg type="s"/></signal>
<method name="WatcherReady"><arg type="b" direction="in"/></method>
<method name="ConfirmGesture"><arg type="s" direction="in"/></method>
<signal name="Gesture"><arg type="s"/></signal>
<signal name="ProbeGesture"><arg type="s"/></signal>
<signal name="Card"><arg type="s"/></signal><signal name="Notice"><arg type="s"/></signal>
<signal name="Operation"><arg type="s"/><arg type="s"/></signal><signal name="Replace"><arg type="s"/><arg type="s"/></signal>
<method name="SetContext"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
<method name="GetStatus"><arg type="s" direction="out"/><arg type="s" direction="out"/></method>
<signal name="StateChanged"><arg type="s"/></signal><signal name="Level"><arg type="d"/></signal>
<signal name="CancelDelivery"/><signal name="Deliver"><arg type="s"/></signal><signal name="Error"><arg type="s"/></signal>
</interface></node>`;
const Proxy = Gio.DBusProxy.makeProxyWrapper(XML);
// "Revert to transcript" only applies right after the paste.
const RAW_WINDOW_US = 10000000;
// From the first keystroke after the paste to the revert shortcut: longer than
// this means typing, and the app's Ctrl+Z would undo the typing instead.
const RAW_CHORD_US = 1500000;
const TERMINAL = /gnome-terminal|kgx|console|ptyxis|kitty|alacritty|wezterm|foot/i;
// Older GNOME versions have no release trigger; hold-to-talk then needs physical gestures.
const RELEASE = Meta.KeyBindingFlags.TRIGGER_RELEASE ?? 0;
const MODE_TITLES = {
    auto: () => _('Automatic'), dictation: () => _('Revised text'), literal: () => _('Literal dictation'),
    message: () => _('Message'), email: () => _('Email'), prompt: () => _('AI instruction'),
    outline: () => _('Talk outline'), script: () => _('Video script'),
};
// Translators keep %s where the value goes; no dependency on String.prototype.format.
const fill = (text, value) => text.replace('%s', value);
const SHORTCUTS = [['edit-shortcut', 'edit'], ['ask-accelerator', 'ask'], ['reply-shortcut', 'reply'],
    ['translate-accelerator', 'translation']];

export default class OrfeuScribeExtension extends Extension {
    enable() {
        this._alive = true;
        this._timers = new Set();
        this._state = 'idle';
        this._held = false;
        this._pressedKeys = new Set();
        this._settings = this.getSettings();
        this._seat = global.stage.get_context().get_backend().get_default_seat();
        this._keyboard = this._seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._clipboard = St.Clipboard.get_default();
        this._panel = new PanelMenu.Button(0.0, 'Orfeu Scribe');
        this._panel.add_child(new St.Icon({gicon: Gio.icon_new_for_string(`${this.path}/orfeuscribe-symbolic.svg`),
            style_class: 'system-status-icon'}));
        this._panel.menu.addAction(_('Open Orfeu Scribe'), () => this._open());
        this._modeMenu = new PopupMenu.PopupSubMenuMenuItem(_('Mode'));
        this._panel.menu.addMenuItem(this._modeMenu);
        this._languageMenu = new PopupMenu.PopupSubMenuMenuItem(_('Translate into'));
        this._panel.menu.addMenuItem(this._languageMenu);
        this._panel.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._pause = new PopupMenu.PopupSwitchMenuItem(_('Pause'), this._settings.get_boolean('paused'));
        this._pause.connect('toggled', (_item, active) => this._settings.set_boolean('paused', active));
        this._panel.menu.addMenuItem(this._pause);
        this._panel.menu.addAction(_('Cancel dictation'), () => this._call('Cancel'));
        Main.panel.addToStatusArea(this.uuid, this._panel);
        this._pill = new St.BoxLayout({style_class: 'orfeuscribe-pill', reactive: true, can_focus: false, opacity: 0, visible: false});
        const pill = this._pill;
        pill.connect('destroy', () => {
            // Shell shutdown can destroy UI actors before disconnecting D-Bus.
            if (this._pill === pill)
                this._pill = null;
        });
        this._pill.connect('button-release-event', () => {
            if (this._operation === 'translation' && this._state === 'listening') {
                const languages = this._settings.get_strv('translation-languages');
                const index = languages.indexOf(this._settings.get_string('translation-language'));
                if (languages.length) this._settings.set_string('translation-language', languages[(index + 1) % languages.length]);
            }
            return Clutter.EVENT_STOP;
        });
        this._pill.set_pivot_point(0.5, 0.5);
        this._icon = new St.Icon({gicon: Gio.icon_new_for_string(`${this.path}/orfeuscribe-symbolic.svg`),
            style_class: 'orfeuscribe-pill-icon', y_align: Clutter.ActorAlign.CENTER});
        this._pill.add_child(this._icon);
        this._label = new St.Label({text: _('Listening…'), style_class: 'orfeuscribe-pill-label', y_align: Clutter.ActorAlign.CENTER});
        this._pill.add_child(this._label);
        const waves = new St.BoxLayout({style_class: 'orfeuscribe-waves', y_align: Clutter.ActorAlign.CENTER});
        this._bars = [];
        for (let i = 0; i < 7; i++) {
            const bar = new St.Widget({style_class: 'orfeuscribe-wave', height: 12, y_align: Clutter.ActorAlign.CENTER});
            waves.add_child(bar);
            this._bars.push(bar);
        }
        this._waves = waves;
        this._pill.add_child(waves);
        this._elapsed = new St.Label({text: '0:00', style_class: 'orfeuscribe-pill-time', y_align: Clutter.ActorAlign.CENTER});
        this._pill.add_child(this._elapsed);
        // Revert to transcript: only on the "done" pill, right after a paste.
        this._rawButton = new St.Button({label: _('Transcript'), style_class: 'orfeuscribe-pill-button', can_focus: false,
            reactive: true, visible: false, y_align: Clutter.ActorAlign.CENTER});
        this._rawButton.connect('clicked', () => this._revertToRaw('button'));
        this._pill.add_child(this._rawButton);
        // No dictation is lost: on the error pill, Recover transcribes the kept audio again.
        this._recoverButton = new St.Button({label: _('Recover'), style_class: 'orfeuscribe-pill-button', can_focus: false,
            reactive: true, visible: false, y_align: Clutter.ActorAlign.CENTER});
        this._recoverButton.connect('clicked', () => this._recover());
        this._pill.add_child(this._recoverButton);
        Main.layoutManager.addChrome(this._pill, {trackFullscreen: false});
        this._monitorId = Main.layoutManager.connect('monitors-changed', () => this._position());
        this._pill.connect('notify::width', () => this._position());
        this._captureId = global.stage.connect('captured-event', (_actor, event) => this._capture(event));
        // The Shell does not see app keystrokes, but knows when the last used device
        // changes: enough to tell whether the keyboard was used after the paste.
        this._deviceId = global.backend.connect('last-device-changed', (_backend, device) => this._deviceChanged(device));
        this._settingsId = this._settings.connect('changed', () => this._settingsChanged());
        this._sessionId = Main.sessionMode.connect('updated', () => {
            if (Main.sessionMode.isLocked) {
                this._held = false;
                this._watcherReady();
                this._closeCard();
                this._target = null;
                this._call('Cancel');
                this._hide();
            }
        });
        Main.wm.addKeybinding('toggle-shortcut', this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT | RELEASE,
            Shell.ActionMode.NORMAL, (_display, _window, event) => this._shortcut(event));
        Main.wm.addKeybinding('raw-shortcut', this._settings, Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL, () => this._revertToRaw('shortcut'));
        Main.wm.addKeybinding('paste-accelerator', this._settings, Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL, () => this._pasteLast());
        // Edit (with a selection) / Write (without one), Ask, Reply and Translate,
        // with the same press/hold mechanics as the dictation shortcut.
        for (const [key, operation] of SHORTCUTS)
            Main.wm.addKeybinding(key, this._settings,
                Meta.KeyBindingFlags.IGNORE_AUTOREPEAT | RELEASE,
                Shell.ActionMode.NORMAL, (_display, _window, event) => this._shortcut(event, operation));
        // The PRIMARY selection outlives the window that made it. It only counts as
        // the request's selection if it was made in the window focused at the shortcut.
        this._selectionOwner = global.display.get_selection();
        this._primaryId = this._selectionOwner.connect('owner-changed', (_selection, type, source) => {
            if (type === Meta.SelectionType.SELECTION_PRIMARY)
                this._primaryWindow = source ? global.display.focus_window : null;
        });
        this._proxy = new Proxy(Gio.DBus.session, BUS, PATH, (proxy, error) => {
            if (!this._alive)
                return;
            if (error) {
                this._error(_('Orfeu Scribe service unavailable'));
                return;
            }
            this._proxySignals = [
                proxy.connectSignal('Gesture', (_p, _sender, [gesture]) => this._physicalGesture(gesture)),
                proxy.connectSignal('ProbeGesture', (_p, _sender, [gesture]) => {
                    if (this._acceptPhysicalGesture(gesture)) this._proxy.ConfirmGestureRemote(gesture, () => {});
                }),
                proxy.connectSignal('StateChanged', (_p, _sender, [state]) => this._showState(state)),
                proxy.connectSignal('Level', (_p, _sender, [level]) => this._level(level)),
                proxy.connectSignal('CancelDelivery', () => {
                    this._deliverySerial = (this._deliverySerial ?? 0) + 1;
                    this._target = null;
                    this._revertOffer = null;
                    this._restoreClipboard();
                }),
                proxy.connectSignal('Revertible', (_p, _sender, [data]) => {
                    try { this._revertOffer = JSON.parse(data); } catch (_e) { this._revertOffer = null; }
                }),
                proxy.connectSignal('Deliver', (_p, _sender, [text]) => {
                    const offer = this._revertOffer;
                    this._revertOffer = null;
                    this._deliver(text, target => this._offerRaw(offer, text, target));
                }),
                proxy.connectSignal('Card', (_p, _sender, [data]) => this._showCard(JSON.parse(data))),
                proxy.connectSignal('Notice', (_p, _sender, [message]) => { this._showState('done'); this._label.text = message; }),
                proxy.connectSignal('Operation', (_p, _sender, [operation, language]) => {
                    this._operation = operation;
                    this._language = language;
                    this._operationLabel();
                }),
                proxy.connectSignal('Replace', (_p, _sender, [text, selection]) => this._replace(text, selection)),
                proxy.connectSignal('Partial', (_p, _sender, [text]) => this._partial(text)),
                proxy.connectSignal('MicWarning', (_p, _sender, [message]) => this._micWarning(message)),
                proxy.connectSignal('Rescued', (_p, _sender, [data]) => {
                    try { this._rescue = JSON.parse(data); } catch (_e) { this._rescue = null; }
                }),
                proxy.connectSignal('Error', (_p, _sender, [message]) => this._error(message)),
            ];
            this._ownerId = proxy.connect('notify::g-name-owner', () => this._sync());
            this._sync();
        }, null, Gio.DBusProxyFlags.DO_NOT_AUTO_START);
        this._settingsChanged();
        this._spawn(true);
        const renew = () => { this._watcherReady(); this._later(2000, renew); };
        renew();
    }

    _later(ms, callback) {
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timers.delete(id);
            if (this._alive)
                callback();
            return GLib.SOURCE_REMOVE;
        });
        this._timers.add(id);
        return id;
    }

    _spawn(background) {
        this._lastWake = GLib.get_monotonic_time();
        try {
            const args = [`${GLib.get_home_dir()}/.local/bin/orfeuscribe`];
            if (background)
                args.push('--background');
            const process = Gio.Subprocess.new(args, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
            process.wait_async(null, () => {});
        } catch (_e) {
            this._error(_('Orfeu Scribe is not installed. Run its install.sh.'));
        }
    }

    _open() {
        if (this._proxy?.g_name_owner)
            this._call('Open');
        else
            this._spawn(false);
    }

    _sync() {
        if (!this._alive)
            return;
        this._watcherReady();
        if (!this._proxy?.g_name_owner) {
            this._held = false;
            this._hide();
            return;
        }
        this._proxy.GetStatusRemote((result, error) => {
            if (this._alive && !error)
                this._showState(result[0]);
        });
    }

    _call(method, callback = null) {
        if (!this._proxy?.g_name_owner) {
            this._spawn(true);
            this._error(_('Orfeu Scribe is starting. Try again.'));
            return;
        }
        this._proxy[`${method}Remote`]((_result, error) => {
            if (!this._alive)
                return;
            if (error)
                this._error(_('Orfeu Scribe service unavailable. Open Orfeu Scribe.'));
            else
                callback?.();
        });
    }

    _capture(event) {
        const press = event.type() === Clutter.EventType.KEY_PRESS;
        if (this._card && press && event.get_key_symbol() === Clutter.KEY_Escape) {
            this._closeCard();
            return Clutter.EVENT_STOP;
        }
        if (this._card) this._touchCard();
        return Clutter.EVENT_PROPAGATE;
    }

    _watcherReady() {
        if (this._proxy?.g_name_owner) {
            this._missingSince = 0;
            this._proxy.WatcherReadyRemote(this._alive && !Main.sessionMode.isLocked &&
                !!(Main.actionMode & Shell.ActionMode.NORMAL), () => {});
        } else if (this._alive && !Main.sessionMode.isLocked && this._settings.get_boolean('watcher-enabled')) {
            const now = GLib.get_monotonic_time();
            this._missingSince ||= now;
            if (now - this._missingSince > 5000000 && now - (this._lastWake ?? 0) > 10000000)
                this._spawn(true);
        }
    }

    _acceptPhysicalGesture(gesture) {
        const [action, operation] = gesture.split(':');
        if (!['start', 'stop', 'cancel', 'toggle'].includes(action) ||
            !['dictation', 'ask', 'translation', 'paste'].includes(operation)) return false;
        return this._alive && !Main.sessionMode.isLocked && !this._settings.get_boolean('paused') &&
            this._settings.get_boolean('watcher-enabled') && !!(Main.actionMode & Shell.ActionMode.NORMAL);
    }

    _physicalGesture(gesture) {
        const [action, operation] = gesture.split(':');
        // Releasing ends a recording already started, even in a modal dialog.
        // The permission to start must never prevent closing the microphone.
        if (action === 'stop' && ['dictation', 'ask', 'translation'].includes(operation)) {
            this._requestSerial = (this._requestSerial ?? 0) + 1;
            if (this._held) { this._held = false; this._call('Stop'); }
            return;
        }
        if (!this._acceptPhysicalGesture(gesture) && !gesture.startsWith('cancel:')) return;
        if (action === 'cancel') {
            this._requestSerial = (this._requestSerial ?? 0) + 1;
            if (this._held) { this._held = false; this._call('Cancel'); }
            return;
        }
        if (!this._alive || Main.sessionMode.isLocked || this._settings.get_boolean('paused') ||
            !this._settings.get_boolean('watcher-enabled') || !(Main.actionMode & Shell.ActionMode.NORMAL)) return;
        if (operation === 'paste') {
            if (action === 'toggle')
                this._pasteLast();
        } else if (action === 'start') {
            this._begin(operation, true);
        } else if (action === 'stop') {
            if (this._held) { this._held = false; this._call('Stop'); }
        } else if (this._state === 'listening') {
            this._call('Stop');
        } else {
            this._begin(operation, false);
        }
    }

    _pasteLast() {
        if (this._settings.get_boolean('paused')) return;
        this._target = global.display.focus_window;
        this._call('PasteLast');
    }

    _shortcut(event, operation = 'dictation') {
        if (this._settings.get_boolean('paused')) return;
        const hold = this._settings.get_string('recording-mode') === 'hold' && RELEASE !== 0;
        if (event.type() === Clutter.EventType.KEY_RELEASE) {
            if (hold && this._held) { this._held = false; this._call('Stop'); }
        } else if (!hold && this._state === 'listening') {
            this._call('Stop');
        } else {
            this._begin(operation, hold);
        }
    }

    _begin(operation, hold) {
        if (['transcribing', 'polishing'].includes(this._state) || this._held) return;
        this._closeCard();
        this._held = hold;
        const serial = this._requestSerial = (this._requestSerial ?? 0) + 1;
        this._target = global.display.focus_window;
        this._operation = operation;
        this._language = this._settings.get_string('translation-language');
        const target = this._target;
        const prepare = selection => {
            if (!this._alive || serial !== this._requestSerial || (hold && !this._held)) return;
            if (!this._proxy?.g_name_owner) { this._held = false; this._call('Start'); return; }
            this._proxy.SetContextRemote(target?.get_wm_class() ?? '', target?.get_title() ?? '', (_r, error) => {
                if (!this._alive || error || (hold && !this._held)) return;
                this._proxy.SetRequestRemote(operation, selection ?? '', target?.get_pid() ?? 0, (_r2, error2) => {
                    if (!this._alive || error2 || (hold && !this._held)) return;
                    this._call('Start', () => { if (hold && !this._held) this._call('Stop'); });
                });
            });
        };
        this._clipboard.get_text(St.ClipboardType.PRIMARY, (_c, text) => {
            const selection = target && this._primaryWindow === target ? text ?? '' : '';
            // The pill shows the start of the text that will be edited, before speaking.
            this._selectionPreview = selection.replace(/\s+/g, ' ').trim();
            prepare(selection);
        });
    }

    _operationLabel() {
        if (!this._label || this._state === 'idle' || this._state === 'error') return;
        const preview = this._selectionPreview ?? '';
        const prefix = this._operation === 'translation' ? fill(_('Translation · %s'), this._language) :
            this._operation === 'ask' ? _('Question') :
            this._operation === 'edit' ? fill(_('Edit · “%s”'), preview.length > 22 ? `${preview.slice(0, 22)}…` : preview) :
            this._operation === 'write' ? _('Draft') :
            this._operation === 'reply' ? _('Reply with context') : '';
        if (prefix)
            this._label.text = `${prefix} · ${this._state === 'listening' ? _('listening…') : this._state === 'done' ? _('done') : _('working…')}`;
    }

    _replace(text, selection) {
        // In a terminal the selection is program output, not a field: pasting there
        // would type into the prompt. The result goes to the card.
        if (TERMINAL.test(this._target?.get_wm_class() ?? '')) {
            this._showCard({text, web: '', note: _('Terminal: the selection cannot be edited.')});
            return;
        }
        this._clipboard.get_text(St.ClipboardType.PRIMARY, (_c, current) => {
            if (!this._alive) return;
            if (current === selection && this._target === global.display.focus_window) this._deliver(text);
            else this._showCard({text, web: '', note: _('The selection changed. The text was kept.')});
        });
    }

    _touchCard() {
        this._cardTouched = GLib.get_monotonic_time();
    }

    _closeCard() {
        if (this._card) {
            Main.wm.removeKeybinding('card-close-shortcut');
            Main.layoutManager.removeChrome(this._card);
            this._card.destroy();
            this._card = null;
        }
    }

    _showCard(data) {
        this._closeCard();
        Main.wm.addKeybinding('card-close-shortcut', this._settings, Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL, () => this._closeCard());
        const card = this._card = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, style_class: 'orfeuscribe-card',
            reactive: true, can_focus: false});
        const interfaceSettings = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        if (interfaceSettings.get_string('color-scheme') === 'prefer-dark') card.add_style_class_name('dark');
        const head = new St.BoxLayout({style_class: 'orfeuscribe-card-head'});
        head.add_child(new St.Label({text: _('Answer'), style_class: 'orfeuscribe-card-title', y_align: Clutter.ActorAlign.CENTER}));
        if (data.note)
            head.add_child(new St.Label({text: data.note, style_class: 'orfeuscribe-card-note', x_expand: true,
                x_align: Clutter.ActorAlign.END, y_align: Clutter.ActorAlign.CENTER}));
        card.add_child(head);
        const scroll = new St.ScrollView({style_class: 'orfeuscribe-card-scroll', overlay_scrollbars: true});
        const content = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL});
        const body = new St.Label({text: data.text, style_class: 'orfeuscribe-card-body'});
        body.clutter_text.line_wrap = true;
        body.clutter_text.ellipsize = 0;
        content.add_child(body);
        scroll.set_child(content);
        card.add_child(scroll);
        const actions = new St.BoxLayout({style_class: 'orfeuscribe-card-actions', x_align: Clutter.ActorAlign.END});
        for (const [title, style, callback] of [
            [_('Close'), 'orfeuscribe-card-button flat', () => this._closeCard()],
            [_('Copy'), 'orfeuscribe-card-button', () => this._clipboard.set_text(St.ClipboardType.CLIPBOARD, data.text)],
            [_('Insert at Cursor'), 'orfeuscribe-card-button suggested', () => { this._deliver(data.text); this._closeCard(); }]]) {
            const widget = new St.Button({label: title, style_class: style, can_focus: true});
            widget.connect('clicked', callback);
            actions.add_child(widget);
        }
        card.add_child(actions);
        Main.layoutManager.addChrome(card, {trackFullscreen: false});
        const position = () => {
            const monitor = Main.layoutManager.focusMonitor ?? Main.layoutManager.primaryMonitor;
            card.set_position(Math.round(monitor.x + (monitor.width - card.width) / 2),
                Math.max(monitor.y + 24, monitor.y + monitor.height - card.height - 155));
        };
        card.connect('notify::allocation', position);
        this._touchCard();
        const expire = () => {
            if (this._card !== card) return;
            if (GLib.get_monotonic_time() - this._cardTouched > 180000000 &&
                global.backend.get_core_idle_monitor().get_idletime() >= 180000) this._closeCard();
            else this._later(10000, expire);
        };
        this._later(10000, expire);
    }

    _settingsChanged() {
        if (!['transcribing', 'polishing'].includes(this._state)) this._language = this._settings.get_string('translation-language');
        this._languageMenu.label.text = fill(_('Translate into: %s'), this._language);
        this._languageMenu.menu.removeAll();
        for (const language of this._settings.get_strv('translation-languages')) {
            const item = this._languageMenu.menu.addAction(language, () => this._settings.set_string('translation-language', language));
            item.setOrnament(language === this._language ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        }
        this._operationLabel();
        const fixed = this._settings.get_string('fixed-mode');
        this._modeMenu.label.text = fill(_('Mode: %s'), (MODE_TITLES[fixed] ?? MODE_TITLES.auto)());
        this._modeMenu.menu.removeAll();
        for (const value of ['auto', ...this._settings.get_strv('offered-modes')]) {
            if (!MODE_TITLES[value]) continue;
            const item = this._modeMenu.menu.addAction(MODE_TITLES[value](), () => this._settings.set_string('fixed-mode', value));
            item.setOrnament(value === fixed ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        }
        this._pause.setToggleState(this._settings.get_boolean('paused'));
        if (this._settings.get_boolean('paused')) {
            this._held = false;
            this._hide();
        }
    }

    _position() {
        const monitor = Main.layoutManager.focusMonitor ?? Main.layoutManager.primaryMonitor;
        if (monitor && this._pill)
            this._pill.set_position(Math.round(monitor.x + (monitor.width - this._pill.width) / 2), monitor.y + monitor.height - 125);
    }

    _showState(state) {
        const previous = this._state;
        this._state = state;
        this._animationGeneration = (this._animationGeneration ?? 0) + 1;
        if (state !== 'idle' && state !== 'done')
            this._clearRevert(false);
        if (state !== 'error' && this._recoverButton) {
            this._recoverButton.visible = false;
            this._rescueOffer = null;
        }
        if (state === 'idle') {
            // The pill with "Transcript" stays until the end of the 10 s window.
            if (!this._revert)
                this._hide();
            return;
        }
        if (state === 'listening' && previous !== 'listening') {
            this._startedAt = GLib.get_monotonic_time();
            this._partialText = '';
        }
        this._elapsed.visible = ['listening', 'transcribing', 'polishing'].includes(state);
        // Done and error show only the words: the waves belong to listening and working.
        this._waves.visible = this._elapsed.visible;
        this._tickElapsed(this._animationGeneration);
        for (const name of ['working', 'done', 'error', 'warning', 'live', 'listening'])
            this._pill.remove_style_class_name(name);
        this._label.text = state === 'listening' ? _('Listening…') : state === 'done' ? _('Done') :
            state === 'error' ? _('Something went wrong') : _('Working…');
        this._operationLabel();
        if (state === 'transcribing' || state === 'polishing') {
            this._pill.add_style_class_name('working');
            this._animateWorking(this._animationGeneration, 0);
        } else {
            this._pill.add_style_class_name(state);
        }
        if (state === 'listening') {
            this._level(0);
            this._animateListening(this._animationGeneration);
        }
        const appearing = !this._pill.visible || this._pill.opacity === 0;
        this._pill.show();
        this._position();
        if (appearing)
            this._pill.set_scale(0.94, 0.94);
        this._pill.ease({opacity: 255, scale_x: 1, scale_y: 1, duration: 220, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        this._rawButton.visible = state === 'done' && !!this._revert;
        if (state === 'done') {
            const generation = this._animationGeneration;
            this._later(1400, () => {
                if (generation === this._animationGeneration && !this._revert)
                    this._hide();
            });
        }
    }

    _tickElapsed(generation) {
        if (generation !== this._animationGeneration || !this._elapsed.visible)
            return;
        const seconds = Math.max(0, Math.floor((GLib.get_monotonic_time() - (this._startedAt ?? GLib.get_monotonic_time())) / 1000000));
        this._elapsed.text = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
        this._later(200, () => this._tickElapsed(generation));
    }

    _animateWorking(generation, step) {
        if (generation !== this._animationGeneration)
            return;
        for (let i = 0; i < this._bars.length; i++)
            this._bars[i].ease({height: 8 + 14 * ((i + step) % 7) / 6, opacity: 255,
                duration: 420, mode: Clutter.AnimationMode.EASE_IN_OUT_SINE});
        this._later(450, () => this._animateWorking(generation, step + 1));
    }

    _level(level) {
        if (this._state !== 'listening')
            return;
        this._voiceLevel = Number.isFinite(level) ? Math.min(1, Math.max(0, level)) : 0;
    }

    _animateListening(generation) {
        if (generation !== this._animationGeneration || this._state !== 'listening')
            return;
        const phase = GLib.get_monotonic_time() / 1000000;
        for (let i = 0; i < this._bars.length; i++) {
            const shape = 1 - Math.abs(i - 3) / 5;
            // At rest the bars breathe less than 2 px; the voice sets the real amplitude.
            const height = 8 + 2 * shape + .8 * Math.sin(phase * 2 + i * .6) + (this._voiceLevel ?? 0) * 20 * shape;
            this._bars[i].ease({height, opacity: 255, duration: 160, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        }
        this._later(60, () => this._animateListening(generation));
    }

    _hide() {
        this._animationGeneration = (this._animationGeneration ?? 0) + 1;
        if (!this._pill)
            return;
        const pill = this._pill;
        pill.ease({opacity: 0, scale_x: 0.96, scale_y: 0.96, duration: 220, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            onComplete: () => {
                // Destroying an actor can finish its animation synchronously.
                // A disabled/reloaded extension must not access that actor.
                if (this._alive && this._pill === pill)
                    pill.hide();
            }});
    }

    _error(message) {
        const rescue = this._rescue;
        this._rescue = null;
        this._showState('error');
        this._label.text = message.length > 95 ? `${message.slice(0, 92)}…` : message;
        this._recoverButton.visible = !!rescue?.id;
        this._rescueOffer = rescue?.id ? rescue : null;
        const generation = this._animationGeneration;
        // With Recover, the pill waits longer: time to read and decide.
        this._later(rescue?.id ? 15000 : 6500, () => {
            if (generation === this._animationGeneration)
                this._hide();
        });
    }

    // Live text: only the end of the provisional text, in the pill. Nothing is pasted.
    _partial(text) {
        if (this._state !== 'listening' || !this._label || !text) return;
        if (GLib.get_monotonic_time() < (this._warningUntil ?? 0)) return;
        const words = text.replace(/\s+/g, ' ').trim();
        this._partialText = words.length > 46 ? `…${words.slice(-45).replace(/^\S*\s/, '')}` : words;
        this._label.text = this._partialText;
        this._pill.add_style_class_name('live');
    }

    // Microphone check: the warning takes the pill's caption for a few seconds,
    // without interrupting the recording.
    _micWarning(message) {
        if (this._state !== 'listening' || !this._label) return;
        const generation = this._animationGeneration;
        this._warningUntil = GLib.get_monotonic_time() + 4000000;
        this._label.text = message;
        this._pill.add_style_class_name('warning');
        this._later(4000, () => {
            if (generation !== this._animationGeneration || this._state !== 'listening') return;
            this._pill.remove_style_class_name('warning');
            this._warningUntil = 0;
            this._label.text = this._partialText || _('Listening…');
            if (!this._partialText) this._operationLabel();
        });
    }

    _recover() {
        const offer = this._rescueOffer;
        this._rescueOffer = null;
        this._recoverButton.visible = false;
        if (!offer?.id || !this._proxy?.g_name_owner) return;
        this._proxy.RecoverRemote(offer.id, (result, error) => {
            if (!this._alive) return;
            if (error || !result?.[0]) this._notice(_('The audio is in History, under Waiting for Recovery.'));
        });
    }

    _deliver(text, pasted = null) {
        if (!text || Main.sessionMode.isLocked)
            return;
        // A new delivery ends the revert offer of the previous one.
        this._clearRevert(false);
        if (this._clipboardSnapshot) { this._error(_('Wait for the current paste to finish.')); return; }
        const deliverySerial = this._deliverySerial ?? 0;
        const valid = () => deliverySerial === (this._deliverySerial ?? 0);
        const target = this._target;
        if (!target || target !== global.display.focus_window) {
            this._error(_('Text saved to history. The focused app changed.'));
            return;
        }
        const type = St.ClipboardType.CLIPBOARD;
        const mimes = this._clipboard.get_mimetypes(type);
        const mime = mimes.find(m => m === 'image/png') ?? mimes.find(m => m === 'text/plain;charset=utf-8') ??
            mimes.find(m => m.startsWith('text/plain')) ?? mimes[0];
        const paste = (bytes = null, previousText = null) => {
            if (!valid() || !this._alive || target !== global.display.focus_window || Main.sessionMode.isLocked)
                return;
            this._clipboardSnapshot = {mime, bytes, text, previousText};
            this._clipboard.set_text(type, text);
            this._later(70, () => {
                if (!valid() || target !== global.display.focus_window || Main.sessionMode.isLocked) {
                    this._restoreClipboard();
                    return;
                }
                const terminal = TERMINAL.test(target.get_wm_class() ?? '');
                const modifiers = [Clutter.KEY_Control_L, ...(terminal ? [Clutter.KEY_Shift_L] : [])];
                for (const key of modifiers)
                    this._key(key, true);
                this._later(20, () => {
                    if (!valid() || target !== global.display.focus_window || Main.sessionMode.isLocked) {
                        for (const key of modifiers) this._key(key, false);
                        this._restoreClipboard();
                        return;
                    }
                    this._key(Clutter.KEY_v, true);
                    this._later(20, () => {
                        this._key(Clutter.KEY_v, false);
                        for (const key of modifiers.reverse())
                            this._key(key, false);
                        pasted?.(target);
                        this._later(400, () => this._restoreClipboard());
                    });
                });
            });
        };
        if (mime?.startsWith('text/plain'))
            this._clipboard.get_text(type, (_clipboard, previousText) => paste(null, previousText ?? ''));
        else if (mime)
            this._clipboard.get_content(type, mime, (_clipboard, bytes) => {
                if (!bytes) {
                    this._error(_('Could not preserve the clipboard. Text saved to history.'));
                    return;
                }
                // The callback's GBytes is borrowed; restoring needs its own copy.
                paste(new GLib.Bytes(bytes.get_data()));
            });
        else
            paste();
    }

    _notice(message) {
        this._showState('done');
        this._label.text = message;
    }

    _offerRaw(offer, text, target) {
        if (!offer || offer.final !== text || !offer.raw || offer.raw === offer.final || !Number.isInteger(offer.id))
            return;
        const revert = this._revert = {id: offer.id, raw: offer.raw, target, at: GLib.get_monotonic_time(),
            terminal: TERMINAL.test(target?.get_wm_class() ?? '')};
        this._animationGeneration = (this._animationGeneration ?? 0) + 1;
        if (this._pill.visible && this._state !== 'listening') {
            this._rawButton.visible = true;
            this._pill.opacity = 255;
        }
        this._later(RAW_WINDOW_US / 1000, () => {
            if (this._revert === revert)
                this._clearRevert();
        });
    }

    _clearRevert(hide = true) {
        const had = !!this._revert;
        this._revert = null;
        if (this._rawButton)
            this._rawButton.visible = false;
        if (had && hide && ['idle', 'done'].includes(this._state))
            this._hide();
    }

    _deviceChanged(device) {
        // Orfeu Scribe's own keys (paste, Ctrl+Z) do not count.
        if (!this._revert || GLib.get_monotonic_time() < (this._ownKeysUntil ?? 0))
            return;
        if (device?.get_device_type() === Clutter.InputDeviceType.KEYBOARD_DEVICE)
            this._revert.keyboardAt ||= GLib.get_monotonic_time();
    }

    _modifiersHeld() {
        const [, , mods] = global.get_pointer();
        return !!(mods & (Clutter.ModifierType.SHIFT_MASK | Clutter.ModifierType.CONTROL_MASK |
            Clutter.ModifierType.MOD1_MASK | Clutter.ModifierType.MOD4_MASK | Clutter.ModifierType.SUPER_MASK));
    }

    // Replaces the text just pasted with the raw transcript. On Wayland the field
    // cannot be read: the path is the app's own Ctrl+Z (undoes the paste and brings
    // back any replaced selection) followed by pasting the transcript, and only when
    // the focus is still the window of the delivery. In a terminal (where Ctrl+Z
    // suspends the program) or after a focus change, the transcript goes to the
    // clipboard with a notice.
    _revertToRaw(source, attempt = 0) {
        const revert = this._revert;
        if (!this._alive || Main.sessionMode.isLocked || this._settings.get_boolean('paused'))
            return;
        if (!revert || GLib.get_monotonic_time() - revert.at > RAW_WINDOW_US) {
            if (source === 'shortcut' && attempt === 0)
                this._notice(_('Nothing to revert right now.'));
            this._clearRevert();
            return;
        }
        const copy = message => {
            this._clearRevert(false);
            this._clipboard.set_text(St.ClipboardType.CLIPBOARD, revert.raw);
            this._notice(message);
        };
        if (revert.terminal) { copy(_('Terminal: the transcript was copied. Delete and paste.')); return; }
        if (global.display.focus_window !== revert.target) { copy(_('The focus changed: the transcript was copied.')); return; }
        // Keyboard used after the paste (besides the shortcut itself): Ctrl+Z would
        // undo the typing, not the paste.
        const typedFor = revert.keyboardAt ? GLib.get_monotonic_time() - revert.keyboardAt : 0;
        if (attempt === 0 && revert.keyboardAt && (source !== 'shortcut' || typedFor > RAW_CHORD_US)) {
            copy(_('You typed after the paste: the transcript was copied.'));
            return;
        }
        // The previous paste is still restoring the clipboard, or the shortcut's
        // modifiers are still held: wait a little, at most 1.5 s.
        if (this._clipboardSnapshot || this._modifiersHeld()) {
            if (attempt >= 50) { copy(_('Keys still held: the transcript was copied.')); return; }
            this._later(30, () => this._revertToRaw(source, attempt + 1));
            return;
        }
        this._clearRevert(false);
        this._key(Clutter.KEY_Control_L, true);
        this._later(20, () => {
            this._key(Clutter.KEY_z, true);
            this._later(20, () => {
                this._key(Clutter.KEY_z, false);
                this._key(Clutter.KEY_Control_L, false);
                this._later(150, () => {
                    if (global.display.focus_window !== revert.target || Main.sessionMode.isLocked) {
                        this._clipboard.set_text(St.ClipboardType.CLIPBOARD, revert.raw);
                        this._notice(_('The focus changed: the transcript was copied.'));
                        return;
                    }
                    this._target = revert.target;
                    this._deliver(revert.raw);
                    this._proxy?.RevertToRawRemote(revert.id, () => {});
                    this._notice(_('Reverted to transcript.'));
                });
            });
        });
    }

    _key(key, pressed) {
        this._ownKeysUntil = GLib.get_monotonic_time() + 250000;
        this._keyboard.notify_keyval(GLib.get_monotonic_time(), key, pressed ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);
        if (pressed)
            this._pressedKeys.add(key);
        else
            this._pressedKeys.delete(key);
    }

    _restoreClipboard() {
        const snapshot = this._clipboardSnapshot;
        this._clipboardSnapshot = null;
        if (!snapshot)
            return;
        const clipboard = this._clipboard;
        const type = St.ClipboardType.CLIPBOARD;
        clipboard.get_text(type, (_clipboard, current) => {
            // A copy made by the user during the paste wins.
            if (current !== snapshot.text)
                return;
            if (snapshot.previousText !== null)
                clipboard.set_text(type, snapshot.previousText);
            else if (snapshot.mime && snapshot.bytes)
                clipboard.set_content(type, snapshot.mime, snapshot.bytes);
            else
                clipboard.set_text(type, '');
        });
    }

    disable() {
        this._alive = false;
        this._watcherReady();
        global.stage.disconnect(this._captureId);
        global.backend.disconnect(this._deviceId);
        this._closeCard();
        this._held = false;
        this._animationGeneration++;
        for (const key of this._pressedKeys)
            this._key(key, false);
        for (const key of ['toggle-shortcut', 'raw-shortcut', 'paste-accelerator', ...SHORTCUTS.map(([name]) => name)])
            Main.wm.removeKeybinding(key);
        this._selectionOwner.disconnect(this._primaryId);
        this._primaryWindow = this._selectionOwner = null;
        this._revert = this._revertOffer = null;
        this._restoreClipboard();
        for (const id of this._timers)
            GLib.source_remove(id);
        this._timers.clear();
        if (this._proxy) {
            if (this._ownerId)
                this._proxy.disconnect(this._ownerId);
            for (const id of this._proxySignals ?? [])
                this._proxy.disconnectSignal(id);
        }
        this._settings.disconnect(this._settingsId);
        Main.layoutManager.disconnect(this._monitorId);
        Main.sessionMode.disconnect(this._sessionId);
        if (this._pill) {
            Main.layoutManager.removeChrome(this._pill);
            this._pill.destroy();
        }
        this._panel.destroy();
        this._proxy = this._panel = this._pill = this._keyboard = this._seat = null;
        this._settings = this._target = this._bars = null;
        this._proxySignals = null;
    }
}
