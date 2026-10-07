"""Фильтр, скоринг и разбор HH без сетевых запросов."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from vacancy_hunter.core import send_telegram, write_digest
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
    result = score_text("Python developer studies reaction times, fastapi", now=NOW)
    assert result is not None
    assert result.whitelist_hits == ("fastapi",)
    assert "react" not in result.soft_hits


def test_node_js_and_phrase_blacklist():
    assert score_text("Python backend and Node.js", now=NOW) is None
    assert score_text("Python data scientist remote", now=NOW) is None


def test_testing_stem_is_rejected():
    assert score_text("Python тестировщик backend", now=NOW) is None


def test_full_score_for_junior_remote_fresh():
    published = NOW - timedelta(hours=2)
    result = score_text(
        "Junior Python developer, удаленно, django",
        published_at=published,
        now=NOW,
    )
    assert result is not None
    assert result.junior is True
    assert result.remote is True
    assert result.fresh is True
    assert result.whitelist_hits == ("django",)
    assert result.score == 30 + 20 + 15 + 10


def test_whitelist_bonus_is_per_keyword():
    result = score_text("Python developer django fastapi", now=NOW)
    assert result is not None
    assert result.whitelist_hits == ("fastapi", "django")
    assert result.score == 30 * 2


def test_senior_and_team_lead_are_hard_rejects():
    assert score_text("Python developer senior fastapi", now=NOW) is None
    assert score_text("Python developer team lead fastapi", now=NOW) is None


def test_soft_hit_is_unique_and_middle_plus_matches():
    result = score_text("Python developer middle+ middle+ fastapi", now=NOW)
    assert result is not None
    assert result.soft_hits == ("middle+",)
    assert result.score == 15


def test_yo_normalization_matches_remote():
    result = score_text("Python developer удалёнка fastapi", now=NOW)
    assert result is not None
    assert result.remote is True
    assert result.whitelist_hits == ("fastapi",)
    assert result.score == 30 + 15


def test_generic_titles_and_moscow_are_rejected():
    assert score_text(".NET разработчик", now=NOW) is None
    assert score_text("Golang", now=NOW) is None
    assert score_text("Python разработчик, Москва", now=NOW) is None


def test_python_junior_remote_fresh_gets_full_score():
    published = NOW - timedelta(hours=1)
    result = score_text(
        "python junior developer удаленно fastapi",
        published_at=published,
        now=NOW,
    )
    assert result is not None
    assert result.whitelist_hits == ("fastapi",)
    assert result.junior is True
    assert result.remote is True
    assert result.fresh is True
    assert result.soft_hits == ()
    assert result.score == 30 + 20 + 15 + 10


def test_stale_vacancy_has_no_fresh_bonus():
    published = NOW - timedelta(hours=30)
    result = score_text("Python junior developer fastapi", published_at=published, now=NOW)
    assert result is not None
    assert result.fresh is False
    assert result.score == 50


def test_news_non_python_and_senior_are_rejected():
    assert score_text("Россияне заработали 5 млрд", now=NOW) is None
    assert score_text("postgresql DBA", now=NOW) is None
    assert score_text("Senior Python Developer", now=NOW) is None


def test_python_core_without_whitelist_scores_low():
    result = score_text("python-backend developer", now=NOW)
    assert result is not None
    assert result.whitelist_hits == ()
    assert result.score == 10
    assert score_text("Java разработчик", now=NOW) is None
    assert score_text("Компания увеличила выручку за квартал", now=NOW) is None


def test_junior_python_fastapi_scores_50():
    result = score_text("Junior Python Developer с fastapi", now=NOW)
    assert result is not None
    assert result.junior is True
    assert result.whitelist_hits == ("fastapi",)
    assert result.soft_hits == ()
    assert result.score == 50


HABR_ITK_MOSCOW = (
    "Требуется «Python Developer» (Москва, от 75 000 ₽)\n"
    "Компания «ITK academy» ищет хорошего специалиста на вакансию «Python Developer». "
    "Москва (Россия), Санкт-Петербург (Россия), Казань (Россия). От 75 000 ₽. "
    "Полный рабочий день. Можно удалённо. Требуемые навыки: #junior, #Git, #SQL, "
    "#Python, #ООП, #Docker, #Django, #FastAPI, #PostgreSQL."
)

WILDBERRIES_REMOTE = (
    "Wildberries & Russ\n"
    "#middle\n#удаленка\n#гибрид\n#офис\nWildberries & Russ\n"
    "Python-разработчик (Направление клиентского сервиса)\nОпыт работы: от 3 лет\n"
    "Формат работы: гибрид, удалённо или офис (Москва)\n☑️\nЧем предстоит заниматься\n"
    "-Разрабатывать и поддерживать сервисы, отвечающие пользователям на запросы в поддержку\n"
    "-Разрабатывать HTTP API с использованием FastAPI, Django, SQLAlchemy\n"
    "-Заниматься профайлингом и оптимизацией производительности сервисов\n"
    "-Разрабатывать и поддерживать фронтенд административных панелей, используя Django templates\n"
    "☑️\nНаши пожелания к кандидатам\n"
    "-Знания python, FastAPI, Django, SQLAlchemy, SQL, Unix/Linux, Docker\n"
    "-Опыт работы с ClickHouse, PostgreSQL\n"
    "-Опыт работы с Java Script и умение работать с React SPA (будет плюсом)"
)

AI_PYTHON_ENGINEER = (
    "AI Python Engineer\n"
    "🤗\nAI Python Engineer\nУдаленно (Санкт-Петербург)\nCRT\n"
    "— IT-компания с 20-летним опытом разработки, понятными процессами и "
    "профессиональным менеджментом. Мы работаем с разными IT-проектами и базируемся в Тюмени.\n"
    "Требования:\n— От 3 лет коммерческой разработки на Python и сильный backend-фундамент\n"
    "— Практический опыт с LLM, RAG или AI-агентами в реальных проектах\n"
    "— Знание async, типизации, тестирования и разработки REST API\n"
    "— Опыт работы с FastAPI, Django"
)

SBER_OFFICE_MOSCOW = (
    "Python-разработчик\n"
    "🤨\nPython-разработчик\nОфис (Москва)\nСбербанк\n"
    "— крупнейший банк в России, Центральной и Восточной Европе.\n"
    "Требования:\n— опыт промышленной разработки на Python.\n"
    "— глубокое понимание алгоритмов и структур данных.\n— знание Linux и контейнеризации."
)

HABR_AQA = (
    "Требуется «AQA Тестировщик (Python)» (от 75 000 до 90 000 ₽)\n"
    "Компания «ITK academy» ищет хорошего специалиста на вакансию «AQA Тестировщик (Python)». "
    "От 75 000 ₽ до 90 000 ₽. Полный рабочий день. Можно удалённо. "
    "Требуемые навыки: #junior, #Python, #ООП, #Базыданных, #REST, #SQL, #HTTP."
)

SENIOR_AYA_GAMES = (
    "Senior Python Developer\n"
    "Senior Python Developer\nв\nAya Games\n"
    "— компания-разработчик мобильной MMO RPG Riorise.\n"
    "Удалённая работа. Гибкий график.\nОписание вакансии на GeekJob.ru"
)

COURSE_AD = (
    "Стать специалистом по Data Science всего за 10 недель — это реально!\n"
    "Если давно смотрите в сторону Data Science, но откладываете старт из-за огромного "
    "количества технологий — сейчас можно зайти в профессию по-другому. "
    "Симулейтив запускает интенсивный буткемп по Data Science. "
    "Стек: Python, Git, продвинутая статистика и A/B-тестирование, Airflow. Ищем тех, кто готов учиться."
)


def test_real_remote_python_vacancies_pass():
    habr = score_text(HABR_ITK_MOSCOW, now=NOW)
    assert habr is not None
    assert habr.remote is True
    wildberries = score_text(WILDBERRIES_REMOTE, now=NOW)
    assert wildberries is not None
    assert wildberries.remote is True
    assert "fastapi" in wildberries.whitelist_hits
    engineer = score_text(AI_PYTHON_ENGINEER, now=NOW)
    assert engineer is not None
    assert "llm" in engineer.whitelist_hits


def test_real_office_tester_senior_and_course_are_rejected():
    assert score_text(SBER_OFFICE_MOSCOW, now=NOW) is None
    assert score_text(HABR_AQA, now=NOW) is None
    assert score_text(SENIOR_AYA_GAMES, now=NOW) is None
    assert score_text(COURSE_AD, now=NOW) is None


def test_location_blocks_only_without_remote():
    assert score_text("Python разработчик, офис (Москва), fastapi", now=NOW) is None
    result = score_text("Python разработчик, Москва или дистанционно, fastapi", now=NOW)
    assert result is not None
    assert result.remote is True


def test_stack_terms_are_checked_only_in_head():
    tail = "x" * 400 + " react и тестирование"
    assert score_text(f"Python разработчик fastapi\n{tail}", now=NOW) is not None
    assert score_text("Python разработчик fastapi\nстек react", now=NOW) is None


def test_core_only_needs_role_in_head_and_lider_is_senior():
    assert score_text("Оффер в IT за 3 дня - да!\nИщем тех, кто знает python", now=NOW) is None
    result = score_text("Python-разработчик (backend)\nищем в команду", now=NOW)
    assert result is not None
    assert result.score == 10
    assert score_text("MLOps Engineer\nЛидер команды, python, вакансия", now=NOW) is None


def test_digest_name_is_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr("vacancy_hunter.core.digests_dir", lambda: tmp_path)
    stats = build_stats({"hh": [], "habr": [], "telegram": []}, 0, [], [])
    moment = datetime(2026, 10, 7, 16, 5)
    first = write_digest([], stats, now=moment)
    second = write_digest([], stats, now=moment)
    later = write_digest([], stats, now=moment + timedelta(hours=4))
    assert first.name == "digest_2026-10-07_1605.md"
    assert second.name == "digest_2026-10-07_1605_2.md"
    assert later.name == "digest_2026-10-07_2005.md"
    assert len(list(tmp_path.glob("digest_*.md"))) == 3


def test_send_retries_once_after_429(monkeypatch):
    calls = []
    sleeps = []

    class _Post:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.ok = status_code == 200
            self._payload = payload or {}

        def json(self):
            return self._payload

    replies = [_Post(429, {"parameters": {"retry_after": 3}}), _Post(200), _Post(200)]

    def fake_post(url, data, timeout):
        calls.append(data["text"])
        return replies.pop(0)

    monkeypatch.setattr("vacancy_hunter.core.requests.post", fake_post)
    monkeypatch.setattr("vacancy_hunter.core.time.sleep", sleeps.append)
    monkeypatch.setattr("vacancy_hunter.core.TELEGRAM_LIMIT", 5)
    send_telegram("token", "chat", [{"title": "x"}], ["first", "second"])
    assert calls == ["first", "first", "second"]
    assert sleeps == [3, 1.0]


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
