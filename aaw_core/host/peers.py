"""Agent sessions on one computer that know about each other.

An agent proposes two things with the `aaw` command: a note for the shared knowledge
base (`aaw know add`), and a message to another session (`aaw tell`). Neither happens
until the user approves it on a card, and only the owner's answer counts, never a
guest's. The command itself returns at once: a tool call has a time limit and the user
may take longer, so a small worker process (this module's `main`) outlives the call,
asks, and acts on the answer.

A message is delivered as a prompt behind a mark that names its sender. When the
receiving agent's turn ends, its answer goes back to the sender behind an answer mark,
with no second card. That is the whole exchange one approval covers: an answer never
earns another free answer, so two agents cannot talk in a loop without the user.

The card is the agreed road for well-behaved agents and the user's view of what crosses
between sessions. It is not a security wall: an agent with a shell could type into
another session's terminal. The agent's own permission system is that wall."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from aaw_core.config import Settings, load_settings
from aaw_core.host import knowledge, sessions, state

MAX_MESSAGE = 4000  # characters; a longer hand-off belongs in a file the message names
ASK_TIMEOUT_S = 600  # no answer on the card means no
TURN_WAIT_S = 600  # how long the worker waits for the sender's turn to end before asking
SETTLE_S = 3.0  # after the turn ends the daemon writes "idle" once; ask after that write
CARD_CHECK_S = 3.0  # how often the worker looks whether its card is still the session's pending one
ANSWER_WAIT_S = 2 * 3600  # an answer that takes longer is dropped
GUEST_RETRIES = 3
SEND, DONT_SEND = "Send", "Don't send"
SAVE, DONT_SAVE = "Save", "Don't save"
REMOVE, KEEP = "Remove", "Keep"

OFF_HINT = ("Messages between agents and note proposals are off on this computer. "
            "The user turns them on with `aaw peers on` or in the Mac app's menu.")


def label(agent: str, project: str) -> str:
    """ "Claude in agent-bridge": how one session is named to the user and to another agent."""
    return f"{(agent or 'agent').capitalize()} in {project}"


def message_mark(agent: str, project: str) -> str:
    return f"[from {label(agent, project)}, approved by the user]"


def answer_mark(agent: str, project: str) -> str:
    return f"[answer from {label(agent, project)}]"


def notice(text: str) -> str:
    """A prompt the engine itself types into a session about a proposal's fate."""
    return f"[aaw] {text}"


# ── the other sessions ───────────────────────────────────────────────────────


def others(settings: Settings, me: str = "") -> list[dict]:
    """Every running session, most recently used first: id, agent, folder, status and
    whether it is the caller's own."""
    docs = state.read_projects(state.mirror_dir(settings.state_dir))
    rows = []
    for s in sessions.list_sessions():
        doc = docs.get(s.project) or {}
        rows.append({
            "id": s.project, "agent": sessions.session_agent(s.project) or doc.get("agent") or "",
            "path": s.path, "status": doc.get("status") or "running",
            "last_event_ts": int(doc.get("last_event_ts") or 0), "me": s.project == me,
        })
    return sorted(rows, key=lambda r: (-r["last_event_ts"], r["id"]))


# ── requests and the worker that carries them out ────────────────────────────


def _dir(settings: Settings, kind: str) -> Path:
    return settings.state_dir / "peer" / kind


def pending_request(settings: Settings, project: str) -> bool:
    """Whether this session already has a proposal waiting for the user."""
    limit = time.time() - (TURN_WAIT_S + ASK_TIMEOUT_S * (GUEST_RETRIES + 1) + 60)
    for f in _dir(settings, "requests").glob("*.json"):
        try:
            if f.stat().st_mtime < limit:
                f.unlink(missing_ok=True)  # a worker that died: do not block the session for good
                continue
            if json.loads(f.read_text()).get("from") == project:
                return True
        except (OSError, ValueError):
            continue
    return False


def submit(settings: Settings, request: dict) -> Path:
    """Leave a request and start the worker that asks the user about it. The worker is
    its own process group: it must outlive the tool call that proposed the request."""
    folder = _dir(settings, "requests")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.json"
    path.write_text(json.dumps({**request, "ts": time.time()}))
    log = settings.logs_dir / "peers.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        subprocess.Popen([sys.executable, "-m", "aaw_core.host.peers", str(path)],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    return path


