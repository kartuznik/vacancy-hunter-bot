"""Детерминированный фильтр и скоринг. Сеть и LLM не используются."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import (
    BLACKLIST_HARD,
    BLACKLIST_LOCATION,
    BLACKLIST_SOFT,
    CORE_PYTHON,
    EXCLUDED_GRADES,
    PENALTY_SENIOR,
    ROLE_MARKERS,
    SENIOR_WORDS,
    VACANCY_MARKERS,
    WHITELIST,
)

WHITELIST_BONUS = 30
CORE_ONLY_BONUS = 10
JUNIOR_BONUS = 20
REMOTE_BONUS = 15
FRESH_BONUS = 10
SOFT_PENALTY = 15
FRESH_WINDOW = timedelta(hours=24)
HEAD_CHARS = 300

_JUNIOR = re.compile(
    r"(?<![\w])(?:junior|intern|стажер\w{0,6}|без\s+опыта)(?![\w])",
    re.IGNORECASE,
)
_REMOTE = re.compile(
    r"(?<![\w])(?:remote|удален(?:но|ка|ке|ку|ки|ная|ную|ной|ное|ный|ные)?|дистанционн\w*)(?![\w])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VacancyScore:
    score: int
    whitelist_hits: tuple[str, ...]
    soft_hits: tuple[str, ...]
    junior: bool
    remote: bool
    fresh: bool


def score_text(
    text: str,
    published_at: datetime | None = None,
    *,
    now: datetime | None = None,
) -> VacancyScore | None:
    normalized = _normalize(text)
    if not normalized.strip():
        return None

    head = _head(normalized)
    remote = _REMOTE.search(normalized) is not None

    if _has_any(head, BLACKLIST_HARD):
        return None
    if not remote and _has_any(normalized, BLACKLIST_LOCATION):
        return None

    if not _has_any(normalized, VACANCY_MARKERS):
        return None
    if not _has_any(normalized, CORE_PYTHON):
        return None

    whitelist_hits = tuple(term for term in WHITELIST if _pattern(term).search(normalized))
    if not whitelist_hits and not _has_any(head, ROLE_MARKERS):
        return None

    soft_hits = tuple(term for term in BLACKLIST_SOFT if _pattern(term).search(normalized))
    junior = _JUNIOR.search(normalized) is not None
    fresh = _is_fresh(published_at, now)

    if whitelist_hits:
        score = WHITELIST_BONUS * len(whitelist_hits)
    else:
        score = CORE_ONLY_BONUS
    if junior:
        score += JUNIOR_BONUS
    if remote:
        score += REMOTE_BONUS
    if fresh:
        score += FRESH_BONUS
    score -= SOFT_PENALTY * len(soft_hits)

    return VacancyScore(
        score=score,
        whitelist_hits=whitelist_hits,
        soft_hits=soft_hits,
        junior=junior,
        remote=remote,
        fresh=fresh,
    )


def excluded_grade(title: str, grades: tuple[str, ...] = EXCLUDED_GRADES) -> str | None:
    normalized = _normalize(title)
    for grade in grades:
        if _grade_pattern(grade).search(normalized):
            return grade
    return None


def senior_penalty(title: str, penalty: int = PENALTY_SENIOR) -> int:
    if penalty and _has_any(_normalize(title), SENIOR_WORDS):
        return penalty
    return 0


def _grade_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(_normalize(term))
    if re.search(r"[а-яА-Я]", term):
        return re.compile(rf"(?<![\w]){escaped}", re.IGNORECASE)
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def _head(normalized: str) -> str:
    title, _, description = normalized.partition("\n")
    return f"{title}\n{description[:HEAD_CHARS]}"


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_pattern(term).search(text) for term in terms)


def _normalize(text: str) -> str:
    return text.replace("ё", "е").replace("Ё", "Е")


def _pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(_normalize(term))
    # «тестиров» — основа (тестировщик, тестирование): граница только слева.
    if _normalize(term) == "тестиров":
        return re.compile(rf"(?<![\w]){escaped}", re.IGNORECASE)
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def _is_fresh(published_at: datetime | None, now: datetime | None) -> bool:
    if published_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    published = published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age = current - published
    return timedelta(0) <= age < FRESH_WINDOW
