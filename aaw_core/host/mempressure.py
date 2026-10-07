"""Cheap memory-pressure sampling for the supervisor.

When the machine runs critically low on memory (seen live 2026-10-01: two Android
emulators plus Android Studio starved an M1, tmux probes timed out for hours, then
macOS jetsam-killed Terminal and the app with no crash report), the agents survive
in tmux but the user has no idea why everything went quiet. The supervisor samples
pressure on its heartbeat and, on sustained critical pressure, warns the phone once
and tells its own watchdog to stop treating probe timeouts as stuck sessions.

Sampling is a single cheap syscall/read, never a spawned helper that could itself
block under pressure:
  - macOS: `sysctl kern.memorystatus_vm_pressure_level` (1 normal, 2 warn, 4 critical)
  - Linux: `/proc/pressure/memory` PSI, the "some avg10" stall percentage
"""

from __future__ import annotations

import subprocess
import sys

NORMAL = "normal"
WARN = "warn"
CRITICAL = "critical"
UNKNOWN = "unknown"

# Linux PSI "some avg10" (percent of the last 10 s some task stalled on memory).
# A few percent is routine; sustained double digits is real pressure.
PSI_WARN = 10.0
PSI_CRITICAL = 30.0

# Consecutive critical samples before we warn, so a brief spike stays quiet.
SUSTAIN = 2

# One push at most this often while pressure persists (seconds).
REPEAT_EVERY = 3600


def _sample_macos() -> str:
    try:
        r = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if r.returncode != 0:
        return UNKNOWN
    level = (r.stdout or "").strip()
    return {"1": NORMAL, "2": WARN, "4": CRITICAL}.get(level, UNKNOWN)


def _sample_linux() -> str:
    try:
        with open("/proc/pressure/memory") as f:
            text = f.read()
    except OSError:
        return UNKNOWN
    # Line: "some avg10=12.34 avg60=... avg300=... total=..."
    for line in text.splitlines():
        if line.startswith("some "):
            for field in line.split():
                if field.startswith("avg10="):
                    try:
                        v = float(field.split("=", 1)[1])
                    except ValueError:
                        return UNKNOWN
                    if v >= PSI_CRITICAL:
                        return CRITICAL
                    if v >= PSI_WARN:
                        return WARN
                    return NORMAL
    return UNKNOWN


def sample() -> str:
    """One pressure reading: normal, warn, critical, or unknown (never raises)."""
    return _sample_macos() if sys.platform == "darwin" else _sample_linux()


class PressureMonitor:
    """Turns a stream of samples into (should_warn_now, is_critical, should_clear).

    - ``is_critical`` is true from the first critical sample, so the watchdog can
      stand down immediately (one bad sample is enough to distrust probe timeouts).
    - A push fires only after ``SUSTAIN`` consecutive critical samples, then at most
      once per ``REPEAT_EVERY`` while it persists.
    - ``should_clear`` fires once when pressure returns to normal after we had warned,
      so the phone badge and the "was warned" state reset.
    """

    def __init__(self, *, sampler=sample, now=None):
        import time
        self._sample = sampler
        self._now = now or time.monotonic
        self._critical_streak = 0
        self._warned_at: float | None = None

    def tick(self) -> tuple[bool, bool, bool]:
        level = self._sample()
        is_critical = level == CRITICAL
        if is_critical:
            self._critical_streak += 1
        else:
            self._critical_streak = 0

        should_warn = False
        if self._critical_streak >= SUSTAIN:
            now = self._now()
            if self._warned_at is None or (now - self._warned_at) >= REPEAT_EVERY:
                self._warned_at = now
                should_warn = True

        should_clear = False
        if level == NORMAL and self._warned_at is not None and self._critical_streak == 0:
            # Only clear on a definite normal reading (not unknown), so a flaky probe
            # does not keep toggling the badge.
            self._warned_at = None
            should_clear = True

        return should_warn, is_critical, should_clear

    @property
    def is_warned(self) -> bool:
        return self._warned_at is not None
