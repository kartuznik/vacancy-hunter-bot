"""Дедупликация вакансий в sqlite3 по хэшу источника и ссылки."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "seen_vacancies.db"


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

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()
