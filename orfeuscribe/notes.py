"""Append-only notes in a Markdown folder chosen by the user; existing pages are never replaced."""
from datetime import datetime
import fcntl
import os
from pathlib import Path
import re
import stat

from .i18n import _


def valid_folder(name):
    return (isinstance(name, str) and bool(name.strip()) and name == name.strip()
            and len(name) <= 120 and '..' not in name
            and not any(c in name for c in '/\\\x00\r\n') and name not in ('.',))


def append_note(text, context, directory, now=None, *, heading="Inbox"):
    """O_APPEND + flock + dir_fd: appends without following links or swapping the directory."""
    if not text.strip():
        raise ValueError(_('The note is empty.'))
    now = (now or datetime.now().astimezone())
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = now.strftime('%Y-%m-%d') + '.md'
    lock = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=lock)
            created = True
        except FileExistsError:
            fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=lock)
            created = False
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError(_('The destination is not a regular note file.'))
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            header = f'# {heading} — {now:%Y-%m-%d}\n\n' if created else ''
            size = os.fstat(stream.fileno()).st_size
            separator = '\n' if size and os.pread(stream.fileno(), 1, size - 1) != b'\n' else ''
            app = ' '.join(context.get('wm_class', '').split()) or 'Orfeu Scribe'
            body = text.strip().replace('\x00', '').replace('\n', '\n  ')
            stream.write(header + separator + f'- {now:%H:%M} · {app}: {body}\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(lock)
    finally:
        os.close(lock)
    return directory / name


def link_vocabulary(text, vocab, root):
    """Links only names that already have a page, without following links or doubling existing links."""
    existing = set()
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith('.') and not (Path(base) / d).is_symlink()]
        for name in files:
            path = Path(base) / name
            if path.suffix.lower() == '.md' and not path.is_symlink():
                existing.add(path.stem)
    terms = [term for term in vocab.terms if term in existing and not any(c in term for c in '[]|#\\/')]
    if not terms:
        return text
    pattern = re.compile(r'(?<![\w])(' + '|'.join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r')(?![\w])')
    pieces = re.split(r'(\[\[.*?\]\])', text, flags=re.S)
    return ''.join(part if i % 2 else pattern.sub(lambda m: '[[' + m[0] + ']]', part)
                   for i, part in enumerate(pieces))


def append_folder_note(text, context, folder, allowed, vocab, root, *, wikilinks=False, now=None):
    if not root:
        raise ValueError(_('No notes folder is configured. The text stayed in the history.'))
    folder = folder or allowed[0]
    if not valid_folder(folder) or folder not in allowed:
        raise ValueError(_('That folder is not in the allowed list in Settings. The note stayed in the history.'))
    root = Path(root).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ValueError(_('The configured notes folder is not available.'))
    directory = root / folder
    if directory.is_symlink():
        raise ValueError(_('The notes subfolder cannot be a link.'))
    body = link_vocabulary(text, vocab, root) if wikilinks else text
    return append_note(body, context, directory, now, heading=folder)
