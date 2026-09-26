"""The supervisor: the always-on part of the host.

Runs as a user service (systemd on Linux, launchd on macOS) or in the foreground via
``aaw supervisor``:
  - keeps <state dir>/enabled present while running, removes it on shutdown
  - registers hooks for every installed agent on start
  - heartbeat to the computer document every 60 s (status, last seen, detected agents)
  - on start: restarts every daemon for a live tmux session (fresh code, no ghosts)
  - every 30 s: orphan restart, stopped-session reconcile, stuck-command restart, and an
    optional desktop "waiting for your answer" alert
  - answers the phone's requests: start a stopped session, start a new one at a browsed
    folder, list a folder, read a file (paths fenced to the browse roots; names, paths
    and contents encrypted with the session key)
"""

from __future__ import annotations

import os
import queue
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag

from aaw_core import encryption
from aaw_core.config import Settings, load_settings
from aaw_core.hooks.common import desktop_banner, read_key, set_mobile_mode
from aaw_core.host import fs, hooks_installer, sessions
from aaw_core.host.identity import load_identity
from aaw_core.host.session_id import resolve
from aaw_core.transport.relay import RelayTransport

HEARTBEAT_EVERY = 60
WATCHDOG_EVERY = 30
STOPPED_STREAK = 4  # ~2 min at the watchdog cadence before a missing session is written stopped
STUCK_COMMAND_AGE = 90
SUPERVISOR_PROJECT = "_supervisor"  # its own channel on the relay; never a session id


def platform_name() -> str:
    return "macos" if sys.platform == "darwin" else "linux"


