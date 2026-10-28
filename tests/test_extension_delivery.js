// Delivery, "Revert to transcript" and clipboard restore, on the real methods of
// extension.js with a simulated Shell: a virtual clock, a fake clipboard and a fake
// target app that reads the clipboard on Ctrl+V and undoes its last insertion on
// Ctrl+Z. This is a logic check, not a GNOME Shell session.
import GLibReal from 'gi://GLib';

const [, data] = GLibReal.file_get_contents('extension/orfeuscribe@lucasmariano003-wq.github.io/extension.js');
const source = new TextDecoder().decode(data);
const begin = source.indexOf('    _later(ms, callback) {');
const end = source.indexOf('\n    disable() {', begin);

let nowMs = 0, nextId = 1, queue = [];
const schedule = (ms, cb) => { const id = nextId++; queue.push({at: nowMs + ms, id, cb}); return id; };
const run = (untilMs = Infinity) => {
    for (;;) {
        queue.sort((a, b) => a.at - b.at || a.id - b.id);
        const item = queue[0];
        if (!item || item.at > untilMs) { if (untilMs !== Infinity) nowMs = untilMs; return; }
        queue.shift();
        nowMs = item.at;
        item.cb();
    }
};
/* eslint-disable no-unused-vars -- names used by the evaluated extension methods */
const GLib = {PRIORITY_DEFAULT: 0, SOURCE_REMOVE: false, get_monotonic_time: () => nowMs * 1000,
    timeout_add: (_p, ms, cb) => schedule(ms, cb), source_remove: id => { queue = queue.filter(i => i.id !== id); },
    Bytes: class { constructor(b) { this.b = b; } get_data() { return this.b; } }};
const Clutter = {KEY_Control_L: 'Ctrl', KEY_Shift_L: 'Shift', KEY_v: 'v', KEY_z: 'z', KEY_Escape: 'Esc',
    KeyState: {PRESSED: 1, RELEASED: 0}, InputDeviceType: {KEYBOARD_DEVICE: 'kbd'},
    ModifierType: {SHIFT_MASK: 1, CONTROL_MASK: 4, MOD1_MASK: 8, MOD4_MASK: 64, SUPER_MASK: 128}};
const St = {ClipboardType: {CLIPBOARD: 0, PRIMARY: 1}};
const _ = s => s;
const fill = (t, v) => t.replace('%s', v);
const RAW_WINDOW_US = 10000000, RAW_CHORD_US = 1500000;
const TERMINAL = /gnome-terminal|kgx|console|ptyxis|kitty|alacritty|wezterm|foot/i;
const editor = {get_wm_class: () => 'org.gnome.TextEditor', get_title: () => 'doc'};
const other = {get_wm_class: () => 'org.gnome.Nautilus', get_title: () => 'files'};
const Main = {sessionMode: {isLocked: false}};
let modifiersUntil = 0;
const global = {display: {focus_window: editor}, get_pointer: () => [0, 0, nowMs < modifiersUntil ? 4 : 0]};
/* eslint-enable no-unused-vars */

const Subject = eval(`(class {${source.slice(begin, end)}})`);

function world() {
    const log = [], field = [], held = new Set();
    const clip = {mimes: {'text/plain;charset=utf-8': 'USER CLIPBOARD'}};
    const clipboard = {
        get_mimetypes: () => Object.keys(clip.mimes),
        get_text: (_t, cb) => schedule(1, () => cb(clipboard, clip.mimes['text/plain;charset=utf-8'] ?? null)),
        set_text: (_t, text) => { clip.mimes = {'text/plain;charset=utf-8': text}; },
        get_content: (_t, mime, cb) => schedule(1, () => cb(clipboard, {get_data: () => clip.mimes[mime]})),
        set_content: (_t, mime, bytes) => { clip.mimes = {[mime]: bytes.get_data()}; },
    };
    const keyboard = {notify_keyval: (_t, key, state) => {
        if (state) held.add(key); else held.delete(key);
        if (state && key === 'v' && held.has('Ctrl'))
            schedule(5, () => { field.push(clip.mimes['text/plain;charset=utf-8']); log.push(`pasted ${field.at(-1)}`); });
        if (state && key === 'z' && held.has('Ctrl'))
            log.push(`undid ${field.pop()}`);
    }};
    const s = new Subject();
    Object.assign(s, {_alive: true, _timers: new Set(), _pressedKeys: new Set(), _clipboard: clipboard,
        _keyboard: keyboard, _target: editor, _state: 'done', _pill: {visible: true, opacity: 255},
        _rawButton: {visible: false}, _settings: {get_boolean: () => false},
        _proxy: {RevertToRawRemote: (id, cb) => { log.push(`recorded revert ${id}`); cb(); }}});
    s._error = m => log.push(`error: ${m}`);
    s._notice = m => log.push(`notice: ${m}`);
    s._hide = () => {};
    return {s, log, field, clip};
}

