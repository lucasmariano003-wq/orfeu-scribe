"""Rescue: no dictation is lost when it fails or is cancelled.

The audio of a dictation that failed (network, transcription, revision), was
cancelled or was cancelled by voice stays for 24 hours in a private folder
(0700, files 0600), whatever the audio retention setting is. After that it is
deleted automatically. Recover transcribes and revises that audio again and
puts the entry in the history.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from .i18n import N_, _

TTL = 24 * 3600
# Less than this is not speech: just the header and a moment of microphone.
MINIMUM_BYTES = 16000  # ~0.5 s of 16 kHz PCM
REASONS = {"error": N_("Failed"), "cancelled": N_("Cancelled"), "voice-cancel": N_("Cancelled by voice")}
IDENT = re.compile(r"^\d{10,12}-[0-9a-f]{8}$")


def reason_label(reason):
    return _(REASONS[reason]) if reason in REASONS else reason


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise OSError("The rescue folder is not a regular directory.")
    path.chmod(0o700)
    return path


def _write(path: Path, data: bytes, replace=False):
    temp = path.with_name("." + path.name + "-" + uuid.uuid4().hex[:6])
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not replace and path.exists():
            raise FileExistsError(path)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class RescueStore:
    def __init__(self, directory: Path, ttl=TTL):
        self.directory = Path(directory)
        self.ttl = ttl

    def save(self, audio: bytes, *, reason, message="", context=None, mode="dictation", request=None,
             recording=0.0, history_id=None, now=None):
        """Stores audio and request; returns the identifier, or None when there is no speech."""
        if not audio or len(audio) < MINIMUM_BYTES or reason not in REASONS:
            return None
        now = time.time() if now is None else now
        _private_dir(self.directory)
        ident = f"{int(now)}-{uuid.uuid4().hex[:8]}"
        request = dict(request or {})
        request.pop("snapshot", None)
        meta = {"id": ident, "created": now, "reason": reason, "message": message[:300],
                "context": {"wm_class": (context or {}).get("wm_class", "")[:512],
                            "title": (context or {}).get("title", "")[:512]},
                "mode": mode, "request": {key: request[key] for key in
                                          ("operation", "language", "selection", "input_language") if key in request},
                "recording": float(recording or 0.0), "history_id": history_id, "bytes": len(audio)}
        _write(self.directory / f"{ident}.wav", audio)
        _write(self.directory / f"{ident}.json", json.dumps(meta, ensure_ascii=False).encode())
        self.prune(now)
        return ident

    def _path(self, ident, suffix):
        if not isinstance(ident, str) or not IDENT.match(ident):
            raise ValueError("Invalid rescue identifier.")
        return self.directory / f"{ident}{suffix}"

    def get(self, ident, now=None):
        try:
            path = self._path(ident, ".json")
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        now = time.time() if now is None else now
        if not isinstance(meta, dict) or now - float(meta.get("created", 0)) > self.ttl:
            return None
        if not self._path(ident, ".wav").is_file():
            return None
        return meta

    def audio(self, ident):
        return self._path(ident, ".wav").read_bytes()

    def update(self, ident, **fields):
        meta = self.get(ident)
        if meta is None:
            return False
        meta.update(fields)
        _write(self._path(ident, ".json"), json.dumps(meta, ensure_ascii=False).encode(), replace=True)
        return True

    def remove(self, ident):
        for suffix in (".wav", ".json"):
            try:
                self._path(ident, suffix).unlink(missing_ok=True)
            except ValueError:
                return False
        return True

    def items(self, now=None):
        if not self.directory.is_dir():
            return []
        found = []
        for path in self.directory.glob("*.json"):
            meta = self.get(path.stem, now)
            if meta:
                found.append(meta)
        return sorted(found, key=lambda item: item["created"], reverse=True)

    def prune(self, now=None):
        """Deletes what is older than 24 h, and leftovers of interrupted writes."""
        if not self.directory.is_dir():
            return 0
        now = time.time() if now is None else now
        removed = 0
        for path in list(self.directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            stem = path.name.split(".", 1)[0] if not path.name.startswith(".") else ""
            created = None
            if IDENT.match(stem):
                created = int(stem.split("-", 1)[0])
            elif path.name.startswith("."):
                created = path.stat().st_mtime
            if created is not None and now - created > self.ttl:
                path.unlink(missing_ok=True)
                removed += path.suffix == ".wav"
        return removed