def _log(settings: Settings, msg: str) -> None:
    from aaw_core.hooks.common import hook_log
    hook_log(settings, "peers", msg)


def _wait_for_turn_end(transport, wait_s: float = TURN_WAIT_S) -> str:
    """The sender's status once its turn is over (or after the wait). The card goes out
    after the turn because the end of a turn clears the session's pending question, and
    a card sent before it would vanish from the phone."""
    end = time.time() + wait_s
    status = "running"
    while time.time() < end:
        try:
            status = transport.get_project().get("status") or "running"
        except Exception:  # noqa: BLE001 - a failed read is not a reason to give up the request
            status = "running"
        if status != "running":
            time.sleep(SETTLE_S)
            break
        time.sleep(2)
    return status


def _await_answer(transport, timeout_s: float) -> str | None:
    """Wait for the answer, keeping the card on the phone meanwhile. The session is idle
    while it waits, and the daemon's idle write (or a late Stop) clears the session's
    pending question; a card that is no longer pending disappears from the phone, so it is
    put back as the pending one."""
    deadline = time.time() + timeout_s
    question_id = getattr(transport, "_question_id", None)
    while time.time() < deadline:
        answer = transport.poll_answer(min(deadline, time.time() + CARD_CHECK_S))
        if answer is not None:
            return answer
        try:
            if question_id and transport.get_project().get("pending_question_id") != question_id:
                transport.set_project_status("waiting", pending_question_id=question_id)
        except Exception:  # noqa: BLE001, S110 - the next look tries again
            pass
    return None


def ask(transport, project: str, agent: str, question: str, context: str, options: list[str],
        timeout_s: float | None = None) -> str | None:
    """Put a card to the user and return the owner's answer, or None when nobody
    answered. A guest of a shared session sees the card too; their answer is not the
    owner's, so the card is put again."""
    timeout_s = ASK_TIMEOUT_S if timeout_s is None else timeout_s
    for _ in range(GUEST_RETRIES + 1):
        transport.send_question(project=project, agent=agent, question=question, context=context,
                                options=options, kind="choice", timeout_s=int(timeout_s))
        answer = _await_answer(transport, timeout_s)
        if answer is None:
            return None
        if not getattr(transport, "last_answer_from", None):
            return answer
        transport.write_notification("Only the owner of this computer can approve this. Asking again.", "info")
    return None


def run_request(settings: Settings, request: dict, open_transport) -> str:
    """Ask about one request and act on the answer. Returns what happened, for the log.
    `open_transport(settings, project)` gives a connected relay transport or None."""
    sender, agent, kind = request["from"], request.get("agent") or "", request["kind"]
    who = label(agent, sender)
    transport = open_transport(settings, sender)
    if transport is None:
        return "not linked to a relay: nobody can be asked"
    try:
        before = _wait_for_turn_end(transport)
        if kind == "tell":
            target, target_agent, text = request["to"], request.get("to_agent") or "", request["text"]
            answer = ask(transport, sender, agent, f"{who} wants to message {label(target_agent, target)}",
                         text, [SEND, DONT_SEND])
            if answer == SEND:
                outcome = deliver(settings, request, open_transport)
                if outcome:
                    transport.write_command(notice(f"Your message to {target} was not delivered: {outcome}."))
                    result = f"not delivered: {outcome}"
                else:
                    transport.write_notification(f"Message sent to {label(target_agent, target)}", "info")
                    result = "sent"
            else:
                why = "the user did not approve it" if answer else "the user did not answer"
                transport.write_command(notice(f"Your message to {target} was not sent: {why}."))
                result = f"not sent: {why}"
        elif kind == "know_add":
            note = knowledge.Note(**request["note"])
            answer = ask(transport, sender, agent, f"{who} wants to save a note: {note.title}",
                         f"{note.description}\n\n{note.body}".strip()
                         + "\n\nEvery agent on this computer can read a saved note.", [SAVE, DONT_SAVE])
            if answer == SAVE:
                knowledge.write(settings, note)
                transport.write_notification(f"Note saved: {note.name}", "info")
                result = "saved"
            else:
                result = "not saved"
        elif kind == "know_remove":
            name = request["name"]
            note = knowledge.read(settings, name)
            answer = ask(transport, sender, agent, f"{who} wants to remove the note: {name}",
                         (note.body if note else ""), [REMOVE, KEEP])
            if answer == REMOVE and knowledge.remove(settings, name):
                transport.write_notification(f"Note removed: {name}", "info")
                result = "removed"
            else:
                result = "kept"
        else:
            result = f"unknown request kind {kind!r}"
        # An answer marks the session "running"; it was idle when we asked, and still is.
        transport.set_project_status(before if before in ("idle", "waiting", "stopped") else "idle",
                                     pending_question_id="")
        return result
    finally:
        transport.stop()