const deliver = (s, id, raw, final) => s._deliver(final, target => s._offerRaw({id, raw, final}, final, target));
const check = (condition, message, log) => {
    if (!condition)
        throw Error(`${message}\n  ${log.join('\n  ')}`);
};
const before = (log, a, b) => log.indexOf(a) >= 0 && log.indexOf(b) > log.indexOf(a);

// 9a. The next part arrives while the revert is undoing the paste: it waits, and
// the revert is recorded and announced only after the transcript was pasted.
for (const [source, delay] of [['button', 60], ['shortcut', 80]]) {
    const {s, log, field} = world();
    global.display.focus_window = editor;
    deliver(s, 1, 'raw one', 'Part one.');
    run(nowMs + 700);
    if (source === 'shortcut') {
        // The revert shortcut's own keys: Ctrl held for 300 ms, seen as keyboard use.
        modifiersUntil = nowMs + 300;
        s._deviceChanged({get_device_type: () => 'kbd'});
    }
    s._revertToRaw(source);
    run(nowMs + delay);
    deliver(s, 2, '\n\nraw two', '\n\nPart two.');
    run();
    check(field.join('') === 'raw one\n\nPart two.', `9a/${source}: wrong field ${JSON.stringify(field.join(''))}`, log);
    check(before(log, 'pasted raw one', 'recorded revert 1'), `9a/${source}: recorded before the paste`, log);
    check(before(log, 'recorded revert 1', 'notice: Reverted to transcript.'), `9a/${source}: announced early`, log);
    check(before(log, 'notice: Reverted to transcript.', 'pasted \n\nPart two.'), `9a/${source}: part two jumped in`, log);
    check(!s._reverting && !(s._queuedDeliveries ?? []).length, `9a/${source}: the queue was left behind`, log);
}
print('A part arriving during a revert is pasted after the transcript; success comes only after that paste.');

// 9b. The transcript cannot be pasted (the service cancels delivery after Ctrl+Z):
// nothing is recorded, success is not announced, and the transcript is copied.
{
    const {s, log, clip} = world();
    deliver(s, 3, 'raw three', 'Part three.');
    run(nowMs + 700);
    s._revertToRaw('button');
    run(nowMs + 200);              // Ctrl+Z sent; the transcript paste has started
    s._deliverySerial = (s._deliverySerial ?? 0) + 1;   // CancelDelivery handler
    s._queuedDeliveries = [];
    s._restoreClipboard();
    run();
    check(!log.some(line => line.startsWith('recorded revert')), '9b: a failed revert was recorded', log);
    check(!log.includes('notice: Reverted to transcript.'), '9b: a failed revert was announced', log);
    check(log.includes('notice: Could not paste the transcript: it was copied.'), '9b: no failure notice', log);
    check(clip.mimes['text/plain;charset=utf-8'] === 'raw three', '9b: the transcript is not in the clipboard', log);
}
// 9c. Focus moves away right after Ctrl+Z: copied, not recorded.
{
    const {s, log} = world();
    global.display.focus_window = editor;
    deliver(s, 4, 'raw four', 'Part four.');
    run(nowMs + 700);
    s._revertToRaw('button');
    run(nowMs + 45);
    global.display.focus_window = other;
    run();
    global.display.focus_window = editor;
    check(!log.some(line => line.startsWith('recorded revert')), '9c: recorded without a paste', log);
    check(log.includes('notice: The focus changed: the transcript was copied.'), '9c: no focus notice', log);
    check(!s._reverting, '9c: the revert never ended', log);
}
print('A revert that could not paste is neither recorded nor announced as done.');
