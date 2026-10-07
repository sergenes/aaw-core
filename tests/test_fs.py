"""The folder browser and file reader: fenced to the roots, allowlisted, capped."""

from __future__ import annotations

import base64
import os

from aaw_core.host import fs


def _tree(tmp_path):
    root = tmp_path / "home"
    (root / "proj" / ".git").mkdir(parents=True)
    (root / "proj" / "notes.md").write_text("# notes\n")
    (root / "proj" / "data.bin").write_bytes(b"\x00\x01")
    (root / ".hidden").mkdir()
    (root / "zeta").mkdir()
    (tmp_path / "outside" / "secret").mkdir(parents=True)
    return root


def test_list_dir_folders_first_then_files_hidden_excluded(tmp_path):
    root = _tree(tmp_path)
    res = fs.list_dir(None, [str(root)])
    assert res["error"] == "" and res["at_root"] and res["parent"] is None
    assert [(e["name"], e["kind"]) for e in res["entries"]] == [("proj", "dir"), ("zeta", "dir")]
    assert res["entries"][0]["is_repo"] is True

    sub = fs.list_dir(str(root / "proj"), [str(root)])
    assert not sub["at_root"] and sub["parent"] == fs.canon(str(root))
    names = [e["name"] for e in sub["entries"]]
    assert names == ["data.bin", "notes.md"]  # the .git folder is hidden
    assert sub["entries"][1]["size"] == len("# notes\n")


def test_list_dir_fence_and_errors(tmp_path):
    root = _tree(tmp_path)
    assert fs.list_dir(str(tmp_path / "outside"), [str(root)])["error"] == fs.ERR_OUTSIDE_ROOTS
    assert fs.list_dir(str(root / ".." / "outside"), [str(root)])["error"] == fs.ERR_OUTSIDE_ROOTS
    assert fs.list_dir(str(root / "missing"), [str(root)])["error"] == fs.ERR_NOT_FOUND
    assert fs.list_dir(str(root / "proj" / "notes.md"), [str(root)])["error"] == fs.ERR_NOT_A_DIR


def test_symlink_out_of_the_fence_is_refused(tmp_path):
    root = _tree(tmp_path)
    os.symlink(tmp_path / "outside", root / "link")
    assert fs.list_dir(str(root / "link"), [str(root)])["error"] == fs.ERR_OUTSIDE_ROOTS
    assert fs.within_roots(str(root / "link" / "secret"), [str(root)]) is False


def test_fetch_file_allowlist_cap_and_chunks(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    res = fs.fetch_file(str(root / "proj" / "notes.md"), [str(root)])
    assert res["error"] == "" and res["mime"] == "text/markdown"
    assert base64.b64decode(res["chunks"][0]) == b"# notes\n"
    assert fs.fetch_file(str(root / "proj" / "data.bin"), [str(root)])["error"] == fs.ERR_UNSUPPORTED
    assert fs.fetch_file(str(root / "proj"), [str(root)])["error"] == fs.ERR_NOT_A_FILE
    assert fs.fetch_file(str(tmp_path / "outside" / "x.md"), [str(root)])["error"] == fs.ERR_OUTSIDE_ROOTS
    (root / "empty.txt").write_text("")
    assert fs.fetch_file(str(root / "empty.txt"), [str(root)])["chunks"] == [""]
    monkeypatch.setattr(fs, "MAX_FETCH_BYTES", 4)
    assert fs.fetch_file(str(root / "proj" / "notes.md"), [str(root)])["error"] == fs.ERR_TOO_LARGE
    monkeypatch.setattr(fs, "RAW_CHUNK", 3)
    monkeypatch.setattr(fs, "MAX_FETCH_BYTES", 1 << 20)
    res = fs.fetch_file(str(root / "proj" / "notes.md"), [str(root)])
    assert b"".join(base64.b64decode(c) for c in res["chunks"]) == b"# notes\n"
    assert len(res["chunks"]) == 3


def test_save_attachment_writes_the_image_and_returns_a_path(tmp_path):
    dest = str(tmp_path / "attach")
    img = b"\xff\xd8\xff" + b"jpeg-bytes" * 100  # stand-in bytes; the daemon never inspects them
    chunks = [base64.b64encode(img[i:i + 8]).decode() for i in range(0, len(img), 8)]
    res = fs.save_attachment(chunks, "jpg", dest)
    assert res["error"] == "" and res["size"] == len(img)
    assert res["path"].startswith(dest) and res["path"].endswith(".jpg")
    with open(res["path"], "rb") as f:
        assert f.read() == img
    assert oct(os.stat(res["path"]).st_mode & 0o777) == "0o600"


def test_save_attachment_rejects_bad_type_and_oversize(tmp_path, monkeypatch):
    dest = str(tmp_path / "attach")
    one = base64.b64encode(b"x").decode()
    assert fs.save_attachment([one], "exe", dest)["error"] == fs.ERR_UNSUPPORTED
    assert fs.save_attachment([one], "svg", dest)["error"] == fs.ERR_UNSUPPORTED
    assert fs.save_attachment([], "jpg", dest)["error"] == fs.ERR_NOT_A_FILE
    monkeypatch.setattr(fs, "MAX_ATTACH_BYTES", 4)
    big = base64.b64encode(b"123456789").decode()
    assert fs.save_attachment([big], "png", dest)["error"] == fs.ERR_TOO_LARGE


def test_save_attachment_sweeps_stale_files(tmp_path, monkeypatch):
    import time
    dest = tmp_path / "attach"
    dest.mkdir()
    old = dest / "old.jpg"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - fs.ATTACH_TTL_S - 10,) * 2)
    fresh = dest / "fresh.jpg"
    fresh.write_bytes(b"new")
    fs.save_attachment([base64.b64encode(b"img").decode()], "jpg", str(dest))
    assert not old.exists()   # swept
    assert fresh.exists()     # within the TTL, kept