class Supervisor:
    def __init__(self, settings: Settings, *, transport: RelayTransport | None = None):
        self.settings = settings
        self.running = True
        self.transport = transport
        self.enc_key = read_key(settings)
        self.cache: dict[str, dict] = {}  # project id -> {status, agent, path, pending_question_id}
        self.missing_streak: dict[str, int] = {}
        self.pending_since: dict[str, float] = {}
        self.alerted_questions: set[str] = set()
        self.computer_name = settings.computer_name

    # ── logging ───────────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        line = f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        try:
            self.settings.logs_dir.mkdir(parents=True, exist_ok=True)
            with self.settings.supervisor_log_file.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ── setup ─────────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """The supervisor's own relay connection. Waits (with a periodic hint) until the host
        is linked; returns False only when asked to stop meanwhile."""
        warned = 0.0
        while self.running:
            ident = load_identity(self.settings)
            if self.settings.relay_url and ident is not None:
                self.computer_name = ident.computer_name
                self.enc_key = read_key(self.settings)
                self.transport = RelayTransport(
                    relay_url=self.settings.relay_url, token=ident.token, computer_id=ident.computer_id,
                    project_id=SUPERVISOR_PROJECT, sessions_dir=self.settings.sessions_dir,
                    computer_name=ident.computer_name, enc_key=self.enc_key,
                ).start()
                return True
            if time.time() - warned > 60:
                self.log("waiting: set AAW_RELAY_URL and run `aaw link` on this machine")
                warned = time.time()
            time.sleep(5)
        return False

    def decrypt(self, value) -> str:
        """Plaintext, or "" when the value is encrypted and cannot be read here (no key, or a
        re-paired key). Ciphertext is never returned as if it were a path."""
        if isinstance(value, str) and encryption.is_encrypted(value):
            if not self.enc_key:
                return ""
            try:
                return encryption.decrypt(value, self.enc_key)
            except (InvalidTag, ValueError, KeyError):
                return ""
        return value or ""

    def encrypt(self, value: str | None) -> str | None:
        if not value:
            return value
        return encryption.encrypt(value, self.enc_key) if self.enc_key else value

    # ── duties ────────────────────────────────────────────────────────────────

    def heartbeat(self) -> None:
        try:
            self.transport.update_computer(
                status="online", platform=platform_name(), detected_agents=hooks_installer.detected_agents(),
                scoot_models=sessions.scoot_models() or [])
        except Exception as e:  # noqa: BLE001 - a heartbeat must never stop the loop
            self.log(f"heartbeat failed: {e}")

    def refresh_cache(self) -> None:
        try:
            docs = self.transport.list_projects()
        except Exception as e:  # noqa: BLE001
            self.log(f"project read failed: {e}")
            return
        self.cache = {
            pid: {"status": d.get("status") or "running", "agent": d.get("agent"),
                  "path": self.decrypt(d.get("project_path")),
                  "pending_question_id": d.get("pending_question_id") or ""}
            for pid, d in docs.items() if not pid.startswith("_")
        }

    def agent_for(self, project: str) -> str:
        """The live tmux session's own AAW_AGENT is authoritative for what runs in the pane
        now; the previous daemon's --agent and the cache go stale once a folder that ran
        Claude is reused by another agent."""
        return (sessions.session_agent(project) or sessions.running_daemon_agent(self.settings, project)
                or (self.cache.get(project) or {}).get("agent") or "claude")

    def restart_all_daemons(self) -> None:
        live = sessions.list_sessions()
        if not live:
            return
        self.log(f"start: restarting {len(live)} daemon(s) for live session(s)")
        for s in live:
            sessions.start_daemon(self.settings, Path(s.path), s.project, self.agent_for(s.project))

    def restart_orphans(self) -> None:
        for s in sessions.list_sessions():
            if not sessions.daemon_pid(self.settings, s.project):
                self.log(f"orphan: {s.project} has no daemon, restarting")
                sessions.start_daemon(self.settings, Path(s.path), s.project, self.agent_for(s.project))

    def reconcile_stopped(self) -> None:
        live = {s.project for s in sessions.list_sessions()}
        tracked = set()
        for pid, data in list(self.cache.items()):
            if data.get("status") == "stopped" or pid in live:
                continue
            tracked.add(pid)
            streak = self.missing_streak.get(pid, 0) + 1
            self.missing_streak[pid] = streak
            if streak < STOPPED_STREAK:
                continue
            self.log(f"reconcile: {pid} has no live tmux session after {streak} checks, writing stopped")
            self.transport.set_project_fields(
                pid, {"status": "stopped", "last_event_ts": int(time.time()), "pending_question_id": ""})
            data["status"] = "stopped"
        self.missing_streak = {k: v for k, v in self.missing_streak.items() if k in tracked}

    def restart_stuck(self) -> None:
        """A live session whose oldest unconsumed command is old has a wedged daemon."""
        for s in sessions.list_sessions():
            try:
                docs = self.transport.list_commands(s.project)
            except Exception as e:  # noqa: BLE001 - the relay may be away; try the next session
                self.log(f"stuck check skipped for {s.project}: {e}")
                continue
            oldest = min((d.get("ts") or 0 for d in docs if d.get("type") == "command"
                          and not d.get("deliver_at")), default=None)
            if not oldest:
                continue
            age = time.time() - oldest / 1000.0
            if age >= STUCK_COMMAND_AGE:
                self.log(f"stuck: {s.project} has an unconsumed command for {int(age)} s, restarting daemon")
                sessions.start_daemon(self.settings, Path(s.path), s.project, self.agent_for(s.project))

    def check_waiting_alerts(self) -> None:
        """A desktop "waiting for your answer" banner when a phone question sits unanswered
        past waiting_alert_seconds (0 = off), for when you are at the desk and the push is
        muted."""
        threshold = self.settings.waiting_alert_seconds
        if threshold <= 0 or not self.settings.local_notifications:
            self.pending_since.clear()
            return
        now = time.time()
        still_waiting: set[str] = set()
        for pid, c in self.cache.items():
            qid = c.get("pending_question_id") or ""
            if not qid:
                continue
            still_waiting.add(pid)
            since = self.pending_since.setdefault(pid, now)
            if qid in self.alerted_questions or (now - since) < threshold:
                continue
            self.alerted_questions.add(qid)
            desktop_banner(f"{pid} is waiting for your answer", (c.get("agent") or "agent").capitalize())
        self.pending_since = {p: t for p, t in self.pending_since.items() if p in still_waiting}
        if len(self.alerted_questions) > 500:
            self.alerted_questions.clear()

    # ── phone requests ────────────────────────────────────────────────────────

    def handle_request(self, kind: str, payload: dict) -> dict:
        """Answer one request. Every error is a result code the phone renders; nothing
        here raises to the loop."""
        handler = {
            "start_session": self.req_start_session, "stop_session": self.req_stop_session,
            "new_session": self.req_new_session, "fs_browse": self.req_fs_browse, "fs_fetch": self.req_fs_fetch,
        }.get(kind)
        if handler is None:
            return {"error": "unknown_request"}
        try:
            return handler(payload or {})
        except Exception as e:  # noqa: BLE001 - one bad request must not take the supervisor down
            self.log(f"request {kind} failed: {e}")
            return {"error": "error"}

    def req_start_session(self, payload: dict) -> dict:
        """Restart a stopped session under its own id (the phone's "Start" on a stopped card)."""
        pid = str(payload.get("project_id") or "")
        self.refresh_cache()
        doc = self.cache.get(pid)
        if not pid or doc is None:
            return {"result": "not_found"}
        if sessions.session_alive(pid):
            return {"result": "started", "session_id": pid}
        path, agent = doc["path"], doc.get("agent") or "claude"
        if not path:
            return {"result": "error"}  # a path this key cannot read
        if not Path(path).is_dir():
            self.log(f"remote start refused for {pid}: folder not found: {path}")
            self.transport.set_project_fields(pid, {
                "status": "stopped",
                "last_event_summary": self.encrypt(f"Folder not found on {self.computer_name}: {path}")})
            return {"result": "not_found"}
        return self._launch(pid, Path(path), agent, payload.get("model"))

    def req_stop_session(self, payload: dict) -> dict:
        pid = str(payload.get("project_id") or "")
        if not pid or not sessions.session_alive(pid):
            return {"result": "not_found"}
        for line in sessions.stop_session(self.settings, pid):
            self.log(line)
        return {"result": "stopped", "session_id": pid}

    def req_new_session(self, payload: dict) -> dict:
        """Launch a brand-new session at a browsed folder. The id comes from the same
        resolver `aaw start` uses; the path is fenced to the browse roots even though the
        browser only offers paths this computer returned."""
        path = self.decrypt(payload.get("path_enc") or "")
        agent = payload.get("agent") or "claude"
        model = payload.get("model") or None
        intent = payload.get("intent")  # "parallel" waives the conflict check
        if not path:
            return {"result": "error"}
        if not fs.within_roots(path, self.settings.browse_roots):
            return {"result": "outside_roots"}
        if not os.path.exists(path):
            return {"result": "not_found"}
        if not os.path.isdir(path):
            return {"result": "not_a_dir"}
        if agent not in hooks_installer.detected_agents():
            return {"result": "agent_unavailable"}
        self.refresh_cache()
        known = sessions.known_sessions(
            {pid: {"project_path": c["path"], "agent": c.get("agent"), "status": c.get("status")}
             for pid, c in self.cache.items() if c.get("path")}, lambda v: v)
        res = resolve(fs.canon(path), agent, known)
        if res.action == "attach":
            return {"result": "started", "session_id": res.id}
        if res.alongside and intent != "parallel":
            out = {"result": "conflict", "conflict_agents": [k.agent for k in res.alongside if k.agent]}
            if len(res.alongside) == 1:
                out["conflict_session_id"] = res.alongside[0].id
            return out
        return self._launch(res.id, Path(fs.canon(path)), agent, model)

    def _launch(self, pid: str, path: Path, agent: str, model) -> dict:
        self.log(f"remote start: {pid} ({agent}) at {path}")
        set_mobile_mode(self.settings)  # phone activity: the session's first prompts go to the phone
        try:
            for line in sessions.start_session(self.settings, path, pid, agent, model=model):
                self.log(line)
        except sessions.SessionError as e:
            self.log(f"remote start failed for {pid}: {e}")
            return {"result": "error"}
        return {"result": "started", "session_id": pid}

    def req_fs_browse(self, payload: dict) -> dict:
        if not self.enc_key:
            return {"error": fs.ERR_DENIED}  # names cannot leave the computer unencrypted
        path_enc = payload.get("path_enc")
        path = self.decrypt(path_enc) if path_enc else None
        if path_enc and not path:
            return {"error": fs.ERR_DENIED}  # ciphertext this key cannot read: not "home"
        res = fs.list_dir(path, self.settings.browse_roots)
        entries = []
        for e in res["entries"]:
            row = {"name_enc": self.encrypt(e["name"]), "kind": e["kind"], "is_repo": e["is_repo"]}
            if "size" in e:
                row["size"] = e["size"]
            entries.append(row)
        return {
            "resolved_path_enc": self.encrypt(res["resolved_path"]),
            "parent_enc": self.encrypt(res["parent"]),
            "at_root": res["at_root"], "entries": entries, "truncated": res["truncated"], "error": res["error"],
        }

    def req_fs_fetch(self, payload: dict) -> dict:
        if not self.enc_key:
            return {"error": fs.ERR_DENIED, "total_chunks": 0}
        path = self.decrypt(payload.get("path_enc") or "")
        if not path:
            return {"error": fs.ERR_DENIED, "total_chunks": 0}
        res = fs.fetch_file(path, self.settings.browse_roots)
        if res["error"]:
            return {"error": res["error"], "total_chunks": 0}
        return {"mime": res["mime"], "size": res["size"], "total_chunks": len(res["chunks"]),
                "chunks": [self.encrypt(c) for c in res["chunks"]], "error": ""}

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        def stop(signum, frame):
            self.running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        hooks_installer.extend_path()  # a service's PATH lacks ~/.local/bin and friends
        self.settings.enabled_flag.write_text(str(int(time.time())))
        self.log(f"supervisor starting (state dir {self.settings.state_dir})")
        try:
            hooks_installer.install_all(self.log)
        except OSError as e:
            self.log(f"hook install failed: {e}")
        if self.transport is None and not self.connect():
            return
        if not self.transport.wait_connected(15):
            self.log("relay not reachable yet; continuing, the transport reconnects on its own")
        self.log(f"computer {self.computer_name} ({self.transport.computer_id[:8]}...)")
        self.heartbeat()
        self.refresh_cache()
        self.restart_all_daemons()

        last_hb = time.time()
        last_wd = time.time()
        while self.running:
            try:
                frame = self.transport.requests.get(timeout=1)
                reply = self.handle_request(frame.get("kind") or "", frame.get("payload") or {})
                self.transport.respond(frame, reply)
            except queue.Empty:
                pass
            now = time.time()
            if now - last_hb >= HEARTBEAT_EVERY:
                self.settings.enabled_flag.write_text(str(int(now)))
                self.heartbeat()
                last_hb = now
            if now - last_wd >= WATCHDOG_EVERY:
                last_wd = now
                self.refresh_cache()
                for duty in (self.restart_orphans, self.reconcile_stopped, self.restart_stuck,
                             self.check_waiting_alerts):
                    try:
                        duty()
                    except Exception as e:  # noqa: BLE001 - one failed duty must not stop the others
                        self.log(f"{duty.__name__} failed: {e}")

        self.log("supervisor stopping")
        try:
            self.transport.update_computer(status="offline")
            self.transport.stop()
        except Exception as e:  # noqa: BLE001
            self.log(f"could not record offline: {e}")
        self.settings.enabled_flag.unlink(missing_ok=True)


def main() -> int:
    Supervisor(load_settings()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
