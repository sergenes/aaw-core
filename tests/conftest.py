import pytest


@pytest.fixture(autouse=True)
def no_update_check(monkeypatch):
    """No test asks PyPI for the newest scoot; the tests of that check switch it back on
    and fake the answer."""
    monkeypatch.setenv("AAW_NO_UPDATE_CHECK", "1")
