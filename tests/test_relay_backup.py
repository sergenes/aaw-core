import sqlite3

from aaw_core.relay.backup import backup


def test_backup_is_a_consistent_copy_and_prunes_to_keep(tmp_path):
    db = tmp_path / "relay.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE devices(token TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO devices VALUES('t1')")
    conn.commit()  # stays open: the relay keeps the store open while the backup runs

    out = tmp_path / "backups"
    first = backup(str(db), str(out), keep=2, now=1_700_000_000)
    conn.execute("INSERT INTO devices VALUES('t2')")
    conn.commit()
    second = backup(str(db), str(out), keep=2, now=1_700_000_060)
    third = backup(str(db), str(out), keep=2, now=1_700_000_120)

    assert not first.exists() and second.exists() and third.exists()
    assert sorted(p.name for p in out.iterdir()) == [second.name, third.name]
    copy = sqlite3.connect(third)
    assert [r[0] for r in copy.execute("SELECT token FROM devices ORDER BY token")] == ["t1", "t2"]
    assert oct(third.stat().st_mode & 0o777) == "0o600"
