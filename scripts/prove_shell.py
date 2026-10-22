from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
OUTPUT = ROOT / '.work/shell-proof.json'


def editor(destination):
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, Gio
    app = Gtk.Application(application_id='org.example.OrfeuScribeProofEditor',
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    def activate(app):
        window = Gtk.ApplicationWindow(application=app, title='Orfeu Scribe proof editor')
        window.set_default_size(700, 500)
        view = Gtk.TextView()
        view.set_margin_top(24)
        view.set_margin_start(24)
        window.set_child(view)
        buf = view.get_buffer()
        buf.connect('changed', lambda b: Path(destination).write_text(
            b.get_text(b.get_start_iter(), b.get_end_iter(), True)))
        window.present()
        view.grab_focus()
    app.connect('activate', activate)
    app.run([])


def worker(base):
    if (os.environ.get('ORFEUSCRIBE_PROOF') != '1'
            or Path(os.environ.get('HOME', '')) != base / 'home'
            or Path(os.environ.get('XDG_RUNTIME_DIR', '')) != base / 'run'):
        raise SystemExit('Refusing to run outside a private proof environment')
    sys.path.insert(0, str(ROOT))
    import gi
    gi.require_version('Gio', '2.0')
    from gi.repository import Gio, GLib
    from tests.fake_openai import serve
    from orfeuscribe.config import Config
    processes = []
    logs = []
    server = serve()
    server.state.text = 'this is a synthetic dictation'
    server.state.reply = 'This is a synthetic dictation.'
    os.environ['ORFEUSCRIBE_STT_API_KEY'] = 'synthetic-proof-only'
    os.environ['ORFEUSCRIBE_PROOF'] = '1'
    os.environ['ORFEUSCRIBE_TEST_SOURCE'] = 'audiotestsrc is-live=true wave=sine freq=330 volume=0.1'
    Config().update(stt_base_url=server.url, stt_model='fake-transcribe',
                    revision_model='fake-chat', input_language='en', sounds=False)
    subprocess.run([sys.executable, str(ROOT / 'scripts/install.py'), 'install',
                    '--home', os.environ['HOME']], check=True)
    extensions = Path(os.environ['XDG_DATA_HOME']) / 'gnome-shell/extensions'
    driver = extensions / 'orfeuscribe-proof@local'
    driver.mkdir()
    (driver / 'metadata.json').write_text(json.dumps({'uuid': driver.name,
        'name': 'Isolated Orfeu Scribe proof driver', 'description': 'Private test only',
        'shell-version': ['51']}))
    (driver / 'extension.js').write_text('''import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
export default class extends Extension {
 enable() { if (GLib.getenv('ORFEUSCRIBE_PROOF') !== '1') throw Error('isolated proof only');
 this.previous = global.context.unsafe_mode; global.context.unsafe_mode = true; }
 disable() { global.context.unsafe_mode = this.previous; }
}
''')
    subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'enabled-extensions',
                    "['orfeuscribe@lucasmariano003-wq.github.io','orfeuscribe-proof@local']"], check=True)
    def launch(command, name):
        log = (ROOT / '.work' / name).open('w')
        logs.append(log)
        p = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
        processes.append(p)
        return p
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    def evaluate(script):
        ok, value = bus.call_sync('org.gnome.Shell', '/org/gnome/Shell', 'org.gnome.Shell',
            'Eval', GLib.Variant('(s)', (script,)), GLib.VariantType.new('(bs)'),
            Gio.DBusCallFlags.NONE, 2000, None).unpack()
        if not ok:
            raise RuntimeError(value)
        return value
    def wait_for(action, seconds=30):
        end = time.monotonic() + seconds
        last = None
        while time.monotonic() < end:
            try:
                value = action()
                if value:
                    return value
            except Exception as error:
                last = str(error)
            time.sleep(.3)
        raise RuntimeError('Proof timed out: ' + str(last))
    record = {'status': 'failed', 'synthetic_audio': True, 'real_microphone': False,
              'external_service': False, 'nested_shell': True}
    try:
        shell = launch(['gnome-shell', '--headless', '--wayland', '--virtual-monitor',
                        '1280x900', '--no-x11', '--wayland-display', 'orfeuscribe-proof',
                        '--debug-control'], 'shell-proof-shell.log')
        wait_for(lambda: 'true' in evaluate('global.context.unsafe_mode'))
        expression = "Main.extensionManager.lookup('orfeuscribe@lucasmariano003-wq.github.io')"
        state = wait_for(lambda: evaluate('JSON.stringify(' + expression +
                         ' && {state:' + expression + '.state,error:' + expression + '.error})'))
        record['extension'] = state
        parsed_state = json.loads(state)
        if isinstance(parsed_state, str):
            parsed_state = json.loads(parsed_state)
        record['extension'] = parsed_state
        if parsed_state.get('state') != 1:
            raise RuntimeError('Extension not enabled: ' + state)
        text_path = base / 'editor.txt'
        text_path.write_text('')
        launch([sys.executable, str(SELF), '--editor', str(text_path)], 'shell-proof-editor.log')
        record['stage'] = 'waiting_for_editor'
        wait_for(lambda: 'true' in evaluate("global.get_window_actors().some(a => a.meta_window.get_title() === 'Orfeu Scribe proof editor')"))
        evaluate("Main.overview.hide(); global.get_window_actors().find(a => a.meta_window.get_title() === 'Orfeu Scribe proof editor').meta_window.activate(global.get_current_time()); true")
        wait_for(lambda: 'true' in evaluate("global.display.focus_window?.get_title() === 'Orfeu Scribe proof editor'"))
        record['stage'] = 'waiting_for_service'
        wait_for(lambda: 'true' in evaluate('Boolean(' + expression + '.stateObj._proxy?.g_name_owner)'))
        record['stage'] = 'waiting_for_recording'
        evaluate(expression + ".stateObj._begin('dictation', false); true")
        wait_for(lambda: any(state in evaluate(expression + '.stateObj._state') for state in ('listening', 'error')))
        if 'error' in evaluate(expression + '.stateObj._state'):
            raise RuntimeError('Recording failed: ' + evaluate(expression + '.stateObj._label.text'))
        time.sleep(1)
        evaluate(expression + ".stateObj._call('Stop'); true")
        record['stage'] = 'waiting_for_paste'
        pasted = wait_for(lambda: text_path.read_text() == server.state.reply, seconds=20)
        record.update(status='passed', stage='complete', pasted_text=text_path.read_text(),
                      transcription_requests=len(server.state.transcriptions),
                      revision_requests=len(server.state.chats))
    except Exception as error:
        record['error'] = str(error)
        try:
            record['debug'] = evaluate("JSON.stringify({windows:global.get_window_actors().map(a=>({title:a.meta_window.get_title(),cls:a.meta_window.get_wm_class()})),state:" + expression + ".stateObj._state,label:" + expression + ".stateObj._label.text,owner:" + expression + ".stateObj._proxy?.g_name_owner,target:global.display.focus_window?.get_title()})")
        except Exception as extra:
            record['debug_error'] = str(extra)
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                try: os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
        server.shutdown()
        server.server_close()
        for log in logs:
            log.close()
        shell_diagnostics = (ROOT / '.work/shell-proof-shell.log').read_text(errors='replace')
        record['gjs_critical_count'] = shell_diagnostics.count('Gjs-CRITICAL')
        if record['gjs_critical_count']:
            record['status'] = 'failed'
            record['error'] = 'GNOME Shell reported JavaScript critical errors during the proof'
        OUTPUT.write_text(json.dumps(record, indent=2))
        print(json.dumps(record), flush=True)
    return 0 if record['status'] == 'passed' else 1


