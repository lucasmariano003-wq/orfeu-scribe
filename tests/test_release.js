// Releasing a held gesture closes the microphone even outside Shell's NORMAL mode.
import GLib from 'gi://GLib';

const [, data] = GLib.file_get_contents('extension/orfeuscribe@lucasmariano003-wq.github.io/extension.js');
const source = new TextDecoder().decode(data);
const begin = source.indexOf('    _physicalGesture(gesture) {');
const end = source.indexOf('\n    _pasteLast(', begin);
const Main = {sessionMode: {isLocked: true}, actionMode: 0};
const Shell = {ActionMode: {NORMAL: 1}};
const Subject = eval(`(class {${source.slice(begin, end)}})`);
for (const operation of ['dictation', 'ask', 'translation']) {
    const calls = [];
    const subject = new Subject();
    Object.assign(subject, {_held: true, _alive: false, _acceptPhysicalGesture: () => false,
        _call: method => calls.push(method)});
    subject._physicalGesture(`stop:${operation}`);
    if (subject._held || calls.join(',') !== 'Stop') throw Error('Release did not stop the recording');
    subject._physicalGesture(`stop:${operation}`);
    subject._physicalGesture(`start:${operation}`);
    if (calls.length !== 1) throw Error('Unexpected start or duplicate stop');
}
print('Release stops the recording in all three operations, even outside NORMAL mode.');

// An animation may complete while GNOME tears down the extension's actor.
const hideBegin = source.indexOf('    _hide() {');
const hideEnd = source.indexOf('\n    _error(', hideBegin);
const Clutter = {AnimationMode: {EASE_OUT_QUAD: 0}};
const Hiding = eval(`(class {${source.slice(hideBegin, hideEnd)}})`);
let completion;
let hidden = 0;
const pill = {ease: options => { completion = options.onComplete; },
    hide: () => { hidden++; }};
const hiding = new Hiding();
Object.assign(hiding, {_alive: true, _pill: pill});
hiding._hide();
completion();
if (hidden !== 1) throw Error('The live actor was not hidden');
hiding._alive = false;
pill.hide = () => { throw Error('Accessed disposed actor during teardown'); };
completion();
hiding._alive = true;
hiding._pill = {};
completion();
print('Pending hide animations leave disposed and replaced actors alone.');
