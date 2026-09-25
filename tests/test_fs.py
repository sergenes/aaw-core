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
