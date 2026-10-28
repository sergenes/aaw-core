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
import re
import shutil
import subprocess
import sys
import tempfile

MAX_ENTRIES = 500
MAX_FETCH_BYTES = 4 * 1024 * 1024  # one response frame; base64 + encryption grow it ~1.8x
# A phone that asks for the file in parts (``parts: true``) gets one response frame per
# chunk, each under the relay's 1 MiB frame cap, so the file itself can be larger: a
# full-screen Retina screenshot is a few MB.
MAX_FETCH_PARTS_BYTES = 12 * 1024 * 1024
# An image fetched to look at (``max_px`` set) is sent as a JPEG rendition no larger than
# max_px on its long side when the original is bigger than this: a few-MB screenshot over
# a home uplink takes ten seconds, its 250 KB rendition one. Save asks without max_px and
# gets the original. macOS renders with sips; elsewhere the original goes as it is.
PREVIEW_MIN_BYTES = 300 * 1024
MAX_PREVIEW_SOURCE_BYTES = 64 * 1024 * 1024  # never read more than this to render a preview
PREVIEW_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".heic"}  # not .gif: it may animate
RAW_CHUNK = 384 * 1024  # base64 (x1.33) then encrypted and base64 again stays under one 1 MiB frame

# A file the phone attaches to a prompt. In one request frame (the phones up to 2.11, a
# downscaled photo) the relay's 1 MiB frame cap is the real limit and this cap only bounds
# what one request can write. In parts (since 0.1.12, one frame per RAW_CHUNK slice) the
# file can be larger: MAX_ATTACH_PARTS_BYTES is about a minute of phone video.
MAX_ATTACH_BYTES = 12 * 1024 * 1024
MAX_ATTACH_PARTS_BYTES = 50 * 1024 * 1024
MAX_ATTACH_PARTS = -(-MAX_ATTACH_PARTS_BYTES // RAW_CHUNK)  # ceil: the most parts an upload may declare
ATTACH_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
# Files an agent can open from a path: images, documents, short videos, and text and source
# files. No archives or binaries. No .svg: the phones treat the image extensions as photos
# (downscale, preview), and .svg is neither a photo nor on the download channel's list.
ATTACH_SUFFIXES = ATTACH_IMAGE_SUFFIXES | {
    ".pdf", ".mov", ".mp4", ".m4v",
    ".txt", ".md", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".toml", ".xml", ".html", ".css", ".sql",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".swift", ".kt", ".kts", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".cs", ".sh", ".zsh", ".gradle",
}
# Attachments older than this are swept on each new upload: a prompt uses its file at once,
# so nothing needs to linger, and the folder never grows without bound.
ATTACH_TTL_S = 24 * 60 * 60
# An upload in parts that has not completed in this long was abandoned (the phone gave up,
# or lost its connection): its staged parts are removed on the next upload.
ATTACH_PARTS_TTL_S = 60 * 60
ATTACH_PARTS_DIR = ".parts"  # under the attachments dir: one folder per upload in progress
_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# extension -> mime. The transfer is binary-safe (base64 slices), so an image needs
# only its entry here; the phone renders by mime.
ALLOWED = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
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


def _sips_preview(src: str, max_px: int) -> bytes | None:
    """A JPEG rendition of an image, at most max_px on the long side, via macOS's sips.
    None when sips is unavailable or fails (the caller then sends the original)."""
    if sys.platform != "darwin":
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as tmp:
            r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                                "-Z", str(max_px), src, "--out", tmp.name],
                               capture_output=True, timeout=30, check=False)
            if r.returncode != 0:
                return None
            with open(tmp.name, "rb") as f:
                data = f.read()
            return data or None
    except (OSError, subprocess.SubprocessError):
        return None


def preview_image(path: str, ext: str, size: int, max_px: int) -> tuple[bytes, str] | None:
    """(bytes, mime) of the rendition to send instead of the original, or None to send
    the original: not an image we downscale, small enough already, or no renderer."""
    if max_px <= 0 or ext not in PREVIEW_EXTS or size <= PREVIEW_MIN_BYTES or size > MAX_PREVIEW_SOURCE_BYTES:
        return None
    data = _sips_preview(path, max_px)
    return (data, "image/jpeg") if data else None


