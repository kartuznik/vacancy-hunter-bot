"""Архив, избранное, метки, экспорт и умный /download на временной SQLite, без сети и .env."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from bot import digest_keyboard, digest_tap_text, export_days, favorite_reply, history_view, label_reply, saved_messages
from vacancy_hunter import core
from vacancy_hunter.database import HASH_PREFIX_CHARS, SeenStore, content_hash
from vacancy_hunter.report import build_stats, card_keyboard, render_markdown, telegram_card_messages

CARD = {
    "source": "telegram",
    "source_type": "telegram",
    "channel_name": "job_python",
    "id": "1",
    "title": "Python-разработчик (backend)",
    "url": "https://t.me/job_python/101",
    "salary": "от 150 000 ₽",
    "description": "Удалённо, fastapi, postgresql",
    "published_at": datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc),
    "score": 120,
    "matched_whitelist_words": ["fastapi", "postgresql"],
}
HABR_CARD = {
    **CARD,
    "source": "habr",
    "source_type": "habr",
    "channel_name": "",
    "title": "Junior Python Developer",
    "url": "https://career.habr.com/vacancies/1",
    "score": 75,
}


def _store(tmp_path: Path) -> SeenStore:
    return SeenStore(tmp_path / "seen.db")


def _prefix(item: dict) -> str:
    return content_hash(item["source"], item["url"])[:HASH_PREFIX_CHARS]


def _digest(directory: Path, name: str, *, empty: bool, mtime: float, new: int = 1) -> Path:
    stats = build_stats({"telegram": [CARD] * 3, "habr": [CARD] * 2}, 4, [CARD], [CARD] * new)
    items = [] if empty else [CARD]
    path = directory / name
    path.write_text(render_markdown(items, stats, day="2026-10-16"), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def digests(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "digests"
    directory.mkdir()
    monkeypatch.setattr(core, "digests_dir", lambda: directory)
    return directory


def test_migration_keeps_old_rows_and_adds_archive_schema(tmp_path):
    path = tmp_path / "seen.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE seen_vacancies (content_hash TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL, "
        "title TEXT NOT NULL, score INTEGER NOT NULL, seen_at TEXT NOT NULL, title_key TEXT, "
        "is_duplicate INTEGER DEFAULT 0, original_source TEXT)"
    )
    connection.execute(
        "INSERT INTO seen_vacancies VALUES (?, 'habr', 'https://x/1', 'Old', 40, '2026-10-02T12:00:00+00:00', "
        "NULL, 0, NULL)",
        (content_hash("habr", "https://x/1"),),
    )
    connection.commit()
    connection.close()

    store = SeenStore(path)
    SeenStore(path)
    with sqlite3.connect(path) as check:
        tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        columns = {row[1] for row in check.execute("PRAGMA table_info(seen_vacancies)")}
        old = check.execute("SELECT title, score, run_id FROM seen_vacancies").fetchall()
    assert {"vacancy_cards", "runs", "saved_vacancies", "vacancy_labels"} <= tables
    assert "run_id" in columns
    assert old == [("Old", 40, None)]
    assert store.has("habr", "https://x/1")


def test_run_and_card_snapshot_keep_first_appearance(tmp_path):
    store = _store(tmp_path)
    first = store.start_run()
    assert store.remember_card(CARD, first) is True
    second = store.start_run()
    assert store.remember_card({**CARD, "score": 999, "description": "другое"}, second) is False
    store.finish_run(
        first,
        collected_by_source={"telegram": 3, "habr": 2},
        passed=1,
        filtered=4,
        new_count=1,
        duplicates_count=0,
        digest_file="digest_2026-10-16_1200.md",
    )
    run = store.run_by_digest("digest_2026-10-16_1200.md")
    assert run["collected_total"] == 5
    assert json.loads(run["collected_by_source"]) == {"telegram": 3, "habr": 2}
    assert run["finished_at"] is not None
    cards = store.top_cards(first)
    assert len(cards) == 1
    assert cards[0]["score"] == 120
    assert cards[0]["salary"] == "от 150 000 ₽"
    assert json.loads(cards[0]["matched_words"]) == ["fastapi", "postgresql"]
    assert cards[0]["published_at"] == "2026-10-16T09:00:00+00:00"
    assert store.top_cards(second) == []


def test_run_search_records_run_cards_and_seen_run_id(tmp_path, digests, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(core, "SeenStore", lambda: store)
    monkeypatch.setattr(core, "get_settings", lambda: type("S", (), {"telegram_bot_token": "", "chat_id": ""})())
    monkeypatch.setattr(core, "gather", lambda: ({"telegram": [CARD], "habr": [HABR_CARD]}, [CARD, HABR_CARD], 3))
    result = core.run_search(notify=False)

    run = store.recent_runs(1)[0]
    assert run["digest_file"] == result.digest_path.name
    assert (run["collected_total"], run["passed"], run["filtered"], run["new_count"]) == (2, 2, 3, 2)
    assert [card["title"] for card in store.top_cards(run["run_id"])] == [CARD["title"], HABR_CARD["title"]]
    with sqlite3.connect(store.path) as check:
        run_ids = {row[0] for row in check.execute("SELECT run_id FROM seen_vacancies")}
    assert run_ids == {run["run_id"]}
    card_messages = [markup for _text, markup in result.messages if markup is not None]
    assert len(card_messages) == 2


def test_card_messages_put_section_header_into_first_card():
    messages = telegram_card_messages([CARD, {**CARD, "url": "https://t.me/job_python/102", "score": 60}])
    assert len(messages) == 2
    assert messages[0][0].startswith("<b>🟣 Telegram</b>\n<b>job_python</b>")
    assert "Telegram" not in messages[1][0].split("\n")[0]
    assert all(markup["inline_keyboard"][0][0]["text"] == "⭐" for _text, markup in messages)


def test_repeated_star_reports_already_saved(tmp_path):
    store = _store(tmp_path)
    store.remember_card(CARD, None)
    assert favorite_reply(_prefix(CARD), store) == "Добавлено в избранное"
    assert favorite_reply(_prefix(CARD), store) == "Уже в избранном"
    assert favorite_reply("0" * HASH_PREFIX_CHARS, store) == "Карточка не найдена в базе."
    assert favorite_reply("../etc", store) == "Карточка не найдена в базе."
    saved = saved_messages(store)
    assert "Python-разработчик (backend)" in saved[0]
    assert CARD["url"] in saved[0]
    assert "добавлено" in saved[0]


def test_label_is_overwritten_by_second_tap(tmp_path):
    store = _store(tmp_path)
    store.remember_card(CARD, None)
    assert label_reply(f"r:{_prefix(CARD)}", store) == "Отмечено: релевантно"
    assert label_reply(f"j:{_prefix(CARD)}", store) == "Отмечено: мусор"
    assert label_reply(f"x:{_prefix(CARD)}", store) == "Неизвестная метка."
    with sqlite3.connect(store.path) as check:
        rows = check.execute("SELECT label FROM vacancy_labels").fetchall()
    assert rows == [("junk",)]


def test_csv_and_jsonl_export_include_snapshot_and_legacy_rows(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.remember_card(CARD, None)
    store.remember("habr", "https://x/legacy", "Legacy", 33)
    favorite_reply(_prefix(CARD), store)
    label_reply(f"r:{_prefix(CARD)}", store)

    raw = core.export_csv(store, 30)
    assert raw.startswith("\ufeff".encode("utf-8"))
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert list(rows[0]) == ["title", "url", "score", "saved", "label", "seen_at", "source"]
    by_title = {row["title"]: row for row in rows}
    assert by_title[CARD["title"]]["saved"] == "1"
    assert by_title[CARD["title"]]["label"] == "relevant"
    assert by_title["Legacy"]["saved"] == "0"
    assert by_title["Legacy"]["label"] == ""

    monkeypatch.setattr(core, "exports_dir", lambda: tmp_path / "exports")
    path = core.write_export_jsonl(30, store)
    assert path.parent == tmp_path / "exports"
    assert path.name.startswith("vacancies_") and path.suffix == ".jsonl"
    records = {record["title"]: record for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())}
    full = records[CARD["title"]]
    assert full["description"] == CARD["description"]
    assert full["matched_words"] == ["fastapi", "postgresql"]
    assert full["channel_name"] == "job_python"
    assert full["saved"] is True and full["label"] == "relevant" and full["has_card"] is True
    legacy = records["Legacy"]
    assert legacy["has_card"] is False and legacy["description"] is None and legacy["matched_words"] == []
    assert core.export_csv(store, 30) == raw
    assert export_days("") == 30 and export_days("7") == 7
    assert export_days("0") is None and export_days("abc") is None


def test_download_picks_last_non_empty_digest_with_note(digests):
    found = _digest(digests, "digest_2026-10-16_1200.md", empty=False, mtime=1000)
    newest = _digest(digests, "digest_2026-10-16_1600.md", empty=True, mtime=2000, new=0)
    assert core.latest_digest() == found
    assert core.newest_digest() == newest
    note = core.download_note(found, newest)
    assert note is not None
    assert note.startswith("Последний прогон в ")
    assert "16:00" in note
    assert note.endswith("был пустой, вот находки от 16.10 12:00")
    assert core.download_note(found, found) is None


def test_download_has_nothing_when_all_digests_empty(digests):
    _digest(digests, "digest_2026-10-16_1600.md", empty=True, mtime=2000, new=0)
    assert core.latest_digest() is None


def test_callback_digest_name_rejects_foreign_paths(digests, tmp_path):
    real = _digest(digests, "digest_2026-10-16_1200.md", empty=False, mtime=1000)
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    assert core.digest_by_name("digest_2026-10-16_1200.md") == real.resolve()
    for name in ("../secret.md", "digest_../../secret.md", "secret.md", "/etc/passwd", "digest_2026-10-17_0000.md", ""):
        assert core.digest_by_name(name) is None


def test_callback_data_fits_telegram_limit():
    long_url = "https://t.me/" + "a" * 500
    for button in card_keyboard({**CARD, "url": long_url})["inline_keyboard"][0]:
        assert len(button["callback_data"].encode("utf-8")) <= 64
    keyboard = digest_keyboard("digest_2026-10-16_1200_12.md")
    assert len(keyboard.inline_keyboard[0][0].callback_data.encode("utf-8")) <= 64


def test_backfill_from_digests_is_idempotent_and_feeds_history(tmp_path, digests):
    _digest(digests, "digest_2026-10-07.md", empty=True, mtime=500, new=0)
    _digest(digests, "digest_2026-10-16_1200.md", empty=False, mtime=1000)
    store = _store(tmp_path)
    assert core.backfill_runs_from_digests(store) == 2
    assert core.backfill_runs_from_digests(store) == 0
    runs = store.recent_runs(10)
    assert [run["digest_file"] for run in runs] == ["digest_2026-10-16_1200.md", "digest_2026-10-07.md"]
    assert runs[0]["collected_total"] == 5 and runs[0]["passed"] == 1 and runs[0]["new_count"] == 1
    assert runs[1]["started_at"].startswith("2026-10-07T00:00:00")
    text, keyboard = history_view(store)
    assert "16.10 12:00 — собрано 5, прошло 1, новых 1" in text
    assert "07.10 00:00 — собрано 5, прошло 1, новых 0" in text
    assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
        "dl:digest_2026-10-16_1200.md",
        "dl:digest_2026-10-07.md",
    ]


def test_backfill_skipped_when_runs_exist(tmp_path, digests):
    _digest(digests, "digest_2026-10-16_1200.md", empty=False, mtime=1000)
    store = _store(tmp_path)
    store.start_run()
    assert core.backfill_runs_from_digests(store) == 0
    assert len(store.recent_runs(10)) == 1


def test_history_tap_shows_counters_for_empty_and_top3_for_full(tmp_path, digests):
    empty = _digest(digests, "digest_2026-10-16_1600.md", empty=True, mtime=2000, new=0)
    full = _digest(digests, "digest_2026-10-16_1200.md", empty=False, mtime=1000)
    store = _store(tmp_path)
    run_id = store.start_run()
    store.remember_card(CARD, run_id)
    store.finish_run(
        run_id,
        collected_by_source={"telegram": 5},
        passed=1,
        filtered=4,
        new_count=1,
        duplicates_count=0,
        digest_file=full.name,
    )
    empty_run = store.start_run()
    store.finish_run(
        empty_run,
        collected_by_source={"telegram": 5},
        passed=1,
        filtered=4,
        new_count=0,
        duplicates_count=0,
        digest_file=empty.name,
    )
    text, send_file = digest_tap_text(empty, store)
    assert text == "Прогон 16.10 16:00 пустой: собрано 5, прошло 1, новых 0."
    assert send_file is False
    text, send_file = digest_tap_text(full, store)
    assert text.startswith("<b>Топ-3 прогона 16.10 12:00</b>")
    assert CARD["url"] in text
    assert send_file is True
