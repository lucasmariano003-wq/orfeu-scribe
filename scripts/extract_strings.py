#!/usr/bin/python3
"""Extracts translatable strings without GNU gettext tools.

    extract_strings.py            write po/orfeuscribe.pot and merge it into po/*.po
    extract_strings.py --check    fail if the template is stale, a translation is
                                  missing, or placeholders differ ({name}, %s)

Python: calls to _(), N_() and ngettext(singular, plural, n) with literal
strings. JavaScript (the extension): _('…') and _("…").
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POT = ROOT / "po" / "orfeuscribe.pot"
SOURCES = sorted([*(ROOT / "orfeuscribe").rglob("*.py")]) + [ROOT / "extension/orfeuscribe@lucasmariano003-wq.github.io/extension.js"]
HEADER = ('msgid ""\nmsgstr ""\n"Project-Id-Version: Orfeu Scribe\\n"\n"MIME-Version: 1.0\\n"\n'
          '"Content-Type: text/plain; charset=UTF-8\\n"\n"Content-Transfer-Encoding: 8bit\\n"\n')
JS_CALL = re.compile(r"""(?<![\w.$])_\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")\s*\)""")
PLACEHOLDER = re.compile(r"\{[a-z_]+(?::[^}]*)?\}|%s")


def python_strings(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        args = node.args
        if node.func.id in ("_", "N_") and args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            yield node.lineno, args[0].value, None
        elif node.func.id == "ngettext" and len(args) >= 2 and all(
                isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args[:2]):
            yield node.lineno, args[0].value, args[1].value


def js_strings(path):
    text = path.read_text(encoding="utf-8")
    for match in JS_CALL.finditer(text):
        raw = match[1] if match[1] is not None else match[2]
        value = re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), raw)
        yield text.count("\n", 0, match.start()) + 1, value, None


def collect():
    entries = {}
    for path in SOURCES:
        found = js_strings(path) if path.suffix == ".js" else python_strings(path)
        for line, msgid, plural in sorted(found, key=lambda item: item[0]):
            entry = entries.setdefault(msgid, {"plural": plural, "refs": []})
            entry["plural"] = entry["plural"] or plural
            entry["refs"].append(f"{path.relative_to(ROOT)}:{line}")
    return entries


def quote(text):
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    if "\n" in escaped.rstrip("\n"):
        lines = escaped.split("\n")
        return '""\n' + "\n".join('"' + line + ('\\n' if index < len(lines) - 1 else '') + '"'
                                   for index, line in enumerate(lines) if line or index < len(lines) - 1)
    return '"' + escaped.replace("\n", "\\n") + '"'


def render(entries, translations=None, plural_forms=None):
    out = [HEADER + (f'"Language: {translations["__language__"]}\\n"\n"Language-Team: none\\n"\n"Last-Translator: Orfeu Scribe contributors\\n"\n"PO-Revision-Date: 2026-10-21 00:00+0000\\n"\n"Plural-Forms: {plural_forms}\\n"\n'
                     if translations else "")]
    for msgid, entry in entries.items():
        lines = ["#: " + " ".join(entry["refs"])]
        if PLACEHOLDER.search(msgid) and "{" in msgid:
            lines.append("#, python-brace-format")
        lines.append("msgid " + quote(msgid))
        value = (translations or {}).get(msgid)
        if entry["plural"]:
            lines.append("msgid_plural " + quote(entry["plural"]))
            forms = value if isinstance(value, list) else ["", ""]
            lines += [f"msgstr[{n}] " + quote(form) for n, form in enumerate(forms)]
        else:
            lines.append("msgstr " + quote(value if isinstance(value, str) else ""))
        out.append("\n".join(lines) + "\n")
    return "\n".join(out)


def read_po(path):
    """{msgid: str | [forms]} plus language and plural forms, from a PO written by render()."""
    import ast as literal
    text = path.read_text(encoding="utf-8")
    language = re.search(r'"Language: ([^\\]+)\\n"', text)
    plural_forms = re.search(r'"Plural-Forms: ([^\\]+)\\n"', text)
    result = {"__language__": language[1] if language else path.stem}
    for block in text.split("\n\n"):
        fields, current = {}, None
        for line in block.splitlines():
            if line.startswith("#"):
                continue
            if line.startswith('"') and current:
                fields[current] += literal.literal_eval(line)
                continue
            name, _, value = line.partition(" ")
            current = name
            fields[name] = literal.literal_eval(value)
        if fields.get("msgid"):
            if "msgid_plural" in fields:
                result[fields["msgid"]] = [fields[k] for k in sorted(k for k in fields if k.startswith("msgstr["))]
            else:
                result[fields["msgid"]] = fields.get("msgstr", "")
    return result, (plural_forms[1] if plural_forms else "nplurals=2; plural=(n != 1);")


def check(entries):
    problems = []
    # Only the strings matter: source references move whenever code moves.
    template = set(read_po(POT)[0]) - {"__language__"} if POT.exists() else set()
    if template != set(entries):
        problems.append("po/orfeuscribe.pot is stale: run scripts/extract_strings.py")
    for po in sorted(ROOT.glob("po/*.po")):
        translations, _forms = read_po(po)
        for msgid, entry in entries.items():
            value = translations.get(msgid)
            forms = value if isinstance(value, list) else [value]
            if not value or not all(forms):
                problems.append(f"{po.name}: missing translation for {msgid!r}")
                continue
            for form in forms:
                if sorted(PLACEHOLDER.findall(form)) != sorted(PLACEHOLDER.findall(msgid)):
                    problems.append(f"{po.name}: placeholders differ in {msgid!r}")
        for msgid in set(translations) - set(entries) - {"__language__"}:
            problems.append(f"{po.name}: obsolete entry {msgid!r}")
    return problems


def main(argv):
    entries = collect()
    if "--check" in argv:
        problems = check(entries)
        print("\n".join(problems) or f"Translations complete: {len(entries)} strings.")
        return 1 if problems else 0
    POT.parent.mkdir(exist_ok=True)
    POT.write_text(render(entries), encoding="utf-8")
    for po in sorted(ROOT.glob("po/*.po")):
        translations, forms = read_po(po)
        po.write_text(render(entries, translations, forms), encoding="utf-8")
    print(f"{len(entries)} strings → {POT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