def fetch_file(path: str, roots: list[str] | tuple[str, ...] | None = None,
               max_bytes: int | None = None, max_px: int = 0) -> dict:
    """Read a file for the phone::

        {mime, size, chunks: [base64-str, ...], error}

    ``chunks`` are base64 of raw slices of at most RAW_CHUNK bytes (plaintext here; the
    supervisor encrypts each). ``error`` is "" on success or one of the ERR_* codes.
    ``max_bytes`` defaults to MAX_FETCH_BYTES (the single-frame cap). ``max_px`` asks for
    a rendition of a large image no bigger than that on its long side (see preview_image);
    ``size`` and ``mime`` then describe the rendition.
    """
    cap = MAX_FETCH_BYTES if max_bytes is None else max_bytes
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
    mime = ALLOWED[ext]
    preview = preview_image(target, ext, size, max_px)
    if preview is not None:
        data, mime = preview
        size = len(data)
    else:
        if size > cap:
            return _file_err(ERR_TOO_LARGE)
        try:
            with open(target, "rb") as f:
                data = f.read()
        except OSError:
            return _file_err(ERR_DENIED)
    if size > cap:
        return _file_err(ERR_TOO_LARGE)

    chunks = [base64.b64encode(data[i:i + RAW_CHUNK]).decode() for i in range(0, len(data), RAW_CHUNK)] or [""]
    return {"mime": mime, "size": size, "chunks": chunks, "error": ""}


def save_attachment(chunks_b64: list[str], suffix: str, dest_dir: str) -> dict:
    """Write a file the phone uploaded in one request and return its local path::

        {path, size, error}

    ``chunks_b64`` are the base64 raw slices (the download path in reverse; the supervisor
    has already decrypted each). The suffix is checked against the attachment allowlist and the
    joined size against MAX_ATTACH_BYTES. Stale attachments are swept first so the folder
    never grows without bound. The write is ours, not a user path, so there is no root fence,
    but the name is random and the suffix sanitized so the request cannot choose the path.
    """
    ext = ("." + suffix.lstrip(".")).lower()
    if ext not in ATTACH_SUFFIXES:
        return _attach_err(ERR_UNSUPPORTED)
    try:
        data = b"".join(base64.b64decode(c) for c in chunks_b64 if c)
    except (ValueError, TypeError):
        return _attach_err(ERR_DENIED)
    if not data:
        return _attach_err(ERR_NOT_A_FILE)
    if len(data) > MAX_ATTACH_BYTES:
        return _attach_err(ERR_TOO_LARGE)
    try:
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        _sweep_old_attachments(dest_dir)
        name = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=") + ext
        target = os.path.join(dest_dir, name)
        with open(target, "wb") as f:
            f.write(data)
        os.chmod(target, 0o600)
    except OSError:
        return _attach_err(ERR_DENIED)
    return {"path": target, "size": len(data), "error": ""}