def deliver(settings: Settings, request: dict, open_transport) -> str:
    """Type the approved message into the receiving session and note that its answer is
    owed to the sender. Returns "" when delivered, else why not."""
    target = request["to"]
    if not sessions.session_alive(target):
        return f"{target} is not running any more"
    receiver = open_transport(settings, target)
    if receiver is None:
        return "the relay could not be reached"
    try:
        mark = message_mark(request.get("agent") or "", request["from"])
        owed = _dir(settings, "awaiting")
        owed.mkdir(parents=True, exist_ok=True)
        (owed / f"{target}.json").write_text(json.dumps({
            "to": request["from"], "to_agent": request.get("agent") or "", "mark": mark,
            "ts_ms": int(time.time() * 1000)}))
        receiver.write_command(f"{mark} {request['text']}")
        receiver.flush()
    finally:
        receiver.stop()
    return ""


# ── the answer on its way back ───────────────────────────────────────────────


def owed_answer(settings: Settings, project: str, log_entries: list[dict], now: float | None = None) -> dict | None:
    """The sender this session owes an answer to, when the turn that just ended was the
    one that read the sender's message: the newest prompt in the session's log is that
    message. A turn that was already running when the message arrived ends first, and
    owes nothing yet. None when nothing is owed now."""
    path = _dir(settings, "awaiting") / f"{project}.json"
    try:
        owed = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    if now * 1000 - owed.get("ts_ms", 0) > ANSWER_WAIT_S * 1000:
        path.unlink(missing_ok=True)
        return {**owed, "expired": True}
    prompts = [e for e in log_entries if e.get("type") == "message" and e.get("role") == "user"]
    if not prompts:
        return None
    last = prompts[-1]
    if last.get("ts", 0) < owed.get("ts_ms", 0) or not str(last.get("content") or "").startswith(owed.get("mark", "\0")):
        return None
    path.unlink(missing_ok=True)
    return owed


def return_answer(settings: Settings, project: str, agent: str, response_text: str, open_transport) -> str:
    """Called when a session's turn ends (the Stop hook): if that turn answered another
    agent's message, type the answer into the sender's session. Returns what was done,
    "" when nothing was owed. Never raises: a hook must not fail on this."""
    try:
        from aaw_core.transport.base import LocalLog
        owed = owed_answer(settings, project, LocalLog(settings.sessions_dir, project).read())
        if owed is None:
            return ""
        sender = owed["to"]
        if owed.get("expired"):
            text = notice(f"No answer came from {project} within {ANSWER_WAIT_S // 3600} hours.")
        elif not response_text.strip():
            text = notice(f"{project} read your message and finished its turn without an answer.")
        else:
            body = response_text.strip()
            if len(body) > MAX_MESSAGE:
                body = body[:MAX_MESSAGE] + "\n[the answer was cut here; ask for the rest or for a file]"
            text = f"{answer_mark(agent, project)} {body}"
        if not sessions.session_alive(sender):
            return f"answer for {sender} dropped: it is not running"
        transport = open_transport(settings, sender)
        if transport is None:
            return f"answer for {sender} dropped: no relay"
        try:
            transport.write_command(text)
            transport.flush()
        finally:
            transport.stop()
        return f"answer returned to {sender}"
    except Exception as e:  # noqa: BLE001
        return f"answer not returned: {e}"


def main(argv: list[str] | None = None) -> int:
    """The worker: `python -m aaw_core.host.peers <request file>`."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        return 2
    path = Path(argv[0])
    settings = load_settings()
    try:
        request = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        _log(settings, f"unreadable request {path.name}: {e}")
        return 1
    from aaw_core.hooks.common import open_transport
    try:
        result = run_request(settings, request, open_transport)
    except Exception as e:  # noqa: BLE001 - the log is the only place a worker can say what went wrong
        result = f"failed: {e}"
    finally:
        path.unlink(missing_ok=True)
    _log(settings, f"{request.get('kind')} from {request.get('from')}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
