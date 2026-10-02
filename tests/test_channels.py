"""Каналы поиска в SQLite, без сети и без чтения .env."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vacancy_hunter.database import SeenStore


def _store(tmp_path: Path) -> SeenStore:
    return SeenStore(tmp_path / "seen.db")


def test_add_channel_strips_prefix_and_rejects_bad_names(tmp_path):
    store = _store(tmp_path)
    assert store.add_channel("@job_python") == "added"
    assert store.add_channel("https://t.me/s/job_python") == "exists"
    assert store.add_channel("bad name") == "invalid"
    assert store.add_channel("ab") == "invalid"
    assert store.active_channels() == ["job_python"]


def test_remove_deactivates_and_add_reactivates(tmp_path):
    store = _store(tmp_path)
    assert store.add_channel("forpython") == "added"
    assert store.deactivate_channel("forpython") == "deactivated"
    assert store.active_channels() == []
    assert store.deactivate_channel("forpython") == "inactive"
    assert store.deactivate_channel("missingchan") == "missing"
    assert store.add_channel("@forpython") == "reactivated"
    assert store.active_channels() == ["forpython"]


def test_recent_counts_use_seven_day_threshold(tmp_path):
    store = _store(tmp_path)
    store.add_channel("job_python")
    store.add_channel("geekjobs")
    now = datetime.now(timezone.utc)
    store.remember("telegram", "https://t.me/job_python/1", "Python", 30)
    old = now - timedelta(days=8)
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO seen_vacancies (content_hash, source, url, title, score, seen_at)
            VALUES (?, 'telegram', ?, 'old', 10, ?)
            """,
            ("old-hash", "https://t.me/job_python/2", old.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO seen_vacancies (content_hash, source, url, title, score, seen_at)
            VALUES (?, 'telegram', ?, 'other', 10, ?)
            """,
            ("habr-hash", "https://career.habr.com/vacancies/1", now.isoformat()),
        )
    since = (now - timedelta(days=7)).isoformat()
    assert store.list_active_with_recent_counts(since) == [("geekjobs", 0), ("job_python", 1)]