def save_attachment_part(upload_id: str, part: int, total: int, chunk_b64: str, suffix: str,
                         dest_dir: str) -> dict:
    """Stage one part of a file the phone uploads in parts, and assemble the file once every
    part is in::

        {received, path, size, error}

    The relay caps a frame at 1 MiB, so a file larger than a photo travels as one request per
    RAW_CHUNK slice, each naming its ``upload_id``, 0-based ``part`` and ``total`` part count
    (the download channel's ``parts`` in reverse). Each part is written to
    ``<dest>/.parts/<upload_id>/<part>`` and acknowledged with ``received`` = its index, so
    the phone sends the next one, or resends a part whose reply it never got (a resend just
    overwrites it). When the last missing part lands, the file is joined in order under a
    random name and ``path`` is set; until then ``path`` is "". Every part repeats the same
    ``total`` and suffix; an upload that changes either, or grows past
    MAX_ATTACH_PARTS_BYTES, is refused and its parts removed.
    """
    ext = ("." + str(suffix).lstrip(".")).lower()
    if ext not in ATTACH_SUFFIXES:
        return _part_err(ERR_UNSUPPORTED)
    if not _UPLOAD_ID.match(str(upload_id)):
        return _part_err(ERR_DENIED)
    try:
        part, total = int(part), int(total)
    except (TypeError, ValueError):
        return _part_err(ERR_DENIED)
    if not (1 <= total <= MAX_ATTACH_PARTS and 0 <= part < total):
        return _part_err(ERR_TOO_LARGE if total > MAX_ATTACH_PARTS else ERR_DENIED)
    try:
        data = base64.b64decode(chunk_b64 or "", validate=True)
    except (ValueError, TypeError):
        return _part_err(ERR_DENIED)
    if not data:
        return _part_err(ERR_NOT_A_FILE)
    if len(data) > RAW_CHUNK:
        return _part_err(ERR_TOO_LARGE)  # one part is one slice: bounds what a request can write
    staging = os.path.join(dest_dir, ATTACH_PARTS_DIR, upload_id)
    meta_path = os.path.join(staging, "meta")
    meta = f"{total} {ext}"
    target = ""
    try:
        if part == 0 or not os.path.exists(meta_path):
            # Before the folder is made: a stale folder under this id (an upload resumed
            # after ATTACH_PARTS_TTL_S) is wiped here and starts over, not wiped under us.
            _sweep_old_attachments(dest_dir)
        os.makedirs(staging, mode=0o700, exist_ok=True)
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                if f.read().strip() != meta:
                    shutil.rmtree(staging, ignore_errors=True)
                    return _part_err(ERR_DENIED)
        else:
            with open(meta_path, "w") as f:
                f.write(meta)
        with open(os.path.join(staging, str(part)), "wb") as f:
            f.write(data)
        have = [n for n in os.listdir(staging) if n.isdigit()]
        staged = sum(os.path.getsize(os.path.join(staging, n)) for n in have)
        if staged > MAX_ATTACH_PARTS_BYTES:
            shutil.rmtree(staging, ignore_errors=True)
            return _part_err(ERR_TOO_LARGE)
        if len(have) < total:
            return {"received": part, "path": "", "size": staged, "error": ""}
        name = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=") + ext
        target = os.path.join(dest_dir, name)
        with open(target, "wb") as out:
            os.chmod(target, 0o600)
            for i in range(total):
                with open(os.path.join(staging, str(i)), "rb") as f:
                    shutil.copyfileobj(f, out)
        shutil.rmtree(staging, ignore_errors=True)
    except OSError:
        if target:  # a join that failed midway (disk full) leaves no half-written file behind
            try:
                os.remove(target)
            except OSError:
                pass
        return _part_err(ERR_DENIED)
    return {"received": part, "path": target, "size": staged, "error": ""}


def _sweep_old_attachments(dest_dir: str) -> None:
    """Remove attachments past ATTACH_TTL_S and uploads in parts abandoned for
    ATTACH_PARTS_TTL_S (by the time their last part was written)."""
    import time
    now = time.time()
    try:
        entries = list(os.scandir(dest_dir))
    except OSError:
        return
    for e in entries:
        try:
            if e.is_file() and e.stat().st_mtime < now - ATTACH_TTL_S:
                os.remove(e.path)
        except OSError:
            pass
    try:
        uploads = list(os.scandir(os.path.join(dest_dir, ATTACH_PARTS_DIR)))
    except OSError:
        return
    for u in uploads:
        try:
            if u.is_dir(follow_symlinks=False) and u.stat().st_mtime < now - ATTACH_PARTS_TTL_S:
                shutil.rmtree(u.path, ignore_errors=True)
        except OSError:
            pass


def _attach_err(kind: str) -> dict:
    return {"path": "", "size": 0, "error": kind}


def _part_err(kind: str) -> dict:
    return {"received": -1, "path": "", "size": 0, "error": kind}


def _dir_err(kind: str) -> dict:
    return {"resolved_path": "", "parent": None, "at_root": False, "entries": [], "truncated": False, "error": kind}


def _file_err(kind: str) -> dict:
    return {"mime": "", "size": 0, "chunks": [], "error": kind}
