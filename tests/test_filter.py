"""Фильтр, скоринг и разбор HH без сетевых запросов."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from vacancy_hunter.database import SeenStore, default_db_path
from vacancy_hunter.filter_and_score import score_text
from vacancy_hunter.hh_parser import fetch_hh
from vacancy_hunter.report import build_stats, relevance_label, render_markdown, telegram_blocks

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self) -> dict:
        return self._payload


def test_hard_blacklist_rejects_even_with_whitelist():
    assert score_text("Python React developer, backend", now=NOW) is None


def test_missing_whitelist_rejects():
    assert score_text("уборщик офиса junior удаленно", now=NOW) is None


def test_word_boundary_does_not_match_react_inside_reaction():
    result = score_text("Python developer studies reaction times", now=NOW)
    assert result is not None
    assert "react" not in result.soft_hits


def test_node_js_and_phrase_blacklist():
    assert score_text("Python backend and Node.js", now=NOW) is None
    assert score_text("Python data scientist remote", now=NOW) is None


def test_testing_stem_is_rejected():
    assert score_text("Python тестировщик backend", now=NOW) is None


def test_full_score_for_junior_remote_fresh():
    published = NOW - timedelta(hours=2)
    result = score_text(
        "Junior Python backend, удаленно, django",
        published_at=published,
        now=NOW,
    )
    assert result is not None
    assert result.junior is True
    assert result.remote is True
    assert result.fresh is True
    assert result.whitelist_hits == ("python", "django")
    assert result.score == 30 * 2 + 20 + 15 + 10


def test_whitelist_bonus_is_per_keyword():
    result = score_text("Python django fastapi backend", now=NOW)
    assert result is not None
    assert result.whitelist_hits == ("python", "fastapi", "django")
    assert result.score == 30 * 3


def test_each_unique_soft_hit_costs_15():
    result = score_text("Python senior team lead backend", now=NOW)
    assert result is not None
    assert result.soft_hits == ("senior", "team lead")
    assert result.score == 30 - 15 - 15


def test_soft_hit_is_unique_and_middle_plus_matches():
    result = score_text("Python middle+ middle+ backend", now=NOW)
    assert result is not None
    assert result.soft_hits == ("middle+",)
    assert result.score == 15


def test_yo_normalization_matches_remote():
    result = score_text("Python удалёнка", now=NOW)
    assert result is not None
    assert result.remote is True
    assert result.whitelist_hits == ("python",)
    assert result.score == 30 + 15


def test_generic_titles_and_moscow_are_rejected():
    assert score_text(".NET разработчик", now=NOW) is None
    assert score_text("Golang", now=NOW) is None
    assert score_text("Python разработчик, Москва", now=NOW) is None


def test_python_junior_remote_fresh_gets_full_score():
    published = NOW - timedelta(hours=1)
    result = score_text("python junior удаленно", published_at=published, now=NOW)
    assert result is not None
    assert result.whitelist_hits == ("python",)
    assert result.junior is True
    assert result.remote is True
    assert result.fresh is True
    assert result.soft_hits == ()
    assert result.score == 30 + 20 + 15 + 10


def test_stale_vacancy_has_no_fresh_bonus():
    published = NOW - timedelta(hours=30)
    result = score_text("Python backend junior", published_at=published, now=NOW)
    assert result is not None
    assert result.fresh is False
    assert result.score == 50


def test_hh_forbidden_returns_empty(monkeypatch, caplog):
    def fake_get(*args, **kwargs):
        return _Response(403, {"errors": [{"type": "forbidden"}]})

    monkeypatch.setattr("vacancy_hunter.hh_parser.requests.get", fake_get)
    with caplog.at_level("WARNING"):
        assert fetch_hh(["python"]) == []
    assert "403" in caplog.text


def test_hh_too_many_requests_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "vacancy_hunter.hh_parser.requests.get",
        lambda *args, **kwargs: _Response(429),
    )
    assert fetch_hh(["python"]) == []


def test_hh_success_parses_fields(monkeypatch):
    payload = {
        "items": [
            {
                "id": "42",
                "name": "Python backend",
                "alternate_url": "https://hh.ru/vacancy/42",
                "published_at": "2026-10-02T10:00:00+0300",
                "salary": {"from": 100000, "to": 150000, "currency": "RUR", "gross": True},
                "snippet": {
                    "requirement": "django и fastapi",
                    "responsibility": "писать API",
                },
            }
        ]
    }

    def fake_get(*args, **kwargs):
        params = kwargs.get("params") or {}
        assert params["schedule"] == "remote"
        assert params["order_by"] == "publication_time"
        assert params["per_page"] == 50
        assert params["period"] == 3
        assert kwargs["timeout"] == 10
        assert "Mozilla/5.0" in kwargs["headers"]["User-Agent"]
        return _Response(200, payload)

    monkeypatch.setattr("vacancy_hunter.hh_parser.requests.get", fake_get)
    rows = fetch_hh(["python"])
    assert len(rows) == 1
    assert rows[0]["source"] == "hh"
    assert rows[0]["id"] == "42"
    assert rows[0]["title"] == "Python backend"
    assert rows[0]["url"] == "https://hh.ru/vacancy/42"
    assert rows[0]["salary"] == "100000–150000 RUR до вычета налогов"
    assert "django" in rows[0]["description"]
    assert rows[0]["published_at"] == datetime(2026, 10, 2, 10, 0, tzinfo=timezone(timedelta(hours=3)))


def test_database_deduplicates_by_source_and_url(tmp_path):
    store = SeenStore(tmp_path / "seen_vacancies.db")
    assert store.remember("hh", "https://hh.ru/vacancy/1", "Python", 30) is True
    assert store.has("hh", "https://hh.ru/vacancy/1") is True
    assert store.remember("hh", "https://hh.ru/vacancy/1", "Python", 30) is False
    assert store.remember("habr", "https://hh.ru/vacancy/1", "Python", 30) is True


def test_default_db_path_follows_package_parent():
    path = default_db_path()
    repo_root = Path(__file__).resolve().parents[1]
    assert path.parent.parent == repo_root
    assert path.parts[-2:] == ("data", "seen_vacancies.db")


def test_relevance_boundaries():
    assert relevance_label(101) == "🔥 HIGH"
    assert relevance_label(100) == "⚡ MEDIUM"
    assert relevance_label(50) == "⚡ MEDIUM"
    assert relevance_label(49) == "📄 LOW"


def _sample_vacancies() -> list[dict]:
    description = ("описание " * 60) + "TAIL_MARKER"
    return [
        {
            "source": "telegram",
            "source_type": "telegram",
            "channel_name": "geekjobs",
            "title": "Стажёр",
            "score": 40,
            "url": "https://t.me/geekjobs/1",
            "salary": "",
            "description": "короткий текст",
            "matched_whitelist_words": ["python"],
        },
        {
            "source": "hh",
            "source_type": "hh",
            "channel_name": "",
            "title": "Python в продукт",
            "score": 120,
            "url": "https://hh.ru/vacancy/1",
            "salary": "100000",
            "description": description,
            "matched_whitelist_words": ["python", "fastapi"],
        },
        {
            "source": "telegram",
            "source_type": "telegram",
            "channel_name": "forpython",
            "title": "Django стажёр",
            "score": 80,
            "url": "https://t.me/forpython/2",
            "salary": "",
            "description": "django remote",
            "matched_whitelist_words": ["django"],
        },
        {
            "source": "habr",
            "source_type": "habr",
            "channel_name": "",
            "title": "Docker для сервиса",
            "score": 60,
            "url": "https://career.habr.com/vacancies/1",
            "salary": "",
            "description": "docker",
            "matched_whitelist_words": ["docker"],
        },
    ]


def test_digest_groups_sources_channels_and_matches():
    items = _sample_vacancies()
    stats = build_stats(
        {"hh": [{}, {}], "habr": [{}], "telegram": [{}, {}, {}]},
        rejected=7,
        passed=items,
        new_items=items[:2],
    )
    text = render_markdown(items, stats, day="2026-10-02")
    hh = text.index("🟢 HH.ru")
    habr = text.index("🔵 Habr Career")
    telegram = text.index("🟣 Telegram")
    assert hh < habr < telegram
    assert text.index("### forpython") < text.index("### geekjobs")
    assert "Совпадения: python, fastapi" in text
    assert "🔥 HIGH" in text
    long_description = items[1]["description"]
    assert long_description[:500] in text
    assert "TAIL_MARKER" not in text
    assert "Всего собрано: 6" in text
    assert "Отсеяно фильтром: 7" in text
    assert "Прошло фильтр: 4" in text
    assert "Новых: 2" in text
    assert "HH.ru — 2" in text
    assert "Habr — 1" in text
    assert "Telegram — 3" in text
    top = text.split("### Топ-3 по скору", 1)[1]
    assert top.index("Python в продукт") < top.index("Django стажёр") < top.index("Docker для сервиса")
    assert "Стажёр" not in top

    telegram_text = "\n\n".join(telegram_blocks(items, stats))
    assert telegram_text.index("🟢 HH.ru") < telegram_text.index("🔵 Habr Career") < telegram_text.index("🟣 Telegram")
    assert telegram_text.index("forpython") < telegram_text.index("geekjobs")
    assert "Совпадения: django" in telegram_text
    assert long_description[:120] not in telegram_text
    assert "Топ-3 по скору" in telegram_text
