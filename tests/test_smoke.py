"""Scaffold smoke tests: the package imports, config resolves, the CLI runs."""

from __future__ import annotations

import pytest

import aaw_core
from aaw_core.config import load_settings
from aaw_core.host.cli import main


def test_version_is_set():
    assert aaw_core.__version__


def test_settings_have_no_hosted_defaults(monkeypatch, tmp_path):
    # Config-first: with nothing set, the relay URL is None (never a hosted project).
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AAW_RELAY_URL", raising=False)
    settings = load_settings()
    assert settings.relay_url is None
    assert settings.state_dir == tmp_path
    assert settings.sessions_dir == tmp_path / "sessions"


def test_env_overrides_config_file(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text('{"relay_url": "wss://from-file.example"}')
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AAW_RELAY_URL", "wss://from-env.example")
    assert load_settings().relay_url == "wss://from-env.example"


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as e:  # argparse's version action exits
        main(["--version"])
    assert e.value.code == 0
    assert aaw_core.__version__ in capsys.readouterr().out
