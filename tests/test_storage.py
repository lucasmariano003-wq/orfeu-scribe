"""History, migration, suggestions, export/import, notes and rescue — synthetic data in temporary folders."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from orfeuscribe.config import Config
from orfeuscribe.exchange import export_history, import_history
from orfeuscribe.history import History
from orfeuscribe.learning import extract_swaps
from orfeuscribe.notes import append_folder_note, link_vocabulary
from orfeuscribe.rescue import RescueStore, TTL
from orfeuscribe.vocabulary import load_vocabulary, parse_vocabulary
from tests.support import wav

# An older table layout, used to prove that migrations keep every field.
OLD_SCHEMA = '''CREATE TABLE dictations (
 id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, app TEXT NOT NULL,
 title TEXT NOT NULL, mode TEXT NOT NULL, raw TEXT NOT NULL, final TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL, recording REAL NOT NULL,
 stt REAL NOT NULL, polishing REAL NOT NULL, total REAL NOT NULL, audio_path TEXT)'''


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.history = History(self.root / "history.db")
        self.vocab_path = self.root / "vocabulary.txt"
        self.vocab_path.write_text("# My words\nZephyrine\n")

    def add(self, final="A text.", mode="dictation", history=None, **kwargs):
        return (history or self.history).add(app="editor", title="note", mode=mode, raw=kwargs.pop("raw", final),
                                             final=final, provider="test", model="test", recording=1, stt=2,
                                             polishing=3, total=6, **kwargs)

    def suggest(self, wrong="Zefirin", right="Zephyrine"):
        for n in range(2):
            ident = self.add(f"I read {wrong} today, volume {n}.")
            self.history.amend(ident, f"I read {right} today, volume {n}.")
        return self.history.pending_suggestions()[0]["id"]


class HistoryTests(Base):
    def test_search_audio_and_permissions(self):
        first, second = self.add("100% Zephyrine"), self.add("Another one")
        self.assertEqual(self.history.search("Zephyrine")[0]["id"], first)
        self.assertEqual(len(self.history.search("%")), 1)
        self.assertEqual(self.history.path.stat().st_mode & 0o777, 0o600)
        self.history.retain_audio(first, b"first", 1)
        self.history.retain_audio(second, b"second", 1)
        self.assertIsNone(self.history.get(first)["audio_path"])
        self.assertEqual(Path(self.history.get(second)["audio_path"]).read_bytes(), b"second")
        self.history.prune_audio(0)
        self.assertEqual(list((self.root / "audio").iterdir()), [])

    def test_migration_keeps_fields_backs_up_once_and_is_idempotent(self):
        path = self.root / "old.db"
        row = (37, "2026-01-01", "editor", "Title", "dictation", "Uh, raw.", "Final.", "svc", "m", 1.5, 2.5, 3.5, 7.5,
               "/audio/kept.wav")
        with closing(sqlite3.connect(path)) as db:
            db.execute(OLD_SCHEMA)
            db.execute("INSERT INTO dictations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            db.commit()
        with ThreadPoolExecutor(max_workers=4) as pool:
            histories = list(pool.map(lambda _n: History(path), range(4)))
        backups = list(self.root.glob("old-before-*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        record = histories[0].get(37)
        self.assertEqual(tuple(record.values())[:14], row)
        self.assertIsNone(record["edited"])
        with histories[0].connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(History(path).last_backup)

    def test_failed_migration_rolls_back(self):
        path = self.root / "old.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute(OLD_SCHEMA)
        original = History.connect

        @contextmanager
        def deny(instance):
            with original(instance) as db:
                db.set_authorizer(lambda action, a, *_rest: sqlite3.SQLITE_DENY
                                  if action == sqlite3.SQLITE_CREATE_TABLE and a == "suggestions" else sqlite3.SQLITE_OK)
                yield db
        with patch.object(History, "connect", deny):
            with self.assertRaises(sqlite3.DatabaseError):
                History(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(len(db.execute("PRAGMA table_info(dictations)").fetchall()), 14)

    def test_amend_revert_and_style_examples(self):
        ident = self.add("I read Zephyrine.", raw="uh, I read zefirin.")
        self.history.amend(ident, "I read Zephyrine calmly.")
        self.assertEqual(History.final_text(self.history.get(ident)), "I read Zephyrine calmly.")
        with self.assertRaises(ValueError):
            self.history.amend(ident, " \n ")
        other = self.add("Revised page.")
        self.history.revert_to_raw(other, "revised page")
        row = self.history.get(other)
        self.assertEqual((row["edited"], row["action"]), ("revised page", "raw"))
        self.assertNotIn("revised page", self.history.style_examples("dictation"))
        self.history.amend(other, "Written by hand.")
        self.assertIn("Written by hand.", self.history.style_examples("dictation"))
        with self.assertRaises(ValueError):
            self.history.revert_to_raw(999, "x")

    def test_style_examples_priority_language_and_exclusions(self):
        approved = self.add("Old other mode", "email")
        self.history.amend(approved, "Edited other mode")
        self.add("Recent message", "message")
        self.add("First dictation")
        self.add("Second dictation")
        self.add("Primeiro ditado", input_language="pt")
        self.assertEqual(self.history.style_examples("dictation", language="en"),
                         ("Edited other mode", "Second dictation", "First dictation"))
        self.assertEqual(self.history.style_examples("dictation", language="pt"), ("Primeiro ditado",))
        literal = self.add("Literal", "literal")
        self.history.amend(literal, "Literal approved")
        failed = self.add("Failed text", error="Did not finish")
        self.history.amend(failed, "Failed but edited")
        self.assertNotIn("Literal approved", self.history.style_examples("dictation"))
        self.assertNotIn("Failed but edited", self.history.style_examples("dictation"))
        self.assertEqual(self.history.style_examples("literal"), ())
        for n in range(9):
            self.add(f"{n}" + "á" * 499)
        examples = self.history.style_examples("dictation", language="en")
        self.assertEqual(len(examples), 5)
        self.assertLessEqual(sum(map(len, examples)), 2500)

    def test_suggestions_need_two_entries_and_explicit_acceptance(self):
        first = self.add("I read Zefirin.")
        self.history.amend(first, "I read Zephyrine.")
        self.history.amend(first, "I read Zephyrine.")
        self.assertEqual(self.history.pending_suggestions(), [])
        second = self.add("I like Zefirin.")
        self.history.amend(second, "I like Zephyrine.")
        pending = self.history.pending_suggestions()
        self.assertEqual([(p["wrong"], p["right"], p["count"]) for p in pending], [("Zefirin", "Zephyrine", 2)])
        self.assertEqual(self.vocab_path.read_text(), "# My words\nZephyrine\n")
        self.assertTrue(self.history.accept_suggestion(pending[0]["id"], self.vocab_path))
        self.assertFalse(self.history.accept_suggestion(pending[0]["id"], self.vocab_path))
        self.assertEqual(self.vocab_path.read_text().count("Zefirin => Zephyrine"), 1)
        self.assertEqual(self.vocab_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(parse_vocabulary(self.vocab_path.read_text()).correct("I read Zefirin."), "I read Zephyrine.")

    def test_accept_into_missing_vocabulary_and_failed_write(self):
        ident = self.suggest()
        missing = self.root / "new" / "vocabulary.txt"
        with patch("orfeuscribe.config.write_private", side_effect=OSError("test")):
            with self.assertRaises(OSError):
                self.history.accept_suggestion(ident, missing)
        self.assertEqual(self.history.pending_suggestions()[0]["id"], ident)
        self.assertTrue(self.history.accept_suggestion(ident, missing))
        self.assertEqual(missing.read_text(), "Zefirin => Zephyrine\n")

    def test_rejection_persists(self):
        ident = self.suggest()
        self.history.reject_suggestion(ident)
        third = self.add("I meditate with Zefirin.")
        History(self.history.path).amend(third, "I meditate with Zephyrine.")
        self.assertEqual(self.history.pending_suggestions(), [])

    def test_delete_audio_evidence_and_never_arbitrary_paths(self):
        ids = [self.add(f"I read Zefirin, volume {i}.") for i in range(2)]
        for i, ident in enumerate(ids):
            self.history.amend(ident, f"I read Zephyrine, volume {i}.")
        self.history.retain_audio(ids[0], b"private audio", 10)
        audio = Path(self.history.get(ids[0])["audio_path"])
        self.history.delete(ids[0])
        self.assertFalse(audio.exists())
        self.assertFalse(self.history.pending_suggestions())
        unrelated = self.root / "keep.wav"
        unrelated.write_bytes(b"ok")
        self.history.delete(self.add(audio_path=str(unrelated)))
        self.assertTrue(unrelated.exists())
        stamp = self.history.get(ids[1])["created_at"]
        self.history.delete(ids[1])
        reused = self.add("New")
        self.assertFalse(self.history.delete(reused, stamp), "a stale confirmation cannot delete a reused id")

    def test_retention_forever_by_default_and_boundary(self):
        old, recent = self.add(), self.add()
        with self.history.connect() as db:
            db.execute("UPDATE dictations SET created_at='2026-10-01T00:00:00+00:00' WHERE id=?", (old,))
            db.execute("UPDATE dictations SET created_at='2026-10-12T00:00:00+00:00' WHERE id=?", (recent,))
        self.assertEqual(Config(self.root)["history_days"], 0)
        self.assertEqual(self.history.prune_days(0), 0)
        self.assertEqual(self.history.prune_days(1, datetime(2026, 10, 13, tzinfo=timezone.utc)), 1)
        self.assertTrue(self.history.get(recent))

    def test_swaps(self):
        self.assertEqual(extract_swaps("I read Zefirin today.", "I read Zephyrine today."), (("Zefirin", "Zephyrine"),))
        self.assertEqual(extract_swaps("Go to abc.", "Go to ABC."), (("abc", "ABC"),))
        for before, after in (("hello world", "Hello, world!"), ("I read it.", "I read it calmly."),
                              ("a b c d e", "f g h i j"), ("Read Zef. Sleep.", "Read Zephyrine. Pray.")):
            self.assertEqual(extract_swaps(before, after), ())


class VocabularyTests(Base):
    def test_starts_empty_and_parses(self):
        self.assertEqual(load_vocabulary(self.root / "missing.txt").terms, ())
        self.assertFalse((self.root / "missing.txt").exists(), "loading never creates a file")
        vocab = parse_vocabulary("# comment\n \nZefirin => Zephyrine\nZephyrine\nZEPHYRINE\n => none\nbad =>\nABC")
        self.assertEqual(vocab.terms, ("Zephyrine", "ABC"))
        self.assertEqual(vocab.correct("ZEFIRIN and Zefirinas"), "Zephyrine and Zefirinas")
        self.assertEqual(parse_vocabulary("a => b\nb => c").correct("a b"), "b c")


class ExchangeTests(Base):
    def test_markdown_and_csv_roundtrip_with_injection_guard(self):
        text = '=A formula?\n```orfeuscribe-entry-v1\n{"bad":true}\n```\n<text>\nZephyrine, "word"; café.'
        ident = self.add(text, mode="outline", input_language="la", note_folder="Talks")
        self.history.amend(ident, text + "\nEdit.")
        self.add("Not in the search.")
        expected = self.history.get(ident)
        for suffix in (".csv", ".md"):
            with self.subTest(suffix=suffix):
                path = self.root / ("export" + suffix)
                self.assertEqual(export_history(self.history, path, "Zephyrine"), 1)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                if suffix == ".csv":
                    self.assertIn("'=A formula?", path.read_text())
                other = History(self.root / ("imported" + suffix + ".db"))
                self.assertEqual(import_history(other, path), 1)
                actual = other.search()[0]
                for key in expected:
                    if key not in ("id", "audio_path"):
                        self.assertEqual(actual[key], expected[key], key)
                self.assertIsNone(actual["audio_path"])

    def test_invalid_entry_rolls_back_everything(self):
        self.add()
        path = self.root / "broken.md"
        export_history(self.history, path)
        path.write_text(path.read_text() + '\n```orfeuscribe-entry-v1\n{"bad":true}\n```\n')
        with self.assertRaises(ValueError):
            import_history(self.history, path)
        self.assertEqual(len(self.history.search()), 1)


class NotesTests(Base):
    def test_append_only_allowlist_links_and_symlinks(self):
        root = self.root / "notes"
        root.mkdir()
        (root / "Zephyrine.md").write_text("An earlier note.")
        vocab = parse_vocabulary("Zephyrine\nOrrery")
        path = append_folder_note("Zephyrine and Orrery.", {"wm_class": "gedit"}, "Ideas", ["Ideas"], vocab, root,
                                  wikilinks=True)
        before = path.read_bytes()
        append_folder_note("Another point.", {}, "Ideas", ["Ideas"], vocab, root)
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertIn("[[Zephyrine]] and Orrery.", path.read_text())
        self.assertEqual(path.read_text().count("# Ideas"), 1)
        self.assertEqual(link_vocabulary("[[Zephyrine]] and Zephyrine.", vocab, root), "[[Zephyrine]] and [[Zephyrine]].")
        for name in ("../out", "Ideas/child", "a..b", "a\\b", "Other"):
            with self.assertRaises(ValueError):
                append_folder_note("no", {}, name, ["Ideas"], vocab, root)
        with self.assertRaises(ValueError):
            append_folder_note("no", {}, "Ideas", ["Ideas"], vocab, "")
        (root / "Link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            append_folder_note("no", {}, "Link", ["Link"], vocab, root)
        victim = self.root / "keep.txt"
        victim.write_text("Before")
        inbox = root / "Inbox"
        inbox.mkdir()
        (inbox / "2026-10-13.md").symlink_to(victim)
        with self.assertRaises(OSError):
            append_folder_note("no", {}, "", ["Inbox"], vocab, root, now=datetime(2026, 10, 13, 12, tzinfo=timezone.utc))
        self.assertEqual(victim.read_text(), "Before")


class RescueStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = RescueStore(Path(temp.name) / "rescue")

    def test_private_files_and_metadata(self):
        ident = self.store.save(wav(), reason="error", message="network", context={"wm_class": "gedit", "title": "x"},
                                mode="email", request={"operation": "dictation", "snapshot": {"pid": 1}, "input_language": "en"})
        self.assertEqual(stat.S_IMODE(os.stat(self.store.directory).st_mode), 0o700)
        for suffix in (".wav", ".json"):
            self.assertEqual(stat.S_IMODE(os.stat(self.store.directory / f"{ident}{suffix}").st_mode), 0o600)
        item = self.store.get(ident)
        self.assertEqual((item["reason"], item["mode"], item["context"]["wm_class"]), ("error", "email", "gedit"))
        self.assertNotIn("snapshot", item["request"])
        self.assertEqual(self.store.audio(ident), wav())

    def test_no_speech_is_not_kept_and_expiry(self):
        self.assertIsNone(self.store.save(wav(0.1), reason="error"))
        old = self.store.save(wav(), reason="cancelled", now=time.time() - TTL - 60)
        new = self.store.save(wav(), reason="error")
        self.assertEqual([item["id"] for item in self.store.items()], [new])
        self.assertFalse((self.store.directory / f"{old}.wav").exists())
        later = time.time() + TTL + 1
        self.assertEqual(self.store.prune(now=later), 1)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_identifiers_outside_the_format_are_refused(self):
        with self.assertRaises(ValueError):
            self.store.audio("../history")
        self.assertIsNone(self.store.get("../../x"))


if __name__ == "__main__":
    unittest.main()
