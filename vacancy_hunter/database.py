"""Дедупликация вакансий в sqlite3 по хэшу источника и ссылки."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CHANNEL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
TITLE_KEY_DESCRIPTION_CHARS = 200
_NON_WORD = re.compile(r"[\W_]+")
_DEDUP_COLUMNS = (
    ("title_key", "TEXT"),
    ("is_duplicate", "INTEGER DEFAULT 0"),
    ("original_source", "TEXT"),
    ("run_id", "INTEGER"),
)
HASH_PREFIX_CHARS = 32
LABELS = ("relevant", "junk")
_ARCHIVE_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS vacancy_cards (
        content_hash TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        description TEXT,
        salary TEXT,
        source TEXT NOT NULL,
        channel_name TEXT,
        published_at TEXT,
        matched_words TEXT,
        score INTEGER NOT NULL,
        seen_at TEXT NOT NULL,
        run_id INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        collected_total INTEGER,
        collected_by_source TEXT,
        passed INTEGER,
        filtered INTEGER,
        new_count INTEGER,
        duplicates_count INTEGER,
        digest_file TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS saved_vacancies (
        content_hash TEXT PRIMARY KEY,
        saved_at TEXT NOT NULL,
        note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vacancy_labels (
        content_hash TEXT PRIMARY KEY,
        label TEXT NOT NULL CHECK (label IN ('relevant', 'junk')),
        labeled_at TEXT NOT NULL
    )
    """,
)


def normalize_title(text: str) -> str:
    lowered = (text or "").lower().replace("ё", "е")
    return " ".join(_NON_WORD.sub(" ", lowered).split())


