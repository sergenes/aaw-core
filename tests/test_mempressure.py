"""The memory-pressure sampler parsing and the warn/clear state machine."""

from __future__ import annotations

import subprocess

from aaw_core.host import mempressure


def test_macos_sysctl_levels(monkeypatch):
    def fake_run(level):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=f"{level}\n", stderr="")
    for level, expected in [("1", mempressure.NORMAL), ("2", mempressure.WARN),
                            ("4", mempressure.CRITICAL), ("7", mempressure.UNKNOWN)]:
        monkeypatch.setattr(mempressure.subprocess, "run", fake_run(level))
        assert mempressure._sample_macos() == expected


def test_macos_sysctl_failure_is_unknown(monkeypatch):
    def boom(*a, **k):
        raise OSError("sysctl missing")
    monkeypatch.setattr(mempressure.subprocess, "run", boom)
    assert mempressure._sample_macos() == mempressure.UNKNOWN


def test_linux_psi_thresholds(tmp_path, monkeypatch):
    def psi(avg10):
        p = tmp_path / "pressure_memory"
        p.write_text(f"some avg10={avg10} avg60=0.0 avg300=0.0 total=1\n"
                     f"full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n")
        import builtins
        real_open = builtins.open
        monkeypatch.setattr(mempressure, "open", lambda *a, **k: real_open(p), raising=False)
    psi("0.50");  assert mempressure._sample_linux() == mempressure.NORMAL
    psi("15.0");  assert mempressure._sample_linux() == mempressure.WARN
    psi("45.0");  assert mempressure._sample_linux() == mempressure.CRITICAL


def test_monitor_sustains_before_warning():
    clock = [0.0]
    seq = iter(["critical", "critical", "critical"])
    m = mempressure.PressureMonitor(sampler=lambda: next(seq), now=lambda: clock[0])

    warn, crit, clear = m.tick()
    assert crit and not warn and not clear  # one sample: critical flag up, no push yet
    warn, crit, clear = m.tick()
    assert warn and crit and not clear  # SUSTAIN reached: push
    warn, crit, clear = m.tick()
    assert crit and not warn  # inside REPEAT_EVERY: no repeat


def test_monitor_repeats_only_after_the_interval():
    clock = [0.0]
    m = mempressure.PressureMonitor(sampler=lambda: mempressure.CRITICAL, now=lambda: clock[0])
    assert m.tick()[0] is False      # 1st
    assert m.tick()[0] is True       # 2nd: first warning
    clock[0] += mempressure.REPEAT_EVERY - 1
    assert m.tick()[0] is False      # still inside the window
    clock[0] += 2
    assert m.tick()[0] is True       # past REPEAT_EVERY: warn again


def test_monitor_clears_once_on_recovery():
    levels = iter(["critical", "critical", "normal", "normal"])
    m = mempressure.PressureMonitor(sampler=lambda: next(levels), now=lambda: 0.0)
    m.tick(); m.tick()
    assert m.is_warned
    _warn, crit, clear = m.tick()
    assert clear and not crit and not m.is_warned  # fires once
    _warn, _crit, clear = m.tick()
    assert not clear  # already cleared, no repeat


def test_monitor_unknown_does_not_clear_a_warning():
    levels = iter(["critical", "critical", "unknown"])
    m = mempressure.PressureMonitor(sampler=lambda: next(levels), now=lambda: 0.0)
    m.tick(); m.tick()
    _warn, crit, clear = m.tick()  # a flaky unknown sample must not clear the badge
    assert not clear and m.is_warned and not crit
