"""Обратная связь: причины, пороги предложений, подмены настроек. Без сети."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import (
    card_actions_keyboard,
    history_limit,
    label_reply,
    reason_keyboard,
    reason_reply,
)
from vacancy_hunter.core import GateSettings, evaluate
from vacancy_hunter.database import SeenStore, content_hash
from vacancy_hunter.feedback import (
    accept_suggestion,
    archive_delta,
    decline_suggestion,
    effective_gates,
    feedback_history,
    feedback_report,
    pattern_allowed,
    pending_views,
    refresh_suggestions,
    overrides_report,
    reset_journal,
    reset_override,
    undo_event,
)
from vacancy_hunter.feedback import Proposal
from vacancy_hunter.report import HASH_PREFIX_CHARS

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
GOOD = "Python-разработчик удаленно, fastapi, django"
GRADES = (
    "lead",
    "principal",
    "head",
    "owner",
    "director",
    "руководител",
    "главн",
    "лидер",
    "тимлид",
    "директор",
)


def _store(tmp_path: Path) -> SeenStore:
    return SeenStore(tmp_path / "seen.db")


def _vote(
    store: SeenStore,
    *,
    url: str,
    company: str = "",
    channel: str = "job_python",
    reason: str | None = "c",
    verdict: str = "junk",
    title: str = "Python Developer",
    salary: int | None = None,
    source: str = "telegram",
    when: datetime = NOW,
) -> int:
    return store.add_feedback_event(
        content_hash=content_hash(source, url),
        verdict=verdict,
        reason=reason,
        source=source,
        channel_name=channel,
        company=company,
        salary_ceiling=salary,
        title=title,
        created_at=when.isoformat(),
    )


def _prefix(source: str, url: str) -> str:
    return content_hash(source, url)[:HASH_PREFIX_CHARS]


def _card(**extra) -> dict:
    item = {
        "source": "telegram",
        "source_type": "telegram",
        "channel_name": "job_python",
        "company": "",
        "url": "https://t.me/job_python/1",
        "title": "Python Developer",
        "description": GOOD,
        "salary": "от 150 000 ₽",
        "published_at": NOW,
        "score": 80,
        "matched_whitelist_words": ["fastapi"],
    }
    item.update(extra)
    return item


def test_one_dislike_does_not_change_filters(tmp_path):
    store = _store(tmp_path)
    _vote(store, url="https://t.me/job_python/1", company="Acme Studio")
    assert refresh_suggestions(store, now=NOW) == []
    assert store.blacklist_patterns() == []
    assert effective_gates(store).min_salary_rub == 80_000


def test_repeated_taps_on_one_card_count_as_one_vote(tmp_path):
    store = _store(tmp_path)
    for _ in range(2):
        _vote(store, url="https://t.me/job_python/1", company="Acme Studio")
    assert refresh_suggestions(store, now=NOW) == []


def test_two_companies_propose_full_name_and_wait_for_accept(tmp_path):
    store = _store(tmp_path)
    store.remember_card(_card(company="Acme Studio", url="https://t.me/job_python/1"), None)
    _vote(store, url="https://t.me/job_python/1", company="Acme Studio")
    _vote(store, url="https://t.me/job_python/2", company="acme studio")
    created = refresh_suggestions(store, now=NOW)
    assert [item["payload"]["pattern"] for item in created] == ["Acme Studio"]
    assert store.blacklist_patterns() == []
    suggestion_id = created[0]["id"]
    assert "Блэклист" in pending_views(store, now=NOW)[0]["text"]
    assert accept_suggestion(store, suggestion_id, now=NOW).startswith("Применено")
    assert store.blacklist_patterns() == ["Acme Studio"]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT suggestion_id FROM blacklist").fetchone()[0] == suggestion_id
    assert evaluate(_card(company="Acme Studio"), store.blacklist_patterns(), effective_gates(store))[0] == "reject"
    assert store.blacklist_remove("Acme Studio") is True
    assert refresh_suggestions(store, now=NOW)[0]["payload"]["pattern"] == "Acme Studio"


def test_archive_shows_how_many_cards_a_blacklist_would_drop(tmp_path):
    store = _store(tmp_path)
    store.remember_card(_card(company="Acme Studio", url="https://t.me/job_python/1"), None)
    store.remember_card(_card(company="Other", url="https://t.me/job_python/2"), None)
    _vote(store, url="https://t.me/a/1", company="Acme Studio")
    _vote(store, url="https://t.me/a/2", company="Acme Studio")
    created = refresh_suggestions(store, now=NOW)
    assert archive_delta(store, Proposal("blacklist", created[0]["payload"], created[0]["evidence"])) == 1


def test_channel_needs_three_votes_and_positive_blocks(tmp_path):
    store = _store(tmp_path)
    _vote(store, url="https://t.me/spam_jobs/1", channel="spam_jobs", company="")
    _vote(store, url="https://t.me/spam_jobs/2", channel="spam_jobs", company="")
    assert refresh_suggestions(store, now=NOW) == []
    _vote(store, url="https://t.me/spam_jobs/3", channel="spam_jobs", company="")
    assert refresh_suggestions(store, now=NOW)[0]["payload"]["pattern"] == "spam_jobs"
    decline_suggestion(store, store.pending_suggestions()[0]["id"], now=NOW)

    other = SeenStore(tmp_path / "positive.db")
    _vote(other, url="https://t.me/spam_jobs/1", channel="spam_jobs", company="")
    _vote(other, url="https://t.me/spam_jobs/2", channel="spam_jobs", company="")
    _vote(other, url="https://t.me/spam_jobs/3", channel="spam_jobs", company="", verdict="relevant", reason=None)
    assert refresh_suggestions(other, now=NOW) == []


def test_short_and_generic_names_are_not_proposed(tmp_path):
    assert pattern_allowed("IT") is False
    assert pattern_allowed("group") is False
    assert pattern_allowed("ООО") is False
    assert pattern_allowed("IT Group") is False
    assert pattern_allowed("Acme Studio") is True
    store = _store(tmp_path)
    _vote(store, url="https://t.me/a/1", company="group")
    _vote(store, url="https://t.me/a/2", company="group")
    assert refresh_suggestions(store, now=NOW) == []


def test_liked_salary_blocks_raise_and_step_is_capped(tmp_path):
    store = _store(tmp_path)
    for index, ceiling in enumerate((90_000, 95_000, 200_000), start=1):
        _vote(store, url=f"https://t.me/a/{index}", reason="s", salary=ceiling, company=f"Co{index}")
    created = refresh_suggestions(store, now=NOW)
    assert created[0]["payload"] == {"value": 100_000}

    blocked = SeenStore(tmp_path / "liked.db")
    for index, ceiling in enumerate((90_000, 95_000, 120_000), start=1):
        _vote(blocked, url=f"https://t.me/a/{index}", reason="s", salary=ceiling)
    _vote(blocked, url="https://t.me/a/9", verdict="relevant", reason=None, salary=90_000)
    assert refresh_suggestions(blocked, now=NOW) == []


def test_salary_accept_changes_gate_and_reset_restores_config(tmp_path):
    store = _store(tmp_path)
    for index, ceiling in enumerate((90_000, 95_000, 100_000), start=1):
        _vote(store, url=f"https://t.me/a/{index}", reason="s", salary=ceiling)
    suggestion_id = refresh_suggestions(store, now=NOW)[0]["id"]
    assert accept_suggestion(store, suggestion_id, now=NOW).startswith("Применено")
    assert effective_gates(store).min_salary_rub == 100_000
    low = _card(salary="от 90 000 ₽", url="https://t.me/job_python/9")
    assert evaluate(low)[0] == "pass"
    assert evaluate(low, gates=effective_gates(store))[0] == "reject"
    assert "MIN_SALARY_RUB = 100000" in overrides_report(store)
    assert reset_override(store, "min_salary_rub", now=NOW).startswith("MIN_SALARY_RUB")
    assert effective_gates(store).min_salary_rub == 80_000
    assert store.suggestion(suggestion_id)["status"] == "reverted"


def test_out_of_range_override_is_ignored(tmp_path):
    store = _store(tmp_path)
    store.upsert_override(
        key="MIN_SALARY_RUB",
        value="99999999",
        suggestion_id=None,
        previous_value="80000",
        applied_at=NOW.isoformat(),
    )
    assert effective_gates(store).min_salary_rub == 80_000


def test_senior_penalty_steps_and_likes_block_it(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        _vote(store, url=f"https://t.me/a/{index}", reason="g", title="Senior Python Developer")
    created = refresh_suggestions(store, now=NOW)
    assert created[0]["payload"] == {"value": 40}
    accept_suggestion(store, created[0]["id"], now=NOW)
    item = _card(title="Senior Python Developer")
    before = evaluate(item, gates=GateSettings(80_000, 30, GRADES))[1]["score"]
    after = evaluate(item, gates=effective_gates(store))[1]["score"]
    assert before - after == 10

    liked = SeenStore(tmp_path / "liked.db")
    for index in range(3):
        _vote(liked, url=f"https://t.me/a/{index}", reason="g", title="Senior Python Developer")
    for index in range(3):
        _vote(liked, url=f"https://t.me/b/{index}", verdict="relevant", reason=None, title="Senior Python Developer")
    assert refresh_suggestions(liked, now=NOW) == []


def test_grade_dictionary_needs_two_cards_and_ignores_staffing(tmp_path):
    store = _store(tmp_path)
    _vote(store, url="https://t.me/a/1", reason="g", title="Python Architect")
    assert refresh_suggestions(store, now=NOW) == []
    _vote(store, url="https://t.me/a/2", reason="g", title="Python Architect")
    created = refresh_suggestions(store, now=NOW)
    assert created[0]["payload"] == {"word": "architect"}
    store.remember_card(_card(title="Python Architect", url="https://t.me/job_python/5"), None)
    assert archive_delta(store, Proposal("excluded_grade", {"word": "architect"}, created[0]["evidence"])) == 1
    accept_suggestion(store, created[0]["id"], now=NOW)
    assert evaluate(_card(title="Python Architect"), gates=effective_gates(store))[0] == "reject"
    assert "architect" in effective_gates(store).excluded_grades

    staffing = SeenStore(tmp_path / "staff.db")
    _vote(staffing, url="https://t.me/a/1", reason="g", title="Python staffing")
    _vote(staffing, url="https://t.me/a/2", reason="g", title="Python staffing")
    _vote(staffing, url="https://t.me/a/3", reason="g", title="Senior Python Developer")
    assert refresh_suggestions(staffing, now=NOW) == []


def test_decline_holds_until_evidence_doubles(tmp_path):
    store = _store(tmp_path)
    _vote(store, url="https://t.me/a/1", company="Acme Studio")
    _vote(store, url="https://t.me/a/2", company="Acme Studio")
    suggestion_id = refresh_suggestions(store, now=NOW)[0]["id"]
    assert decline_suggestion(store, suggestion_id, now=NOW).startswith("Предложение отклонено")
    assert refresh_suggestions(store, now=NOW) == []
    _vote(store, url="https://t.me/a/3", company="Acme Studio")
    _vote(store, url="https://t.me/a/4", company="Acme Studio")
    assert refresh_suggestions(store, now=NOW)[0]["payload"]["pattern"] == "Acme Studio"


def test_old_decline_does_not_block_fresh_votes(tmp_path):
    store = _store(tmp_path)
    store.insert_suggestion(
        kind="blacklist",
        payload=json.dumps({"pattern": "Acme Studio"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        evidence="[1,2]",
        created_at=(NOW - timedelta(days=40)).isoformat(),
    )
    store.set_suggestion_status(1, "declined", (NOW - timedelta(days=31)).isoformat())
    _vote(store, url="https://t.me/a/1", company="Acme Studio")
    _vote(store, url="https://t.me/a/2", company="Acme Studio")
    assert refresh_suggestions(store, now=NOW)[0]["kind"] == "blacklist"


def test_votes_older_than_window_are_ignored(tmp_path):
    store = _store(tmp_path)
    old = NOW - timedelta(days=31)
    _vote(store, url="https://t.me/a/1", company="Acme Studio", when=old)
    _vote(store, url="https://t.me/a/2", company="Acme Studio", when=old)
    assert refresh_suggestions(store, now=NOW) == []


def test_undo_removes_the_vote_and_the_label(tmp_path):
    store = _store(tmp_path)
    card = _card(company="Acme Studio", url="https://t.me/job_python/1")
    store.remember_card(card, None)
    prefix = _prefix("telegram", card["url"])
    assert label_reply(f"j:{prefix}", store) == "Отмечено: мусор"
    text, keyboard = reason_reply(f"c:{prefix}", store)
    assert text == "Причина сохранена: спам-компания или канал."
    assert keyboard is not None
    _vote(store, url="https://t.me/job_python/2", company="Acme Studio")
    assert refresh_suggestions(store, now=NOW)
    undo_text, undo_keyboard = reason_reply(f"u:{prefix}", store)
    assert undo_text == "Оценка отменена."
    assert undo_keyboard is not None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT label FROM vacancy_labels").fetchall() == []
    assert refresh_suggestions(store, now=NOW) == []
    assert store.pending_suggestions() == []


def test_reason_buttons_fit_callback_limit():
    prefix = "a" * HASH_PREFIX_CHARS
    for row in reason_keyboard(prefix).inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) <= 64
    for button in card_actions_keyboard(prefix).inline_keyboard[0]:
        assert len(button.callback_data.encode("utf-8")) <= 64
    assert history_limit("") == 10
    assert history_limit("30") == 30
    assert history_limit("31") is None


def test_report_history_and_reset_keep_applied_changes(tmp_path):
    store = _store(tmp_path)
    for index in range(2):
        _vote(store, url=f"https://t.me/a/{index}", company="Acme Studio")
    suggestion_id = refresh_suggestions(store, now=NOW)[0]["id"]
    accept_suggestion(store, suggestion_id, now=NOW)
    report = feedback_report(store, now=NOW)
    assert "Карточек: 2" in report
    assert "Acme Studio — 2" in report
    text, active = feedback_history(store, 10)
    assert "Acme Studio" not in text
    assert "мусор" in text
    assert active
    assert reset_journal(store).startswith("Журнал очищен")
    assert store.feedback_recent(10) == []
    assert store.blacklist_patterns() == ["Acme Studio"]
    assert store.suggestion(suggestion_id)["status"] == "accepted"


def test_undo_from_history_marks_event(tmp_path):
    store = _store(tmp_path)
    event_id = _vote(store, url="https://t.me/a/1", company="Acme Studio")
    assert undo_event(store, event_id, now=NOW) == "Оценка отменена."
    assert undo_event(store, event_id, now=NOW) == "Оценка уже отменена."
    text, active = feedback_history(store, 10)
    assert "отменено" in text
    assert active == []


def test_old_blacklist_table_gains_suggestion_column(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE blacklist (
                id INTEGER PRIMARY KEY,
                pattern TEXT NOT NULL UNIQUE COLLATE NOCASE,
                added_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE seen_vacancies (
                content_hash TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                score INTEGER NOT NULL,
                seen_at TEXT NOT NULL
            )
            """
        )
    store = SeenStore(path)
    assert store.blacklist_add("Acme", suggestion_id=7) == "added"
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(blacklist)")}
        assert "suggestion_id" in columns
        assert connection.execute("SELECT suggestion_id FROM blacklist").fetchone()[0] == 7
