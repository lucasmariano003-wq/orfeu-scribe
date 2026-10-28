"""Portable history: readable Markdown and reversible CSV, without audio paths."""
from contextlib import contextmanager
import csv
from datetime import datetime
import io
import json
import math
from pathlib import Path
import re

from .config import write_private
from .i18n import _

FIELDS = ('created_at', 'app', 'title', 'mode', 'raw', 'final', 'provider', 'model',
          'recording', 'stt', 'polishing', 'total', 'edited', 'edited_at', 'error',
          'action', 'language', 'web', 'input_language', 'note_folder')
NUMBERS = ('recording', 'stt', 'polishing', 'total')
NULLABLE = ('edited', 'edited_at')
MAX_IMPORT = 64 * 1024 * 1024
# The csv module refuses fields over 128 KiB by default; an export can hold longer
# texts. Any field of a file the importer accepts fits in the file's own size limit.
FIELD_LIMIT = MAX_IMPORT
VERSION_FIELD = 'orfeuscribe_version'
FENCE = 'orfeuscribe-entry-v1'
TITLE = '# Orfeu Scribe history'


class CopyError(ValueError):
    """A copy that cannot be written or imported; the message is translated and safe to show."""


@contextmanager
def _field_limit(limit):
    previous = csv.field_size_limit()
    csv.field_size_limit(limit)
    try:
        yield
    finally:
        csv.field_size_limit(previous)


def csv_safe(value):
    # A spreadsheet must not execute a sentence that starts with =, +, - or @.
    text = str(value)
    return "'" + text if text.startswith(("'", '=', '+', '-', '@', '\t', '\r')) else text


def export_history(history, path, query='', category='all'):
    path = Path(path)
    if path.suffix.lower() not in ('.md', '.csv'):
        raise CopyError(_('Choose a .md or .csv file.'))
    rows = [{key: row.get(key) for key in FIELDS} for row in history.search(query, limit=None, category=category)]
    if path.suffix.lower() == '.csv':
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=(VERSION_FIELD, *FIELDS), lineterminator='\n')
        writer.writeheader()
        for row in rows:
            # JSON in the two nullable fields tells "no edit" apart from an empty edit.
            record = {k: csv_safe(json.dumps(v, ensure_ascii=False) if k in NULLABLE else v) for k, v in row.items()}
            writer.writerow({VERSION_FIELD: '1', **record})
        text = stream.getvalue()
    else:
        pages = [TITLE + '\n']
        for row in rows:
            visible = '\n'.join('> ' + line if line else '>' for line in history.final_text(row).splitlines())
            pages.append(f"\n## {row['created_at']} · {row['mode']}\n\n{visible}\n\n")
            pages.append(f'```{FENCE}\n' + json.dumps(row, ensure_ascii=False, indent=2) + '\n```\n')
        text = ''.join(pages)
    write_private(path, text)
    return len(rows)


def validate_record(record):
    if set(record) != set(FIELDS):
        raise CopyError(_('The copy does not have all the fields of a Orfeu Scribe entry.'))
    for key in FIELDS:
        value = record[key]
        if key in NUMBERS:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise CopyError(_('Invalid timing in the copy.')) from None
            if not math.isfinite(value) or value < 0:
                raise CopyError(_('Invalid timing in the copy.'))
            record[key] = value
        elif not isinstance(value, str) and not (key in NULLABLE and value is None):
            raise CopyError(_('Invalid text in the copy.'))
    for key in ('created_at', 'edited_at'):
        if record[key] is not None:
            try:
                datetime.fromisoformat(record[key])
            except ValueError:
                raise CopyError(_('Invalid date in the copy.')) from None
    return record


def _csv_records(text):
    with _field_limit(FIELD_LIMIT):
        reader = csv.DictReader(io.StringIO(text, newline=''))
        if reader.fieldnames != [VERSION_FIELD, *FIELDS]:
            raise CopyError(_('Choose a CSV file exported by Orfeu Scribe.'))
        records = []
        for row in reader:
            if row.pop(VERSION_FIELD) != '1':
                raise CopyError(_('Unknown copy version.'))
            row = {k: v[1:] if isinstance(v, str) and v.startswith("'") else v for k, v in row.items()}
            for key in NULLABLE:
                row[key] = json.loads(row[key])
            records.append(row)
        return records


def import_history(history, path):
    path = Path(path)
    if path.stat().st_size > MAX_IMPORT:
        raise CopyError(_('The copy is larger than 64 MB. Import it in parts.'))
    suffix = path.suffix.lower()
    if suffix not in ('.csv', '.md'):
        raise CopyError(_('Choose a .md or .csv file.'))
    try:
        if suffix == '.csv':
            # newline='': a \r or \r\n inside a quoted field is text and must come back unchanged.
            with path.open(encoding='utf-8-sig', newline='') as handle:
                records = _csv_records(handle.read())
        else:
            text = path.read_text(encoding='utf-8-sig')
            blocks = re.findall(r'^```' + re.escape(FENCE) + r'\n(.*?)\n```\s*$', text, flags=re.M | re.S)
            if not blocks and text.strip() != TITLE:
                raise CopyError(_('Choose a Markdown file exported by Orfeu Scribe.'))
            records = [json.loads(block) for block in blocks]
        records = [validate_record(row) for row in records]
    except CopyError:
        raise
    except (csv.Error, ValueError, TypeError, KeyError, AttributeError):
        # Parser details (and fragments of the file) never reach the interface.
        raise CopyError(_('The copy is damaged or was not exported by Orfeu Scribe.')) from None
    # Everything is validated first; IDs, audio and vocabulary decisions are never imported.
    with history.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in reversed(records):
            db.execute(f"INSERT INTO dictations ({','.join(FIELDS)}) VALUES ({','.join('?' for field in FIELDS)})",
                       tuple(row[k] for k in FIELDS))
    return len(records)
