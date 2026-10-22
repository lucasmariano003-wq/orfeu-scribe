"""Local history of dictations and optional audio retention."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from contextlib import contextmanager, closing
import os
import fcntl
import tempfile
from pathlib import Path
import sqlite3

from .config import DATA_DIR, private_dir
from .i18n import _
from .learning import bounded_examples, extract_swaps, pair_key

FILENAME = "history.db"
WRITTEN_MODES = ('dictation', 'message', 'email', 'prompt', 'literal', 'translation', 'outline', 'script',
                 'file', 'edit', 'write')
STYLE_MODES = ('dictation', 'message', 'email', 'prompt', 'outline', 'script')


class History:
    def __init__(self, path: Path = DATA_DIR / FILENAME):
        self.path = path
        private_dir(path.parent)
        self.last_backup = None
        # flock covers inspection, backup and migration across processes and threads.
        fd = os.open(path.with_suffix(path.suffix + ".migration.lock"),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._migrate()
        finally:
            os.close(fd)
        path.chmod(0o600)

    def _migrate(self):
        additions = (("edited", "TEXT"), ("edited_at", "TEXT"),
                     ("error", "TEXT NOT NULL DEFAULT ''"), ("action", "TEXT NOT NULL DEFAULT 'deliver'"),
                     ("language", "TEXT NOT NULL DEFAULT ''"), ("web", "TEXT NOT NULL DEFAULT ''"),
                     ("input_language", "TEXT NOT NULL DEFAULT 'en'"),
                     ("note_folder", "TEXT NOT NULL DEFAULT ''"))
        existed = self.path.exists() and self.path.stat().st_size > 0
        # Create the private file BEFORE SQLite opens it.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(dictations)")}
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = any(name not in columns for name, _definition in additions) or not {
                'dictations', 'suggestions', 'suggestion_evidence'} <= tables
            if existed and missing:
                # No backup on the same connection inside BEGIN IMMEDIATE: the API would
                # wait for its own transaction. The external lock serializes this.
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                backup_fd, name = tempfile.mkstemp(prefix=f'{self.path.stem}-before-{stamp}-', suffix='.db',
                                                   dir=self.path.parent)
                os.close(backup_fd)
                backup = Path(name)
                try:
                    with closing(sqlite3.connect(backup)) as target:
                        db.backup(target)
                        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise sqlite3.DatabaseError('Invalid backup.')
                    with backup.open('rb') as handle:
                        os.fsync(handle.fileno())
                    directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    self.last_backup = backup
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
            # ALTER TABLE is transactional too. Serializes concurrent migrations.
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS dictations (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                app TEXT NOT NULL, title TEXT NOT NULL, mode TEXT NOT NULL,
                raw TEXT NOT NULL, final TEXT NOT NULL, provider TEXT NOT NULL,
                model TEXT NOT NULL, recording REAL NOT NULL, stt REAL NOT NULL,
                polishing REAL NOT NULL, total REAL NOT NULL, audio_path TEXT)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(dictations)")}
            for name, definition in additions:
                if name not in columns:
                    db.execute(f"ALTER TABLE dictations ADD COLUMN {name} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY, wrong TEXT NOT NULL, right TEXT NOT NULL,
                wrong_key TEXT NOT NULL, right_key TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','accepted','rejected')),
                UNIQUE(wrong_key, right_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS suggestion_evidence (
                dictation_id INTEGER NOT NULL REFERENCES dictations(id),
                suggestion_id INTEGER NOT NULL REFERENCES suggestions(id),
                PRIMARY KEY(dictation_id, suggestion_id))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, *, app, title, mode, raw, final, provider, model, recording, stt,
            polishing, total, audio_path=None, error="", action="deliver", language="", web="",
            input_language="en", note_folder=""):
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO dictations
                (created_at,app,title,mode,raw,final,provider,model,recording,stt,polishing,total,audio_path,
                 error,action,language,web,input_language,note_folder)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.now(timezone.utc).isoformat(), app, title, mode, raw, final,
                 provider, model, recording, stt, polishing, total, audio_path, error, action, language, web,
                 input_language, note_folder))
            return cursor.lastrowid

    def search(self, query="", limit=100, category="all"):
        written = ",".join(f"'{mode}'" for mode in WRITTEN_MODES)
        with self.connect() as db:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            needle = f"%{escaped}%"
            return [dict(r) for r in db.execute(rf"""SELECT * FROM dictations
                WHERE (?='all' OR (?='questions' AND mode='question') OR (?='dictations' AND mode IN ({written})))
                  AND (raw LIKE ? ESCAPE '\' OR final LIKE ? ESCAPE '\'
                    OR edited LIKE ? ESCAPE '\' OR app LIKE ? ESCAPE '\')
                ORDER BY id DESC LIMIT ?""", (category, category, category, needle, needle, needle, needle,
                                              -1 if limit is None else limit))]

    @staticmethod
    def final_text(row):
        return row.get("edited") if row.get("edited") is not None else row["final"]

    def style_examples(self, mode, enabled=True, exclude_id=None, language=None):
        """Earlier texts as style references: hand edits first, then recent ones of the same mode."""
        if not enabled or mode == "literal":
            return ()
        modes = ",".join(f"'{name}'" for name in STYLE_MODES)
        with self.connect() as db:
            rows = db.execute(f"""SELECT edited, final FROM dictations
                WHERE mode IN ({modes}) AND (? IS NULL OR input_language=?) AND error = '' AND trim(final) != ''
                  AND action != 'raw'
                  AND (? IS NULL OR id != ?)
                  AND (trim(coalesce(edited,'')) != '' OR mode = ?)
                ORDER BY (trim(coalesce(edited,'')) != '') DESC,
                         created_at DESC, id DESC""", (language, language, exclude_id, exclude_id, mode))
            return bounded_examples(self.final_text(dict(row)) for row in rows if row['final'].strip())

    def amend(self, ident, edited):
        if not edited.strip():
            raise ValueError(_("Write the correction before saving."))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM dictations WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError(_("This entry is no longer in the history."))
            if row['edited'] == edited:
                return
            # A hand edit is the user's own writing again, even on an entry that had
            # been reverted to the transcript: it counts as a style example again.
            db.execute("UPDATE dictations SET edited=?, edited_at=?, action=CASE WHEN action='raw' THEN 'deliver' "
                       "ELSE action END WHERE id=?", (edited, datetime.now(timezone.utc).isoformat(), ident))
            # The same swap in the transcript and in the revision, or several clicks on
            # Save, counts once per entry. Editing again reviews the evidence.
            pairs = {}
            for original in (row['raw'], row['final']):
                for wrong, right in extract_swaps(original, edited):
                    pairs[pair_key(wrong, right)] = (wrong, right)
            db.execute("DELETE FROM suggestion_evidence WHERE dictation_id=?", (ident,))
            for key, (wrong, right) in pairs.items():
                db.execute("""INSERT INTO suggestions(wrong,right,wrong_key,right_key)
                    VALUES (?,?,?,?) ON CONFLICT(wrong_key,right_key) DO NOTHING""",
                           (wrong, right, *key))
                suggestion = db.execute("SELECT id FROM suggestions WHERE wrong_key=? AND right_key=?", key).fetchone()[0]
                db.execute("INSERT INTO suggestion_evidence VALUES (?,?)", (ident, suggestion))
            db.execute("""UPDATE suggestions SET count=(SELECT count(*) FROM suggestion_evidence
                          WHERE suggestion_id=suggestions.id)""")

    def revert_to_raw(self, ident, text):
        """Records "Revert to transcript" as an edit, without learning from it.

        The raw speech is neither the user's style nor a vocabulary correction: the
        entry leaves the style examples (action='raw') and loses its suggestion
        evidence, so the revision→transcript swap never becomes a "right => wrong" rule.
        """
        if not text.strip():
            raise ValueError(_("The transcript is empty."))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM dictations WHERE id=?", (ident,)).fetchone() is None:
                raise ValueError(_("This entry is no longer in the history."))
            db.execute("UPDATE dictations SET edited=?, edited_at=?, action='raw' WHERE id=?",
                       (text, datetime.now(timezone.utc).isoformat(), ident))
            db.execute("DELETE FROM suggestion_evidence WHERE dictation_id=?", (ident,))
            db.execute("""UPDATE suggestions SET count=(SELECT count(*) FROM suggestion_evidence
                          WHERE suggestion_id=suggestions.id)""")

    def pending_suggestions(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM suggestions
                WHERE status='pending' AND count >= 2 ORDER BY count DESC, id""")]

    def reject_suggestion(self, ident):
        with self.connect() as db:
            db.execute("UPDATE suggestions SET status='rejected' WHERE id=? AND status='pending'", (ident,))

    def accept_suggestion(self, ident, vocabulary_path):
        from . import config
        from .vocabulary import parse_vocabulary
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM suggestions WHERE id=? AND status='pending' AND count>=2", (ident,)).fetchone()
            if row is None:
                return False
            text = vocabulary_path.read_text(encoding='utf-8') if vocabulary_path.exists() else ""
            existing = {pair_key(a, b) for a, b in parse_vocabulary(text).corrections}
            if (row['wrong_key'], row['right_key']) not in existing:
                text = (text.rstrip() + "\n" if text.strip() else "") + f"{row['wrong']} => {row['right']}\n"
                # The atomic file first, then the state. If something fails in between,
                # accepting again does not duplicate the line.
                config.write_private(vocabulary_path, text)
            db.execute("UPDATE suggestions SET status='accepted' WHERE id=?", (ident,))
            return True

    def last_dictation(self):
        written = ",".join(f"'{mode}'" for mode in WRITTEN_MODES)
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM dictations WHERE mode IN ({written}) AND error='' AND final!='' "
                             "ORDER BY id DESC LIMIT 1").fetchone()
            return self.final_text(dict(row)) if row else ""

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM dictations WHERE id=?", (ident,)).fetchone()
            return dict(row) if row else None

    def retain_audio(self, ident: int, audio: bytes, keep: int):
        audio_dir = self.path.parent / "audio"
        if keep > 0:
            private_dir(audio_dir)
            path = audio_dir / f"dictation-{ident}.wav"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(audio)
            with self.connect() as db:
                db.execute("UPDATE dictations SET audio_path=? WHERE id=?", (str(path), ident))
        self.prune_audio(keep)

    def prune_audio(self, keep: int):
        with self.connect() as db:
            rows = db.execute("SELECT id,audio_path FROM dictations WHERE audio_path IS NOT NULL ORDER BY id DESC").fetchall()
            for row in rows[max(0, keep):]:
                path = Path(row["audio_path"])
                if path.parent == self.path.parent / "audio":
                    path.unlink(missing_ok=True)
                db.execute("UPDATE dictations SET audio_path=NULL WHERE id=?", (row["id"],))

    def delete(self, ident, created_at=None):
        """Deletes the entry, its own audio and evidence; never follows imported paths."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT audio_path FROM dictations WHERE id=? AND (? IS NULL OR created_at=?)",
                             (ident, created_at, created_at)).fetchone()
            if row is None:
                return False
            if row['audio_path']:
                path = Path(row['audio_path'])
                expected = self.path.parent / 'audio' / f'dictation-{ident}.wav'
                if path == expected:
                    path.unlink(missing_ok=True)
            db.execute("DELETE FROM suggestion_evidence WHERE dictation_id=?", (ident,))
            db.execute("DELETE FROM dictations WHERE id=?", (ident,))
            db.execute("UPDATE suggestions SET count=(SELECT count(*) FROM suggestion_evidence WHERE suggestion_id=suggestions.id)")
            db.execute("DELETE FROM suggestions WHERE count=0 AND status='pending'")
        return True

    def prune_days(self, days, now=None):
        if days <= 0:
            return 0
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
        with self.connect() as db:
            ids = list(db.execute("SELECT id,created_at FROM dictations WHERE julianday(created_at) < julianday(?)",
                                  (cutoff,)))
        for ident, created_at in ids:
            self.delete(ident, created_at)
        return len(ids)
