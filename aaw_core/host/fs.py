"""Headless folder browsing and file reading for the phone.

The phone's "new session" browser asks to list a folder; "view file" asks for a
file's contents. Both are constrained: no shell, no search, no writes. Every
requested path is fenced to an allowed ``roots`` list (default: the user's home)
after realpath canonicalization, so a crafted request can never reach outside the
configured roots. Names, paths and contents returned here are PLAINTEXT; the
supervisor encrypts them with the session key before they leave the computer.

Contents are far more sensitive than names, so reading is tighter still:
read-only, an extension allowlist, and a hard size cap.
"""

from __future__ import annotations

import base64
import os

MAX_ENTRIES = 500
MAX_FETCH_BYTES = 4 * 1024 * 1024  # one response frame; base64 + encryption grow it ~1.8x
RAW_CHUNK = 512 * 1024

# extension -> mime; text only for now (images would need only an entry here: the
# transfer is already binary-safe).
ALLOWED = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
}

ERR_NOT_FOUND = "not_found"
ERR_NOT_A_DIR = "not_a_dir"
ERR_NOT_A_FILE = "not_a_file"
ERR_DENIED = "denied"
ERR_OUTSIDE_ROOTS = "outside_roots"
ERR_UNSUPPORTED = "unsupported_type"
ERR_TOO_LARGE = "too_large"


def canon(path: str) -> str:
    """Expand ~ and resolve symlinks, so the fence compares real paths."""
    return os.path.realpath(os.path.expanduser(path))


def default_roots() -> list[str]:
    return [canon("~")]


def within_roots(path: str, roots: list[str] | tuple[str, ...]) -> bool:
    """True when the canonical path IS one of roots or a descendant of one."""
    cpath = canon(path)
    for root in roots:
        croot = canon(root)
        if cpath == croot or cpath.startswith(croot + os.sep):
            return True
    return False


def list_dir(path: str | None, roots: list[str] | tuple[str, ...] | None = None) -> dict:
    """List a folder for the browser::

        {resolved_path, parent, at_root, entries, truncated, error}

    ``entries`` is folders first (alphabetical) then files (alphabetical), each
    ``{name, kind: "dir"|"file", is_repo, size?}``. ``error`` is "" on success or one
    of the ERR_* codes. Hidden entries (dotfiles) are excluded.
    """
    roots = [canon(r) for r in (roots or default_roots())]
    target = canon(path) if path else roots[0]

    if not within_roots(target, roots):
        return _dir_err(ERR_OUTSIDE_ROOTS)
    if not os.path.exists(target):
        return _dir_err(ERR_NOT_FOUND)
    if not os.path.isdir(target):
        return _dir_err(ERR_NOT_A_DIR)

    at_root = any(target == r for r in roots)
    parent = None if at_root else os.path.dirname(target)
    if parent is not None and not within_roots(parent, roots):  # "up" never escapes the fence
        parent, at_root = None, True

    try:
        raw = list(os.scandir(target))
    except OSError:
        return _dir_err(ERR_DENIED)

    dirs: list[dict] = []
    files: list[dict] = []
    for e in raw:
        if e.name.startswith("."):
            continue
        try:
            is_dir = e.is_dir()  # follows symlinks; the realpath fence guards entry
        except OSError:
            is_dir = False
        if is_dir:
            dirs.append({"name": e.name, "kind": "dir",
                         "is_repo": os.path.isdir(os.path.join(target, e.name, ".git"))})
        else:
            entry = {"name": e.name, "kind": "file", "is_repo": False}
            try:
                entry["size"] = e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            files.append(entry)

    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    entries = dirs + files
    return {
        "resolved_path": target,
        "parent": parent,
        "at_root": at_root,
        "entries": entries[:MAX_ENTRIES],
        "truncated": len(entries) > MAX_ENTRIES,
        "error": "",
    }


def fetch_file(path: str, roots: list[str] | tuple[str, ...] | None = None) -> dict:
    """Read a file for the phone::

        {mime, size, chunks: [base64-str, ...], error}

    ``chunks`` are base64 of raw slices of at most RAW_CHUNK bytes (plaintext here; the
    supervisor encrypts each). ``error`` is "" on success or one of the ERR_* codes.
    """
    roots = [canon(r) for r in (roots or default_roots())]
    target = canon(path)

    if not within_roots(target, roots):
        return _file_err(ERR_OUTSIDE_ROOTS)
    if not os.path.exists(target):
        return _file_err(ERR_NOT_FOUND)
    if not os.path.isfile(target):
        return _file_err(ERR_NOT_A_FILE)
    ext = os.path.splitext(target)[1].lower()
    if ext not in ALLOWED:
        return _file_err(ERR_UNSUPPORTED)
    try:
        size = os.path.getsize(target)
    except OSError:
        return _file_err(ERR_DENIED)
    if size > MAX_FETCH_BYTES:
        return _file_err(ERR_TOO_LARGE)
    try:
        with open(target, "rb") as f:
            data = f.read()
    except OSError:
        return _file_err(ERR_DENIED)

    chunks = [base64.b64encode(data[i:i + RAW_CHUNK]).decode() for i in range(0, len(data), RAW_CHUNK)] or [""]
    return {"mime": ALLOWED[ext], "size": size, "chunks": chunks, "error": ""}


def _dir_err(kind: str) -> dict:
    return {"resolved_path": "", "parent": None, "at_root": False, "entries": [], "truncated": False, "error": kind}


def _file_err(kind: str) -> dict:
    return {"mime": "", "size": 0, "chunks": [], "error": kind}
