"""Статическое сопроводительное письмо: подстановка в шаблон и гвард. Модели не используются."""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import LETTER_COMPANY_FALLBACK, LETTER_TEMPLATE

MAX_LETTER_LENGTH = 2000

_TLDS = (
    "ru|com|io|ai|org|net|dev|me|tech|рф|su|xyz|info|biz|app|co|cc|to|ly|gg|link|site|online|pro|top|"
    "club|shop|store|live|in|us|uk|de|kz|by|ua|cn"
)
URL_RE = re.compile(
    rf"(?i)(?:\b(?:https?://|www\.)\S+|(?<![\w@.])[a-zа-я0-9][a-zа-я0-9-]{{0,62}}(?:\.[a-zа-я0-9-]{{1,63}})*\.(?:{_TLDS})\b\S*)"
)
EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
HANDLE_RE = re.compile(r"(?<![\w@])@[\w]{3,}")
NUMBER_RE = re.compile(r"\d+")
_URL_TAIL = ".,;:!?)»\"'"


@dataclass(frozen=True)
class LetterResult:
    text: str | None
    violations: tuple[str, ...]


def build_letter(card: dict, facts: dict, template: str = LETTER_TEMPLATE) -> str:
    links = tuple(facts.get("links") or ())
    return template.format(
        title=" ".join(str(card.get("title") or "").split()),
        company=" ".join(str(card.get("company") or "").split()) or LETTER_COMPANY_FALLBACK,
        years=str(facts.get("years") or ""),
        stack=str(facts.get("stack") or ""),
        links=f"Ссылки: {', '.join(links)}\n" if links else "",
    )


def guard_letter(text: str, *, facts: dict, salary: str, whitelist: tuple[str, ...]) -> list[str]:
    allowed_numbers = _numbers_of_facts(facts) | set(NUMBER_RE.findall(salary or ""))
    prefixes = {_normalize_url(link) for link in (*whitelist, *(facts.get("links") or ())) if link}
    violations: list[str] = []
    if len(text) > MAX_LETTER_LENGTH:
        violations.append(f"объём {len(text)} знаков больше {MAX_LETTER_LENGTH}")
    allowed_spans: list[tuple[int, int]] = []
    for match in EMAIL_RE.finditer(text):
        violations.append(f"email: {match.group(0)[:60]}")
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TAIL)
        if EMAIL_RE.search(url):
            continue
        if _allowed_url(url, prefixes):
            allowed_spans.append((match.start(), match.start() + len(url)))
        else:
            violations.append(f"ссылка вне белого списка: {url[:60]}")
    for match in HANDLE_RE.finditer(text):
        violations.append(f"контакт: {match.group(0)[:60]}")
    for match in NUMBER_RE.finditer(text):
        if match.group(0) in allowed_numbers:
            continue
        if any(start <= match.start() < end for start, end in allowed_spans):
            continue
        violations.append(f"число не из профиля и не из зарплаты: {match.group(0)}")
    return violations


def compose_letter(card: dict, facts: dict, whitelist: tuple[str, ...]) -> LetterResult:
    text = build_letter(card, facts)
    violations = guard_letter(text, facts=facts, salary=str(card.get("salary") or ""), whitelist=whitelist)
    if violations:
        return LetterResult(None, tuple(violations))
    return LetterResult(text, ())


def _numbers_of_facts(facts: dict) -> set[str]:
    values = [facts.get("years"), facts.get("stack"), *(facts.get("links") or ())]
    return {number for value in values if value for number in NUMBER_RE.findall(str(value))}


def _normalize_url(url: str) -> str:
    value = url.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value.rstrip("/")


def _allowed_url(url: str, prefixes: set[str]) -> bool:
    normalized = _normalize_url(url)
    return any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in prefixes)
