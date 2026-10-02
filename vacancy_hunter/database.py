"""Дедупликация вакансий в sqlite3 по хэшу источника и ссылки."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CHANNEL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "seen_vacancies.db"


def normalize_channel(raw: str) -> str | None:
    text = raw.strip()
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:t\.me/s/|t\.me/)", "", text, flags=re.IGNORECASE)
    text = text.lstrip("@").strip().strip("/")
    if "/" in text:
        text = text.split("/", 1)[0]
    if CHANNEL_NAME.fullmatch(text):
        return text
    return None


def content_hash(source: str, url: str) -> str:
    payload = f"{source}\n{url}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SeenStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def has(self, source: str, url: str) -> bool:
        digest = content_hash(source, url)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM seen_vacancies WHERE content_hash = ?",
                (digest,),
            ).fetchone()
        return row is not None

    def remember(self, source: str, url: str, title: str, score: int) -> bool:
        digest = content_hash(source, url)
        seen_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO seen_vacancies
                    (content_hash, source, url, title, score, seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (digest, source, url, title, score, seen_at),
            )
            return cursor.rowcount == 1

    def count_vacancies(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM seen_vacancies").fetchone()
        return int(row[0])

    def stats_by_source(self) -> list[tuple[str, int, str | None]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source, COUNT(*), MAX(seen_at)
                FROM seen_vacancies
                GROUP BY source
                ORDER BY source
                """
            ).fetchall()
        return [(str(row[0]), int(row[1]), row[2]) for row in rows]

    def add_channel(self, raw_name: str) -> str:
        name = normalize_channel(raw_name)
        if name is None:
            return "invalid"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT is_active FROM search_channels WHERE channel_name = ?",
                (name,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO search_channels (channel_name, is_active, added_at)
                    VALUES (?, 1, ?)
                    """,
                    (name, now),
                )
                return "added"
            if int(existing[0]) == 1:
                return "exists"
            connection.execute(
                "UPDATE search_channels SET is_active = 1 WHERE channel_name = ?",
                (name,),
            )
            return "reactivated"

    def deactivate_channel(self, raw_name: str) -> str:
        name = normalize_channel(raw_name)
        if name is None:
            return "invalid"
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE search_channels SET is_active = 0 WHERE channel_name = ? AND is_active = 1",
                (name,),
            )
            if cursor.rowcount == 1:
                return "deactivated"
            found = connection.execute(
                "SELECT 1 FROM search_channels WHERE channel_name = ?",
                (name,),
            ).fetchone()
        return "inactive" if found else "missing"

    def active_channels(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT channel_name
                FROM search_channels
                WHERE is_active = 1
                ORDER BY channel_name
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def channels_for_search(self) -> list[str]:
        names = self.active_channels()
        if names:
            return names
        from config import get_settings

        return [name for name in (normalize_channel(item) for item in get_settings().tg_channels) if name]

    def list_active_with_recent_counts(self, since_iso: str) -> list[tuple[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.channel_name, COUNT(v.content_hash)
                FROM search_channels AS c
                LEFT JOIN seen_vacancies AS v
                  ON v.source = 'telegram'
                 AND v.url LIKE 'https://t.me/' || c.channel_name || '/%'
                 AND v.seen_at >= ?
                WHERE c.is_active = 1
                GROUP BY c.channel_name
                ORDER BY c.channel_name
                """,
                (since_iso,),
            ).fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_vacancies (
                    content_hash TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_channels (
                    id INTEGER PRIMARY KEY,
                    channel_name TEXT UNIQUE,
                    is_active INTEGER DEFAULT 1,
                    added_at TEXT
                )
                """
            )
        if self.path.resolve() == default_db_path().resolve():
            self._seed_channels_if_empty()

    def _seed_channels_if_empty(self) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM search_channels").fetchone()
            if int(row[0]) > 0:
                return
        from config import get_settings

        now = datetime.now(timezone.utc).isoformat()
        names = [name for name in (normalize_channel(item) for item in get_settings().tg_channels) if name]
        if not names:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO search_channels (channel_name, is_active, added_at)
                VALUES (?, 1, ?)
                """,
                [(name, now) for name in names],
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()