def cleanup_document_mount(base):
    # The private portal may leave a dead FUSE mount after its bus exits.
    # Only detach the mount inside this proof's freshly-created directory.
    target = str(base / 'run/doc')
    for _attempt in range(20):
        mounts = {line.split()[4] for line in Path('/proc/self/mountinfo').read_text().splitlines()}
        if target not in mounts:
            return
        subprocess.run(['fusermount3', '-u', target], capture_output=True)
        time.sleep(.1)
    raise RuntimeError('The private document portal mount could not be removed')


def main():
    if '--editor' in sys.argv:
        return editor(sys.argv[-1])
    if '--worker' in sys.argv:
        return worker(Path(sys.argv[-1]))
    OUTPUT.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='orfeuscribe-shell-') as tmp:
        base = Path(tmp)
        for name in ('home','run','config','data','cache','state'):
            (base / name).mkdir(mode=0o700)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(base / 'home'),
               'XDG_RUNTIME_DIR': str(base / 'run'),
               'XDG_CONFIG_HOME': str(base / 'config'),
               'XDG_DATA_HOME': str(base / 'home/.local/share'),
               'XDG_CACHE_HOME': str(base / 'cache'), 'XDG_STATE_HOME': str(base / 'state'),
               'GSETTINGS_BACKEND': 'keyfile', 'GIO_USE_VFS': 'local',
               'ORFEUSCRIBE_SECRET_BACKEND': 'memory', 'ORFEUSCRIBE_PROOF': '1',
               'WAYLAND_DISPLAY': 'orfeuscribe-proof', 'GDK_BACKEND': 'wayland',
               'GTK_A11Y': 'none', 'NO_AT_BRIDGE': '1',
               'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1',
               'LANG': 'C.UTF-8', 'LIBGL_ALWAYS_SOFTWARE': '1'}
        try:
            status = subprocess.run(['dbus-run-session','--',sys.executable,str(SELF),
                                     '--worker',str(base)],env=env,cwd=ROOT).returncode
        finally:
            cleanup_document_mount(base)
    if OUTPUT.exists():
        result = json.loads(OUTPUT.read_text())
        result['test_environment_cleaned'] = True
        OUTPUT.write_text(json.dumps(result, indent=2))
    return status


if __name__ == '__main__':
    sys.exit(main())
