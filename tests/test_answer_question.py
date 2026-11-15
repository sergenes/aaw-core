"""The answer's echo in the feed carries its question (0.1.17): a prompt can land between
the two, and "Yes" alone says nothing."""
from __future__ import annotations

from aaw_core.encryption import decrypt, encrypt, generate_key_b64
from aaw_core.transport import relay as relay_module
from aaw_core.transport.relay import RelayTransport

KEY = generate_key_b64()


def transport(tmp_path) -> RelayTransport:
    (tmp_path / "sessions").mkdir(exist_ok=True)
    return RelayTransport(relay_url="wss://example/v1/ws", token="t", computer_id="c", project_id="p",
                          sessions_dir=tmp_path / "sessions", enc_key=KEY)


def answer(t: RelayTransport, question_id: str, text: str = "Yes", **payload) -> dict:
    t._on_frame({"type": "command", "project_id": "p", "command": {
        "id": f"a-{question_id}", "type": "answer", "ts": 1, "consumed": False,
        "payload": {"question_id": question_id, "answer_enc": encrypt(text, KEY), **payload}}})
    assert t.poll_perm_answer_once(question_id) == text
    return [f["event"]["payload"] for f in t._buffer if f.get("type") == "event" and f["event"]["type"] == "message"][-1]


def test_the_echo_quotes_the_question_this_process_asked(tmp_path):
    t = transport(tmp_path)
    qid = t.write_event("question", {"question": "Run: rm -rf build", "options": ["Yes", "No"], "kind": "permission"})
    echo = answer(t, qid)
    assert echo["question_id"] == qid and decrypt(echo["question"], KEY) == "Run: rm -rf build"
    assert decrypt(echo["content"], KEY) == "Yes"
    # the local log, which the desktop feed reads, holds it in the clear like the answer
    local = [e for e in t._log.read() if e.get("role") == "user"][-1]
    assert local["question"] == "Run: rm -rf build" and local["question_id"] == qid


def test_a_question_asked_before_a_restart_is_quoted_from_the_phone(tmp_path):
    t = transport(tmp_path)
    echo = answer(t, "q-old", question_enc=encrypt("Trust this folder?", KEY))
    assert echo["question_id"] == "q-old" and decrypt(echo["question"], KEY) == "Trust this folder?"


def test_the_engines_own_words_win_over_the_phones_quote(tmp_path):
    t = transport(tmp_path)
    qid = t.write_event("question", {"question": "Allow the edit?", "options": ["Yes", "No"]})
    echo = answer(t, qid, question_enc=encrypt("something else", KEY))
    assert decrypt(echo["question"], KEY) == "Allow the edit?"


def test_no_known_question_leaves_the_id_alone(tmp_path):
    t = transport(tmp_path)
    echo = answer(t, "q-unknown")
    assert echo["question_id"] == "q-unknown" and "question" not in echo
    # a quote this key cannot read is dropped, not shown as ciphertext
    echo = answer(t, "q-foreign", question_enc=encrypt("secret", generate_key_b64()))
    assert "question" not in echo


def test_only_the_latest_questions_are_kept_and_a_long_one_is_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(relay_module, "QUESTIONS_REMEMBERED", 2)
    t = transport(tmp_path)
    ids = [t.write_event("question", {"question": f"q{i} " + "x" * 5000, "options": []}) for i in range(3)]
    assert list(t._questions) == ids[1:]
    assert "question" not in answer(t, ids[0])
    assert len(decrypt(answer(t, ids[2])["question"], KEY)) == relay_module.QUESTION_QUOTE_MAX
