"""Детерминированный фильтр и скоринг. Сеть и LLM не используются."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import BLACKLIST_HARD, BLACKLIST_SOFT, WHITELIST

WHITELIST_BONUS = 30
JUNIOR_BONUS = 20
REMOTE_BONUS = 15
FRESH_BONUS = 10
SOFT_PENALTY = 15
FRESH_WINDOW = timedelta(hours=24)

_JUNIOR = re.compile(
    r"(?<![\w])(?:junior|intern|стажер\w{0,6}|без\s+опыта)(?![\w])",
    re.IGNORECASE,
)
_REMOTE = re.compile(
    r"(?<![\w])(?:remote|удален(?:но|ка|ке|ку|ки|ная|ную|ной|ное)?)(?![\w])",
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

    for term in BLACKLIST_HARD:
        if _pattern(term).search(normalized):
            return None

    whitelist_hits = tuple(term for term in WHITELIST if _pattern(term).search(normalized))
    if not whitelist_hits:
        return None

    soft_hits = tuple(term for term in BLACKLIST_SOFT if _pattern(term).search(normalized))
    junior = _JUNIOR.search(normalized) is not None
    remote = _REMOTE.search(normalized) is not None
    fresh = _is_fresh(published_at, now)

    score = WHITELIST_BONUS * len(whitelist_hits)
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
