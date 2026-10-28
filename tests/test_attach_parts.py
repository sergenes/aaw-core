"""Files attached to a prompt, sent in parts (0.1.12): the relay caps a frame at 1 MiB, so a
PDF or a short video travels as one fs_attach request per RAW_CHUNK slice and the computer
joins them. The single-frame upload of today's phones keeps working unchanged."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import threading
import time

import pytest
import uvicorn
from test_relay import _phone_connect, _recv_until

from aaw_core.config import Settings
from aaw_core.encryption import decrypt, encrypt, generate_key_b64
from aaw_core.host import fs
from aaw_core.host.identity import load_or_create_identity
from aaw_core.host.supervisor import Supervisor
from aaw_core.relay.server import WS_MAX_SIZE, create_app
from aaw_core.transport.relay import RelayTransport

UPLOAD = "u_3f9a1c2b7d"


def _parts(data: bytes) -> list[str]:
    return [base64.b64encode(data[i:i + fs.RAW_CHUNK]).decode() for i in range(0, len(data), fs.RAW_CHUNK)]


def _send_all(dest, data: bytes, suffix: str, order=None, upload_id: str = UPLOAD) -> list[dict]:
    parts = _parts(data)
    return [fs.save_attachment_part(upload_id, i, len(parts), parts[i], suffix, dest)
            for i in (order if order is not None else range(len(parts)))]


# ── fs: staging, assembly, and the rules ───────────────────────────────────────


def test_a_file_in_parts_is_joined_in_order(tmp_path):
    dest = str(tmp_path / "attach")
    data = os.urandom(fs.RAW_CHUNK * 3 + 1234)  # four parts, the last one short
    replies = _send_all(dest, data, "pdf", order=[2, 0, 3, 1])  # arrival order does not matter
    assert [r["received"] for r in replies] == [2, 0, 3, 1]
    assert all(r["error"] == "" for r in replies)
    assert [bool(r["path"]) for r in replies] == [False, False, False, True]
    path = replies[-1]["path"]
    assert path.endswith(".pdf") and replies[-1]["size"] == len(data)
    with open(path, "rb") as f:
        assert f.read() == data
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert not os.listdir(os.path.join(dest, fs.ATTACH_PARTS_DIR))  # staging cleaned up


def test_a_resent_part_is_harmless(tmp_path):
    dest = str(tmp_path / "attach")
    data = os.urandom(fs.RAW_CHUNK + 10)
    replies = _send_all(dest, data, "mov", order=[0, 0, 1])  # the reply to part 0 got lost
    assert replies[-1]["path"]
    with open(replies[-1]["path"], "rb") as f:
        assert f.read() == data


def test_a_changed_total_or_suffix_is_refused(tmp_path):
    dest = str(tmp_path / "attach")
    one = base64.b64encode(b"x" * 100).decode()
    assert fs.save_attachment_part(UPLOAD, 0, 3, one, "pdf", dest)["error"] == ""
    assert fs.save_attachment_part(UPLOAD, 1, 4, one, "pdf", dest)["error"] == fs.ERR_DENIED
    assert not os.path.exists(os.path.join(dest, fs.ATTACH_PARTS_DIR, UPLOAD))  # dropped
    assert fs.save_attachment_part(UPLOAD, 0, 3, one, "pdf", dest)["error"] == ""
    assert fs.save_attachment_part(UPLOAD, 1, 3, one, "mp4", dest)["error"] == fs.ERR_DENIED


def test_the_limits(tmp_path):
    dest = str(tmp_path / "attach")
    one = base64.b64encode(b"x").decode()
    assert fs.save_attachment_part(UPLOAD, 0, 1, one, "exe", dest)["error"] == fs.ERR_UNSUPPORTED
    assert fs.save_attachment_part(UPLOAD, 0, 1, one, "svg", dest)["error"] == fs.ERR_UNSUPPORTED
    assert fs.save_attachment_part("../../etc", 0, 1, one, "pdf", dest)["error"] == fs.ERR_DENIED
    assert fs.save_attachment_part(UPLOAD, 1, 1, one, "pdf", dest)["error"] == fs.ERR_DENIED  # past the total
    assert fs.save_attachment_part(UPLOAD, 0, fs.MAX_ATTACH_PARTS + 1, one, "pdf", dest)["error"] == fs.ERR_TOO_LARGE
    big = base64.b64encode(b"x" * (fs.RAW_CHUNK + 1)).decode()
    assert fs.save_attachment_part(UPLOAD, 0, 2, big, "pdf", dest)["error"] == fs.ERR_TOO_LARGE  # one slice per part
    assert fs.save_attachment_part(UPLOAD, 0, 2, "not base64!", "pdf", dest)["error"] == fs.ERR_DENIED
    assert fs.save_attachment_part(UPLOAD, 0, 2, "", "pdf", dest)["error"] == fs.ERR_NOT_A_FILE


def test_the_size_cap_counts_what_is_staged(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_ATTACH_PARTS_BYTES", fs.RAW_CHUNK + 10)
    dest = str(tmp_path / "attach")
    replies = _send_all(dest, os.urandom(fs.RAW_CHUNK * 2), "mp4")
    assert replies[0]["error"] == "" and replies[1]["error"] == fs.ERR_TOO_LARGE
    assert not os.path.exists(os.path.join(dest, fs.ATTACH_PARTS_DIR, UPLOAD))


def test_an_abandoned_upload_is_swept(tmp_path):
    dest = str(tmp_path / "attach")
    one = base64.b64encode(b"x" * 10).decode()
    fs.save_attachment_part("u_abandoned1", 0, 2, one, "pdf", dest)
    stale = os.path.join(dest, fs.ATTACH_PARTS_DIR, "u_abandoned1")
    old = time.time() - fs.ATTACH_PARTS_TTL_S - 60
    os.utime(stale, (old, old))
    fs.save_attachment_part(UPLOAD, 0, 2, one, "pdf", dest)  # a new upload sweeps first
    assert not os.path.exists(stale)
    assert os.path.exists(os.path.join(dest, fs.ATTACH_PARTS_DIR, UPLOAD))
    # an upload resumed under its own id after the TTL starts over instead of failing
    mine = os.path.join(dest, fs.ATTACH_PARTS_DIR, UPLOAD)
    os.utime(mine, (old, old))
    assert fs.save_attachment_part(UPLOAD, 0, 2, one, "pdf", dest)["error"] == ""
    assert fs.save_attachment_part(UPLOAD, 1, 2, one, "pdf", dest)["path"]


def test_a_join_that_fails_leaves_no_half_written_file(tmp_path, monkeypatch):
    dest = str(tmp_path / "attach")
    one = base64.b64encode(b"x" * 10).decode()
    fs.save_attachment_part(UPLOAD, 0, 2, one, "pdf", dest)
    real = shutil.copyfileobj

    def boom(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(shutil, "copyfileobj", boom)
    assert fs.save_attachment_part(UPLOAD, 1, 2, one, "pdf", dest)["error"] == fs.ERR_DENIED
    monkeypatch.setattr(shutil, "copyfileobj", real)
    assert [n for n in os.listdir(dest) if n != fs.ATTACH_PARTS_DIR] == []


def test_documents_videos_and_source_files_are_accepted():
    for ext in (".pdf", ".mov", ".mp4", ".txt", ".md", ".log", ".csv", ".json", ".py", ".swift", ".kt", ".heic"):
        assert ext in fs.ATTACH_SUFFIXES, ext
    for ext in (".exe", ".svg", ".zip", ".dmg", ".app", ".sh.exe"):
        assert ext not in fs.ATTACH_SUFFIXES, ext


# ── the supervisor request ─────────────────────────────────────────────────────


KEY = generate_key_b64()


@pytest.fixture
def sup(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", relay_url="ws://x", computer_name="box", keep_awake=False)
    settings.state_dir.mkdir()
    settings.session_key_file.write_text(KEY)
    return Supervisor(settings)


def test_the_request_acknowledges_each_part_and_returns_the_path(sup):
    data = os.urandom(fs.RAW_CHUNK + 500)
    parts = _parts(data)
    first = sup.handle_request("fs_attach", {"upload_id": UPLOAD, "part": 0, "total_parts": 2, "suffix": "pdf",
                                             "chunks": [encrypt(parts[0], KEY)]})
    assert first == {"received": 0, "size": fs.RAW_CHUNK, "error": ""}
    last = sup.handle_request("fs_attach", {"upload_id": UPLOAD, "part": 1, "total_parts": 2, "suffix": "pdf",
                                            "chunks": [encrypt(parts[1], KEY)]})
    assert last["received"] == 1 and last["error"] == ""
    with open(decrypt(last["path_enc"], KEY), "rb") as f:
        assert f.read() == data


def test_the_request_refuses_a_part_it_cannot_read(sup):
    other = generate_key_b64()
    one = base64.b64encode(b"x").decode()
    payload = {"upload_id": UPLOAD, "part": 0, "total_parts": 1, "suffix": "pdf"}
    assert sup.handle_request("fs_attach", {**payload, "chunks": [encrypt(one, other)]})["error"] == fs.ERR_DENIED
    assert sup.handle_request("fs_attach", {**payload, "chunks": []})["error"] == fs.ERR_DENIED
    assert sup.handle_request("fs_attach", {**payload, "chunks": [encrypt(one, KEY)] * 2})["error"] == fs.ERR_DENIED


# ── end to end: a phone, the production relay frame cap, a real supervisor ────


@pytest.fixture
def capped_relay(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    app = create_app(str(tmp_path / "relay.sqlite"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           ws_max_size=WS_MAX_SIZE))  # the production cap
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"ws://127.0.0.1:{port}/v1/ws"
    server.should_exit = True
    thread.join(timeout=5)


def test_a_three_megabyte_pdf_reaches_the_computer_through_the_relay(tmp_path, capped_relay, monkeypatch):
    """The phone sends each part as its own request and waits for its reply, as it does for
    any request; every frame stays under the relay's 1 MiB cap, and the reply to the last
    part names the file. The same file in one frame is what the relay refuses."""
    monkeypatch.setenv("AAW_RELAY_URL", capped_relay)
    settings = Settings(state_dir=tmp_path / "state", relay_url=capped_relay, computer_name="box", keep_awake=False)
    settings.state_dir.mkdir()
    settings.session_key_file.write_text(KEY)
    ident = load_or_create_identity(settings)
    transport = RelayTransport(relay_url=capped_relay, token=ident.token, computer_id=ident.computer_id,
                               project_id="_supervisor", sessions_dir=settings.sessions_dir, enc_key=KEY).start()
    sup = Supervisor(settings, transport=transport)
    serving = threading.Thread(target=sup._serve_loop, daemon=True)
    try:
        assert transport.wait_connected(10)
        serving.start()
        phone = _phone_connect(capped_relay, transport.register_phone_token())
        data = os.urandom(3 * 1024 * 1024 + 4321)
        parts = _parts(data)
        assert len(parts) == 9
        path_enc = ""
        for i, chunk in enumerate(parts):
            frame = json.dumps({"type": "request", "req": f"a{i}", "kind": "fs_attach",
                                "payload": {"upload_id": UPLOAD, "part": i, "total_parts": len(parts),
                                            "suffix": "pdf", "chunks": [encrypt(chunk, KEY)]}})
            assert len(frame.encode()) < WS_MAX_SIZE
            phone.send(frame)
            reply = _recv_until(phone, lambda f, i=i: f.get("type") == "response" and f.get("req") == f"a{i}",
                                timeout=15)["payload"]
            assert reply["error"] == "" and reply["received"] == i
            path_enc = reply.get("path_enc", "")
        path = decrypt(path_enc, KEY)
        assert path.startswith(str(settings.attachments_dir)) and path.endswith(".pdf")
        with open(path, "rb") as f:
            assert f.read() == data
        # the whole file in one frame, the way a 2.11 phone sends a photo, is over the cap
        whole = json.dumps({"type": "request", "req": "w", "kind": "fs_attach",
                            "payload": {"suffix": "pdf", "chunks": [encrypt(c, KEY) for c in parts]}})
        assert len(whole.encode()) > WS_MAX_SIZE
        phone.close()
    finally:
        sup.running = False
        transport.stop(flush_timeout=0)
