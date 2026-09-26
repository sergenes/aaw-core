"""A local, plaintext mirror of this computer's project documents, for a GUI on the same
machine (the Mac app) that must not open its own relay connection.

``<state dir>/projects/<project id>.json`` holds the fields the relay holds for that session
(status, agent, path, pending question, last summary, auto-approve, scheduled count), with
the encrypted fields decrypted. The transport merges every state write into it, and the
supervisor rewrites the whole set from the relay on each watchdog tick, so it converges
even when a frame was missed. Files are 0600 like the session log; the directory is the
user's own state dir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aaw_core.encryption import is_encrypted

ENCRYPTED_FIELDS = ("last_event_summary", "project_path", "pending_message")


def mirror_dir(state_dir: Path) -> Path:
    return state_dir / "projects"


def _path(directory: Path, project_id: str) -> Path:
    return directory / f"{project_id}.json"


def read_project(directory: Path, project_id: str) -> dict:
    try:
        return json.loads(_path(directory, project_id).read_text())
    except (OSError, ValueError):
        return {}


def read_projects(directory: Path) -> dict[str, dict]:
    """Every mirrored document, by project id; an empty dict when nothing is mirrored yet."""
    out: dict[str, dict] = {}
    try:
        names = sorted(p for p in directory.glob("*.json"))
    except OSError:
        return out
    for p in names:
        doc = read_project(directory, p.stem)
        if doc:
            out[p.stem] = doc
    return out


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, sort_keys=True)
    tmp.replace(path)


def merge_project(directory: Path, project_id: str, fields: dict, decrypt=None) -> dict:
    """Merge ``fields`` into the mirrored document (decrypting the encrypted ones with
    ``decrypt``, a str -> str callable) and return the result. Never raises: a mirror that
    cannot be written must not stop the daemon."""
    doc = read_project(directory, project_id)
    for k, v in fields.items():
        if k in ENCRYPTED_FIELDS and isinstance(v, str) and v and is_encrypted(v):
            try:
                v = decrypt(v) if decrypt is not None else ""
            except Exception:  # noqa: BLE001 - a re-paired key: keep the ciphertext out, not the daemon
                v = ""
            if is_encrypted(v):
                v = ""  # fail-soft decrypt handed the envelope back: this key cannot read it
        doc[k] = v
    doc["project_id"] = project_id
    try:
        _write(_path(directory, project_id), doc)
    except OSError:
        pass
    return doc


def replace_projects(directory: Path, docs: dict[str, dict], decrypt=None) -> None:
    """Rewrite the mirror from the relay's full set: documents that are gone (the phone
    removed the session) are removed here too."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        existing = {p.stem for p in directory.glob("*.json")}
    except OSError:
        return
    for pid, doc in docs.items():
        merge_project(directory, pid, doc, decrypt)
    for stale in existing - set(docs):
        try:
            _path(directory, stale).unlink()
        except OSError:
            pass
