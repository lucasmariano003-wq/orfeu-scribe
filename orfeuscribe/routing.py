"""Explicit voice destinations. A spoken question is still a dictation.

Only one destination exists: a Markdown notes folder, and only when the user
configured one in Settings. "note: buy bread" or "note in Ideas: …" then goes to
the folder instead of the cursor.
"""
import re

NOTE_FOLDER = re.compile(r"^\s*(?:note\s+in|nota\s+em|nota\s+en)\s+([^:\r\n]+):\s*", re.I)
NOTE = re.compile(r"^\s*(?:take\s+a\s+note|anota|anote)\b[\s,:;.!?—-]*|^\s*(?:note|nota)\s*:\s*", re.I)


def route(text, operation="dictation", notes=False):
    if not notes:
        return operation, text
    match = NOTE_FOLDER.match(text)
    if match:
        return "note", text[match.end():].strip()
    match = NOTE.match(text)
    if match:
        return "note", text[match.end():].strip()
    return operation, text


def note_folder(text, notes=False):
    match = NOTE_FOLDER.match(text) if notes else None
    return match[1].strip() if match else ""


def selection_action(reply, editable):
    # Missing, unexpected or uncertain values never authorize a replacement.
    return "replace" if (editable and reply.get("action") == "edit"
                         and reply.get("certain") is True) else "card"
