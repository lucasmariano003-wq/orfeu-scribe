"""Portable history: readable Markdown and reversible CSV, without audio paths."""
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
VERSION_FIELD = 'orfeuscribe_version'
FENCE = 'orfeuscribe-entry-v1'
TITLE = '# Orfeu Scribe history'


def csv_safe(value):
    # A spreadsheet must not execute a sentence that starts with =, +, - or @.
    text = str(value)
    return "'" + text if text.startswith(("'", '=', '+', '-', '@', '\t', '\r')) else text


def export_history(history, path, query='', category='all'):
    path = Path(path)
    if path.suffix.lower() not in ('.md', '.csv'):
        raise ValueError(_('Choose a .md or .csv file.'))
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
        raise ValueError(_('The copy does not have all the fields of a Orfeu Scribe entry.'))
    for key in FIELDS:
        value = record[key]
        if key in NUMBERS:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(_('Invalid timing in the copy.')) from None
            if not math.isfinite(value) or value < 0:
                raise ValueError(_('Invalid timing in the copy.'))
            record[key] = value
        elif not isinstance(value, str) and not (key in NULLABLE and value is None):
            raise ValueError(_('Invalid text in the copy.'))
    for key in ('created_at', 'edited_at'):
        if record[key] is not None:
            datetime.fromisoformat(record[key])
    return record


def import_history(history, path):
    path = Path(path)
    if path.stat().st_size > MAX_IMPORT:
        raise ValueError(_('The copy is larger than 64 MB. Import it in parts.'))
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() == '.csv':
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames != [VERSION_FIELD, *FIELDS]:
            raise ValueError(_('Choose a CSV file exported by Orfeu Scribe.'))
        records = []
        for row in reader:
            if row.pop(VERSION_FIELD) != '1':
                raise ValueError(_('Unknown copy version.'))
            row = {k: v[1:] if isinstance(v, str) and v.startswith("'") else v for k, v in row.items()}
            for key in NULLABLE:
                row[key] = json.loads(row[key])
            records.append(row)
    elif path.suffix.lower() == '.md':
        blocks = re.findall(r'^```' + re.escape(FENCE) + r'\n(.*?)\n```\s*$', text, flags=re.M | re.S)
        if not blocks and text.strip() != TITLE:
            raise ValueError(_('Choose a Markdown file exported by Orfeu Scribe.'))
        records = [json.loads(block) for block in blocks]
    else:
        raise ValueError(_('Choose a .md or .csv file.'))
    records = [validate_record(row) for row in records]
    # Everything is validated first; IDs, audio and vocabulary decisions are never imported.
    with history.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in reversed(records):
            db.execute(f"INSERT INTO dictations ({','.join(FIELDS)}) VALUES ({','.join('?' for field in FIELDS)})",
                       tuple(row[k] for k in FIELDS))
    return len(records)