def title_key(title: str, description: str = "") -> str:
    head = normalize_title(description)[:TITLE_KEY_DESCRIPTION_CHARS]
    payload = f"{normalize_title(title)}\n{head}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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

    def remember(
        self,
        source: str,
        url: str,
        title: str,
        score: int,
        *,
        title_key: str | None = None,
        is_duplicate: bool = False,
        original_source: str | None = None,
        run_id: int | None = None,
    ) -> bool:
        digest = content_hash(source, url)
        seen_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO seen_vacancies
                    (content_hash, source, url, title, score, seen_at,
                     title_key, is_duplicate, original_source, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (digest, source, url, title, score, seen_at, title_key, int(is_duplicate), original_source, run_id),
            )
            return cursor.rowcount == 1

    def start_run(self) -> int:
        started_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute("INSERT INTO runs (started_at) VALUES (?)", (started_at,))
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        collected_by_source: dict[str, int],
        passed: int,
        filtered: int,
        new_count: int,
        duplicates_count: int,
        digest_file: str,
    ) -> None:
        finished_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET finished_at = ?, collected_total = ?, collected_by_source = ?, passed = ?,
                    filtered = ?, new_count = ?, duplicates_count = ?, digest_file = ?
                WHERE run_id = ?
                """,
                (
                    finished_at,
                    sum(collected_by_source.values()),
                    json.dumps(collected_by_source, ensure_ascii=False),
                    passed,
                    filtered,
                    new_count,
                    duplicates_count,
                    digest_file,
                    run_id,
                ),
            )

    def backfill_runs(self, rows: list[dict]) -> int:
        with self._connect() as connection:
            if int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]) > 0:
                return 0
            connection.executemany(
                """
                INSERT INTO runs
                    (started_at, finished_at, collected_total, collected_by_source, passed,
                     filtered, new_count, duplicates_count, digest_file)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["started_at"],
                        sum(row["collected_by_source"].values()),
                        json.dumps(row["collected_by_source"], ensure_ascii=False),
                        row["passed"],
                        row["filtered"],
                        row["new_count"],
                        row["duplicates_count"],
                        row["digest_file"],
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def run_by_digest(self, digest_file: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE digest_file = ? ORDER BY run_id DESC LIMIT 1",
                (digest_file,),
            ).fetchone()
        return dict(row) if row is not None else None

    def remember_card(self, item: dict, run_id: int | None) -> bool:
        published = item.get("published_at")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO vacancy_cards
                    (content_hash, title, url, description, salary, source, channel_name,
                     published_at, matched_words, score, seen_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_hash(item["source"], item["url"]),
                    item.get("title") or "",
                    item["url"],
                    item.get("description") or "",
                    item.get("salary") or "",
                    item["source"],
                    item.get("channel_name") or "",
                    published.isoformat() if isinstance(published, datetime) else published,
                    json.dumps(list(item.get("matched_whitelist_words") or []), ensure_ascii=False),
                    int(item.get("score") or 0),
                    datetime.now(timezone.utc).isoformat(),
                    run_id,
                ),
            )
            return cursor.rowcount == 1

    def top_cards(self, run_id: int, limit: int = 3) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM vacancy_cards WHERE run_id = ? ORDER BY score DESC, title LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_hash(self, prefix: str) -> str | None:
        if len(prefix) != HASH_PREFIX_CHARS or not re.fullmatch(r"[0-9a-f]+", prefix):
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT content_hash FROM vacancy_cards WHERE substr(content_hash, 1, ?) = ?
                UNION
                SELECT content_hash FROM seen_vacancies WHERE substr(content_hash, 1, ?) = ?
                LIMIT 1
                """,
                (HASH_PREFIX_CHARS, prefix, HASH_PREFIX_CHARS, prefix),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def save_vacancy(self, digest: str, note: str | None = None) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO saved_vacancies (content_hash, saved_at, note) VALUES (?, ?, ?)",
                (digest, datetime.now(timezone.utc).isoformat(), note),
            )
            return cursor.rowcount == 1

    def set_label(self, digest: str, label: str) -> None:
        if label not in LABELS:
            raise ValueError(f"unknown label {label}")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO vacancy_labels (content_hash, label, labeled_at) VALUES (?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET label = excluded.label, labeled_at = excluded.labeled_at
                """,
                (digest, label, datetime.now(timezone.utc).isoformat()),
            )

    def saved_list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.content_hash, s.saved_at, s.note,
                       COALESCE(c.title, v.title) AS title,
                       COALESCE(c.url, v.url) AS url,
                       COALESCE(c.score, v.score) AS score
                FROM saved_vacancies AS s
                LEFT JOIN vacancy_cards AS c ON c.content_hash = s.content_hash
                LEFT JOIN seen_vacancies AS v ON v.content_hash = s.content_hash
                ORDER BY s.saved_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def export_rows(self, since_iso: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.content_hash, c.title, c.url, c.description, c.salary, c.source, c.channel_name,
                       c.published_at, c.matched_words, c.score, c.seen_at, c.run_id, 1 AS has_card,
                       s.saved_at, s.note, l.label, l.labeled_at
                FROM vacancy_cards AS c
                LEFT JOIN saved_vacancies AS s ON s.content_hash = c.content_hash
                LEFT JOIN vacancy_labels AS l ON l.content_hash = c.content_hash
                WHERE c.seen_at >= ?
                UNION ALL
                SELECT v.content_hash, v.title, v.url, NULL, NULL, v.source, NULL,
                       NULL, NULL, v.score, v.seen_at, v.run_id, 0 AS has_card,
                       s.saved_at, s.note, l.label, l.labeled_at
                FROM seen_vacancies AS v
                LEFT JOIN saved_vacancies AS s ON s.content_hash = v.content_hash
                LEFT JOIN vacancy_labels AS l ON l.content_hash = v.content_hash
                WHERE v.seen_at >= ?
                  AND v.content_hash NOT IN (SELECT content_hash FROM vacancy_cards)
                ORDER BY seen_at DESC
                """,
                (since_iso, since_iso),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_original(self, key: str) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(original_source, source), seen_at
                FROM seen_vacancies
                WHERE title_key = ?
                ORDER BY seen_at
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    def count_duplicates(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM seen_vacancies WHERE is_duplicate = 1").fetchone()
        return int(row[0])

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
            existing = {row[1] for row in connection.execute("PRAGMA table_info(seen_vacancies)")}
            for column, kind in _DEDUP_COLUMNS:
                if column not in existing:
                    connection.execute(f"ALTER TABLE seen_vacancies ADD COLUMN {column} {kind}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_vacancies_title_key ON seen_vacancies (title_key)"
            )
            for statement in _ARCHIVE_TABLES:
                connection.execute(statement)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_vacancy_cards_run_id ON vacancy_cards (run_id)")
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
