"""Per-agent usage summaries for the /usage command, read from each agent's own local
files, written to the feed as a message and to the computer doc as card proxies."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def _publish(transport, lines: list[str], session_pct: int, weekly_pct: int, extra: str) -> None:
    transport.update_computer(session_pct=session_pct, weekly_pct=weekly_pct, extra_pct=0,
                              extra_spent=extra, usage_updated_at=int(time.time()))
    transport.write_event("message", {"role": "assistant", "content": "\n".join(lines)})


def fetch_usage_claude(transport, project_dir: Path) -> bool:
    """Claude Code: live per-project message counts from ~/.claude/history.jsonl plus
    lifetime token totals from ~/.claude/stats-cache.json (may be days old)."""
    home = Path.home()
    history_path, stats_path = home / ".claude" / "history.jsonl", home / ".claude" / "stats-cache.json"
    project_str = str(project_dir)
    today_str = datetime.now(UTC).astimezone().date().isoformat()  # local date, tz-aware
    week_start_ts = (datetime.now(UTC) - timedelta(days=7)).timestamp() * 1000
    today_msgs = week_msgs = 0
    if history_path.exists():
        try:
            for raw in history_path.read_text(errors="replace").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if entry.get("project") != project_str:
                    continue
                ts = entry.get("timestamp", 0)
                if ts >= week_start_ts:
                    week_msgs += 1
                    if datetime.fromtimestamp(ts / 1000, tz=UTC).astimezone().date().isoformat() == today_str:
                        today_msgs += 1
        except OSError as e:
            print(f"[daemon] history.jsonl read error: {e}", file=sys.stderr, flush=True)

    model_usage: dict = {}
    total_messages = total_sessions = 0
    first_session_date = stats_as_of = ""
    if stats_path.exists():
        try:
            cache = json.loads(stats_path.read_text())
            model_usage = cache.get("modelUsage", {})
            total_messages = cache.get("totalMessages", 0)
            total_sessions = cache.get("totalSessions", 0)
            first_session_date = cache.get("firstSessionDate", "")
            stats_as_of = cache.get("lastComputedDate", "")
        except (OSError, ValueError) as e:
            print(f"[daemon] stats-cache.json read error: {e}", file=sys.stderr, flush=True)
    lifetime_in = sum(v.get("inputTokens", 0) for v in model_usage.values())
    lifetime_out = sum(v.get("outputTokens", 0) for v in model_usage.values())
    lifetime_cached = sum(v.get("cacheReadInputTokens", 0) for v in model_usage.values())
    primary = max(model_usage, key=lambda m: model_usage[m].get("outputTokens", 0)) if model_usage else ""

    lines = ["📊 Claude Usage"]
    if today_msgs or week_msgs:
        lines.append(f"Project ({project_dir.name}):")
        if today_msgs:
            lines.append(f"  Today: {today_msgs} messages")
        if week_msgs > today_msgs:
            lines.append(f"  7 days: {week_msgs} messages")
    if lifetime_in or lifetime_out:
        lines.append("Lifetime tokens:")
        lines.append(f"  Input {_fmt(lifetime_in)}  ·  Output {_fmt(lifetime_out)}  ·  Cached {_fmt(lifetime_cached)}")
    if total_messages:
        lines.append(f"  {total_messages:,} messages · {total_sessions:,} sessions"
                     + (f" · since {first_session_date}" if first_session_date else ""))
    if primary:
        lines.append(f"  Primary model: {primary}")
    if stats_as_of and stats_as_of < today_str:
        lines.append(f"  (token data as of {stats_as_of})")
    _publish(transport, lines, min(today_msgs * 100 // 50, 100), min(week_msgs * 100 // 300, 100),
             f"Today: {today_msgs} msgs · 7d: {week_msgs} msgs")
    return True


def fetch_usage_codex(transport, project_dir: Path) -> bool:
    """Codex: thread and token totals from ~/.codex/state_5.sqlite, per project and global."""
    db_path = Path.home() / ".codex" / "state_5.sqlite"
    if not db_path.exists():
        print("[daemon] codex state_5.sqlite not found", file=sys.stderr, flush=True)
        return False
    project_str = str(project_dir)
    now_ts = datetime.now(UTC).timestamp()
    day_ago, week_ago = now_ts - 86_400, now_ts - 7 * 86_400
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        def query(where: str, params: tuple) -> tuple[int, int]:
            row = conn.execute(f"SELECT COUNT(*) AS threads, COALESCE(SUM(tokens_used),0) AS tokens"
                               f" FROM threads WHERE {where}", params).fetchone()
            return int(row["threads"]), int(row["tokens"])

        p_today = query("cwd = ? AND created_at >= ?", (project_str, day_ago))
        p_week = query("cwd = ? AND created_at >= ?", (project_str, week_ago))
        p_all = query("cwd = ?", (project_str,))
        g_today = query("created_at >= ?", (day_ago,))
        g_week = query("created_at >= ?", (week_ago,))
        g_all = query("1=1", ())
        row = conn.execute("SELECT model, SUM(tokens_used) AS t FROM threads WHERE model IS NOT NULL"
                           " GROUP BY model ORDER BY t DESC LIMIT 1").fetchone()
        primary = row["model"] if row else ""
        conn.close()
    except sqlite3.Error as e:
        print(f"[daemon] codex sqlite read error: {e}", file=sys.stderr, flush=True)
        return False

    lines = ["📊 Codex Usage"]
    if p_today[1] or p_today[0]:
        lines.append(f"Project ({project_dir.name}):")
        if p_today[0]:
            lines.append(f"  Today: {p_today[0]} threads · {_fmt(p_today[1])} tokens")
        if p_week[0] > p_today[0]:
            lines.append(f"  7 days: {p_week[0]} threads · {_fmt(p_week[1])} tokens")
        if p_all[0] > p_week[0]:
            lines.append(f"  All: {p_all[0]} threads · {_fmt(p_all[1])} tokens")
    lines.append("All projects:")
    if g_today[0]:
        lines.append(f"  Today: {g_today[0]} threads · {_fmt(g_today[1])} tokens")
    if g_week[0] > g_today[0]:
        lines.append(f"  7 days: {g_week[0]} threads · {_fmt(g_week[1])} tokens")
    lines.append(f"  Total: {g_all[0]} threads · {_fmt(g_all[1])} tokens")
    if primary:
        lines.append(f"  Model: {primary}")
    _publish(transport, lines, min(g_today[0] * 100 // 5, 100), min(g_week[0] * 100 // 25, 100),
             f"Today: {g_today[0]} threads · {_fmt(g_today[1])} tok")
    return True


def fetch_usage_grok(transport, project_dir: Path) -> bool:
    """Grok: message counts from ~/.grok/sessions/*/*/summary.json, per project and global."""
    root = Path.home() / ".grok" / "sessions"
    if not root.exists():
        print("[daemon] ~/.grok/sessions not found", file=sys.stderr, flush=True)
        return False
    project_str = str(project_dir)
    now_utc = datetime.now(UTC)
    today_str, week_ago = now_utc.date().isoformat(), now_utc - timedelta(days=7)
    p = {"today": 0, "week": 0, "all": 0, "sessions": 0}
    g = {"today": 0, "week": 0, "all": 0, "sessions": 0}
    models: dict[str, int] = {}
    for summary in root.glob("*/*/summary.json"):
        try:
            d = json.loads(summary.read_text())
            created = datetime.fromisoformat(d.get("created_at", ""))
        except (OSError, ValueError):
            continue
        msgs = d.get("num_chat_messages", 0) or 0
        model = d.get("current_model_id", "")
        buckets = [g] + ([p] if d.get("info", {}).get("cwd", "") == project_str else [])
        for b in buckets:
            b["sessions"] += 1
            b["all"] += msgs
            if created >= week_ago:
                b["week"] += msgs
            if created.date().isoformat() == today_str:
                b["today"] += msgs
        if model:
            models[model] = models.get(model, 0) + msgs
    primary = max(models, key=models.get) if models else ""

    lines = ["📊 Grok Usage"]
    if p["sessions"]:
        lines.append(f"Project ({project_dir.name}):")
        if p["today"]:
            lines.append(f"  Today: {p['today']} messages")
        if p["week"] > p["today"]:
            lines.append(f"  7 days: {p['week']} messages")
        lines.append(f"  All: {p['all']} messages · {p['sessions']} sessions")
    lines.append("All projects:")
    if g["today"]:
        lines.append(f"  Today: {g['today']} messages")
    if g["week"] > g["today"]:
        lines.append(f"  7 days: {g['week']} messages")
    lines.append(f"  Total: {g['all']} messages · {g['sessions']} sessions")
    if primary:
        lines.append(f"  Model: {primary}")
    _publish(transport, lines, min(g["today"] * 100 // 50, 100), min(g["week"] * 100 // 300, 100),
             f"Today: {g['today']} msgs · 7d: {g['week']} msgs")
    return True


def fetch_usage_gemini(transport, project_dir: Path) -> bool:
    """Gemini CLI: user messages per session from ~/.gemini/tmp/<project>/chats/,
    the project name resolved through ~/.gemini/projects.json."""
    home = Path.home()
    tmp_root, proj_file = home / ".gemini" / "tmp", home / ".gemini" / "projects.json"
    if not tmp_root.exists():
        print("[daemon] ~/.gemini/tmp not found", file=sys.stderr, flush=True)
        return False
    proj_name = project_dir.name
    if proj_file.exists():
        try:
            proj_name = json.loads(proj_file.read_text()).get("projects", {}).get(str(project_dir), proj_name)
        except (OSError, ValueError):
            pass
    now_utc = datetime.now(UTC)
    today_str, week_ago = now_utc.date().isoformat(), now_utc - timedelta(days=7)

    def parse_session(path: Path) -> tuple[int, datetime | None]:
        try:
            if path.suffix == ".json":
                d = json.loads(path.read_text())
                msgs, st = d.get("userMessageCount", 0) or 0, d.get("startTime") or d.get("lastUpdated")
            else:
                rows = [ln for ln in path.read_text().splitlines() if ln.strip()]
                if not rows:
                    return 0, None
                header = json.loads(rows[0])
                st = header.get("startTime") or header.get("lastUpdated")
                msgs = sum(1 for ln in rows[1:] if '"type": "user"' in ln or "'type': 'user'" in ln)
            if st:
                return msgs, datetime.fromisoformat(st)
        except (OSError, ValueError):
            pass
        return 0, None

    def tally(chat_dir: Path) -> tuple[int, int, int, int]:
        td = wk = al = ss = 0
        if chat_dir.exists():
            for path in chat_dir.glob("session-*"):
                msgs, dt = parse_session(path)
                if msgs == 0 or dt is None:
                    continue
                ss += 1
                al += msgs
                if dt >= week_ago:
                    wk += msgs
                if dt.date().isoformat() == today_str:
                    td += msgs
        return td, wk, al, ss

    p_today, p_week, p_all, p_sessions = tally(tmp_root / proj_name / "chats")
    g_today = g_week = g_all = g_sessions = 0
    for chat_dir in tmp_root.glob("*/chats"):
        td, wk, al, ss = tally(chat_dir)
        g_today, g_week, g_all, g_sessions = g_today + td, g_week + wk, g_all + al, g_sessions + ss

    lines = ["📊 Gemini Usage"]
    if p_sessions:
        lines.append(f"Project ({project_dir.name}):")
        if p_today:
            lines.append(f"  Today: {p_today} messages")
        if p_week > p_today:
            lines.append(f"  7 days: {p_week} messages")
        lines.append(f"  All: {p_all} messages · {p_sessions} sessions")
    lines.append("All projects:")
    if g_today:
        lines.append(f"  Today: {g_today} messages")
    if g_week > g_today:
        lines.append(f"  7 days: {g_week} messages")
    lines.append(f"  Total: {g_all} messages · {g_sessions} sessions")
    _publish(transport, lines, min(g_today * 100 // 50, 100), min(g_week * 100 // 300, 100),
             f"Today: {g_today} msgs · 7d: {g_week} msgs")
    return True


FETCHERS = {"codex": fetch_usage_codex, "grok": fetch_usage_grok, "gemini": fetch_usage_gemini}


def fetch_usage(transport, project_dir: Path, agent: str) -> bool:
    return FETCHERS.get(agent, fetch_usage_claude)(transport, project_dir)
