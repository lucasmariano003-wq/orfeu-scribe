#!/usr/bin/python3
"""Audits the working tree for personal references, legacy names, secrets and stray data.

    audit.py           print findings grouped by rule (exit 1 if any are outside the allowlist)
    audit.py --all     also print allowed findings (attribution and generalization documents)

A clean result means these patterns were not found; it does not prove that no
secret or personal detail exists. Review by a person is still required.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".work", "__pycache__", "locale", "build", "dist"}
BINARY = {".png", ".mo", ".compiled", ".svg"}
RULES = {
    "legacy identifiers": r"padrelucas|org\.calamus|calamus@|\bcalamus\b",
    "personal or private context": r"\bPadre\b|\bLucas\b|lucasmariano|/home/lucas|Taverna|Guigo|homilia|homily|"
                                   r"Segundo C[ée]rebro|Pal[aá]cio da Mem[oó]ria|Chesterton|Bernanos|Sofia",
    "previous provider coupling": r"\bHermes\b|HERMES_HOME|xai-oauth|resolve_xai|\bGrok\b|api\.x\.ai",
    "secret-like strings": r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}|Bearer [A-Za-z0-9._-]{32,}|AKIA[0-9A-Z]{16}|"
                           r"-----BEGIN [A-Z ]*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}",
    "e-mail addresses": r"[A-Za-z0-9._%+-]+@(?!orfeuscribe\.github\.io|anthropic\.com|orfeuscribe\.invalid|example\.com)"
                        r"[A-Za-z0-9-]+\.[A-Za-z]{2,}",
}
# Documents whose job is to name the origin, and tests that plant synthetic Calamus paths.
ALLOWED = {
    "ATTRIBUTION.md": {"legacy identifiers", "personal or private context", "previous provider coupling"},
    "NOTICE": {"personal or private context"},
    "LICENSE-DECISION.md": {"legacy identifiers", "personal or private context", "previous provider coupling"},
    "docs/GENERALIZATION.md": {"legacy identifiers", "personal or private context", "previous provider coupling"},
    "docs/IDENTITY.md": {"legacy identifiers"},
    "SUMMARY.md": {"legacy identifiers", "personal or private context", "previous provider coupling"},
    "REVIEW.md": {"legacy identifiers"},
    "docs/PROVIDERS.md": {"previous provider coupling"},
    "README.md": {"legacy identifiers"},
    "README.pt-BR.md": {"legacy identifiers"},
    "AGENTS.md": {"legacy identifiers", "personal or private context"},
    "scripts/audit.py": set(RULES),
    "tests/test_install.py": {"legacy identifiers"},
    "tests/test_project.py": {"legacy identifiers", "personal or private context", "previous provider coupling"},
    "orfeuscribe/ids.py": {"legacy identifiers"},
    "orfeuscribe/window.py": {"legacy identifiers"},
    "orfeuscribe/text.py": {"legacy identifiers"},
    "tests/test_text.py": {"personal or private context"},
}
DATA_SUFFIXES = {".wav", ".db", ".sqlite", ".ogg", ".mp3", ".mp4", ".mkv", ".env", ".pem", ".key"}

# Public project identifiers are not personal runtime data. Mask only these
# exact approved namespace components, leaving other account references and
# credentials subject to the existing rules. Spaces preserve line numbers.
PUBLIC_NAMESPACE = ("github.com/lucasmariano003-wq", "lucasmariano003_wq", "lucasmariano003-wq.github.io")


def files():
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and not SKIP_DIRS & set(path.relative_to(ROOT).parts):
            yield path


def audit():
    findings = []
    for path in files():
        relative = str(path.relative_to(ROOT))
        if path.suffix.lower() in DATA_SUFFIXES:
            findings.append(("stray data file", relative, 0, path.suffix))
        if path.suffix.lower() in BINARY:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            findings.append(("unreadable file", relative, 0, ""))
            continue
        scan_text = text
        for public_name in PUBLIC_NAMESPACE:
            scan_text = scan_text.replace(public_name, " " * len(public_name))
        for rule, pattern in RULES.items():
            for match in re.finditer(pattern, scan_text, re.I if rule != "secret-like strings" else 0):
                line = text.count("\n", 0, match.start()) + 1
                findings.append((rule, relative, line, match[0]))
    return findings


def blocking(findings):
    return [f for f in findings if f[0] not in ALLOWED.get(f[1], set())]


def main(argv):
    findings = audit()
    shown = findings if "--all" in argv else blocking(findings)
    for rule in sorted({f[0] for f in shown}):
        print(f"{rule}:")
        for _rule, path, line, text in (f for f in shown if f[0] == rule):
            print(f"  {path}:{line}  {text!r}")
    print(f"{len(findings)} findings, {len(blocking(findings))} outside the allowlist.")
    return 1 if blocking(findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
