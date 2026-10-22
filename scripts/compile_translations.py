#!/usr/bin/python3
"""Compiles po/*.po into gettext catalogs for the app and the extension.

Usage: compile_translations.py [OUTPUT_ROOT]

Writes OUTPUT_ROOT/locale/<lang>/LC_MESSAGES/orfeuscribe.mo and
OUTPUT_ROOT/extension/<uuid>/locale/<lang>/LC_MESSAGES/orfeuscribe.mo.
OUTPUT_ROOT defaults to the repository. The built-in compiler needs no GNU
gettext tools; tests/test_i18n.py checks its output with Python's gettext.
"""
from __future__ import annotations

import ast
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UUID = "orfeuscribe@lucasmariano003-wq.github.io"
DOMAIN = "orfeuscribe"


def parse_po(path):
    """Minimal PO parser: msgctxt is not used by this project; plurals are supported."""
    messages, entry, field = {}, {}, None

    def flush():
        if "msgid" in entry and not entry.get("fuzzy"):
            if "msgid_plural" in entry:
                key = entry["msgid"] + "\0" + entry["msgid_plural"]
                value = "\0".join(entry[k] for k in sorted(k for k in entry if k.startswith("msgstr[")))
            else:
                key, value = entry["msgid"], entry.get("msgstr", "")
            if value.replace("\0", "") or key == "":
                messages[key] = value
        entry.clear()

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#,") and "fuzzy" in line:
            flush()
            entry["fuzzy"] = True
            continue
        if not line or line.startswith("#"):
            if not line and "msgid" in entry:
                flush()
            continue
        if line.startswith('"'):
            entry[field] += ast.literal_eval(line)
            continue
        name, _, value = line.partition(" ")
        if name == "msgid" and "msgid" in entry:
            fuzzy = entry.get("fuzzy")
            flush()
            if fuzzy:
                entry["fuzzy"] = fuzzy
        field = name
        entry[field] = ast.literal_eval(value)
    flush()
    return messages


def write_mo(messages, path):
    keys = sorted(messages)
    ids = [k.encode("utf-8") for k in keys]
    strs = [messages[k].encode("utf-8") for k in keys]
    offset = 7 * 4 + 16 * len(keys)
    id_table, str_table, data = [], [], b""
    for item in ids:
        id_table.append((len(item), offset + len(data)))
        data += item + b"\0"
    for item in strs:
        str_table.append((len(item), offset + len(data)))
        data += item + b"\0"
    header = struct.pack("Iiiiiii", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + 8 * len(keys), 0, 0)
    tables = b"".join(struct.pack("ii", *pair) for pair in id_table + str_table)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + tables + data)


def main():
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    for po in sorted((ROOT / "po").glob("*.po")):
        language = po.stem
        targets = [output / "locale" / language / "LC_MESSAGES" / f"{DOMAIN}.mo",
                   output / "extension" / UUID / "locale" / language / "LC_MESSAGES" / f"{DOMAIN}.mo"]
        for target in targets:
            write_mo(parse_po(po), target)
        print(f"{language}: {len(parse_po(po)) - 1} messages → {targets[0].relative_to(output)}")


if __name__ == "__main__":
    main()
