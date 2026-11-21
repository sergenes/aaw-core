"""The shared knowledge base: small notes every agent session on this computer can read.

One fact per Markdown file in <state dir>/knowledge, with a short header (title,
description, who added it and when). Reading is free; a note an agent proposes is
written only after the user approves it on a card (see peers.py). The user can edit,
add or delete the files by hand at any time."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aaw_core.config import Settings

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_NOTE = 4000  # characters of text; a longer fact belongs in a file the note points to
INDEX_LINES = 40

# What a credential looks like. A note is readable by every agent on the computer and
# its text crosses the relay on the approval card, so these are refused outright.
SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "an API key"),
    (re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"), "a GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*\S{6,}"), "a password or token"),
)


@dataclass
class Note:
    name: str
    title: str
    description: str
    added: str
    body: str


def folder(settings: Settings) -> Path:
    return settings.state_dir / "knowledge"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def parse(name: str, text: str) -> Note:
    """A note from its file text. A file with no header (written by hand) is all body,
    titled by its name."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        head, sep, rest = text[4:].partition("\n---\n")
        if sep:
            body = rest
            for line in head.splitlines():
                key, colon, value = line.partition(":")
                if colon:
                    meta[key.strip().lower()] = value.strip()
    return Note(name=name, title=meta.get("title") or name, description=meta.get("description", ""),
                added=meta.get("added", ""), body=body.strip())


def render(note: Note) -> str:
    def one_line(value: str) -> str:
        return " ".join(value.split())
    return (f"---\ntitle: {one_line(note.title)}\ndescription: {one_line(note.description)}\n"
            f"added: {one_line(note.added)}\n---\n{note.body.strip()}\n")


def read(settings: Settings, name: str) -> Note | None:
    if not valid_name(name):
        return None
    try:
        return parse(name, (folder(settings) / f"{name}.md").read_text())
    except (OSError, UnicodeDecodeError):
        return None


def list_notes(settings: Settings) -> list[Note]:
    try:
        files = sorted(folder(settings).glob("*.md"))
    except OSError:
        return []
    notes = []
    for f in files:
        try:
            notes.append(parse(f.stem, f.read_text()))
        except (OSError, UnicodeDecodeError):
            continue
    return notes


def search(settings: Settings, words: list[str]) -> list[Note]:
    """The notes that hold every word (case does not matter) in their name, title,
    description or text."""
    wanted = [w.lower() for w in words if w.strip()]
    out = []
    for note in list_notes(settings):
        hay = f"{note.name}\n{note.title}\n{note.description}\n{note.body}".lower()
        if all(w in hay for w in wanted):
            out.append(note)
    return out


def looks_secret(text: str) -> str:
    """What kind of credential the text seems to hold ("an API key"), or ""."""
    for pattern, what in SECRET_PATTERNS:
        if pattern.search(text):
            return what
    return ""


def check(name: str, title: str, body: str) -> str:
    """Why this note cannot be proposed, or "" when it can."""
    if not valid_name(name):
        return "the name must be lower-case letters, digits and dashes, up to 64 characters (e.g. ssh-rocky)"
    if not body.strip():
        return "the note has no text"
    if len(body) > MAX_NOTE:
        return f"the note is longer than {MAX_NOTE} characters; keep the fact short, or point to a file"
    secret = looks_secret(f"{title}\n{body}")
    if secret:
        return (f"the text looks like it holds {secret}. Notes are readable by every agent on this "
                "computer: say where the credential is kept, not what it is")
    return ""


def stamp(agent: str, project: str) -> str:
    who = f"{agent} in {project}" if project else "the user"
    return f"{datetime.now().astimezone().strftime('%Y-%m-%d')} by {who}"


def write(settings: Settings, note: Note) -> Path:
    path = folder(settings) / f"{note.name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render(note))
    tmp.replace(path)
    return path


def remove(settings: Settings, name: str) -> bool:
    if not valid_name(name):
        return False
    try:
        (folder(settings) / f"{name}.md").unlink()
    except OSError:
        return False
    return True


def index(settings: Settings, limit: int = INDEX_LINES) -> list[str]:
    """One line per note for a session's start: "name: title. description"."""
    notes = list_notes(settings)
    lines = []
    for note in notes[:limit]:
        text = note.title if not note.description else f"{note.title}. {note.description}"
        lines.append(f"- {note.name}: {text}"[:200])
    if len(notes) > limit:
        lines.append(f"- and {len(notes) - limit} more: `aaw know` lists them all")
    return lines
