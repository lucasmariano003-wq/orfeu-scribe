"""Confirms only the focused field's selection, with a timeout outside the GTK process."""
import json
import os
import subprocess
import sys

from .config import ROOT


def snapshot(pid, selected, operation="ask"):
    # Only a question about the selection can end in a replacement. Plain
    # dictation and translation never wait for AT-SPI: listening starts at once.
    if operation != "ask" or not pid or not selected:
        return None
    try:
        result = subprocess.run(['/usr/bin/python3', '-m', 'orfeuscribe.selection'],
                                input=json.dumps({'pid': pid, 'selected': selected}), capture_output=True,
                                text=True, timeout=2, cwd=ROOT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def inspect(pid, selected):
    import gi
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi
    Atspi.set_timeout(150, 300)
    desktop = Atspi.get_desktop(0)
    queue = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app.get_process_id() == pid:
            queue.append((app, [i]))
    count = 0
    while queue and count < 500:
        node, path = queue.pop(0)
        count += 1
        states = node.get_state_set()
        if states.contains(Atspi.StateType.FOCUSED) and states.contains(Atspi.StateType.EDITABLE):
            text = node.get_text_iface()
            if text and Atspi.Text.get_n_selections(text) == 1:
                span = Atspi.Text.get_selection(text, 0)
                if Atspi.Text.get_text(text, span.start_offset, span.end_offset) == selected:
                    return {'pid': pid, 'path': path, 'start': span.start_offset, 'end': span.end_offset,
                            'text': selected}
        for i in range(min(node.get_child_count(), 200)):
            child = node.get_child_at_index(i)
            if child:
                queue.append((child, path + [i]))
    return None


if __name__ == '__main__':
    try:
        args = json.load(sys.stdin)
        print(json.dumps(inspect(args['pid'], args['selected'])))
    except Exception:  # noqa: BLE001
        print('null')
