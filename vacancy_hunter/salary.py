"""Зарплата вакансии из поля salary, названия или описания. Сеть не используется."""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import CURRENCY_RATES

# Суммы меньше этой — не месячная зарплата («10k подписчиков», «500 ₽ бонус»).
MIN_PLAUSIBLE_RUB = 20_000

_SPACES = re.compile(r"[\u00a0\u202f\u2009\u2007]")
_NUMBER = r"\d{1,3}(?:[ .]\d{3})+|\d+"
_MULT = r"k|к|тыс\.?|т\.р\.?"
_CURRENCY = r"₽|руб\w*\.?|р\.|rub|rur|\$|usd|долл\w*|€|eur|евро"
_HOURLY = re.compile(r"\s*(?:/|в|за|per)\s*(?:час|ч|hour|h)(?![\w])", re.IGNORECASE)
_HASHTAG = re.compile(r"(?<![\w])#(\d{2,3})\s?[kк](?![\w])", re.IGNORECASE)


def _amount(prefix: str) -> str:
    return (
        rf"(?:(?P<{prefix}pre>[$€]|usd|eur|rub|rur)\s?)?(?P<{prefix}num>{_NUMBER})\s?"
        rf"(?:(?P<{prefix}mult>{_MULT})(?![\w]))?\s?"
        rf"(?:(?P<{prefix}cur>{_CURRENCY})(?![\w]))?"
    )


_RANGE = re.compile(rf"(?<![\w])(?:от\s*)?{_amount('a')}\s*(?:-|–|—|до)\s*{_amount('b')}", re.IGNORECASE)
_SINGLE = re.compile(rf"(?<![\w])(?P<kind>от|до)?\s*{_amount('a')}", re.IGNORECASE)


@dataclass(frozen=True)
class SalaryInfo:
    low: int | None
    high: int | None
    text: str
    from_text: bool

    @property
    def ceiling(self) -> int | None:
        return self.high if self.high is not None else self.low

    @property
    def numbers(self) -> set[str]:
        return set(re.findall(r"\d+", self.text))


def parse_salary(item: dict) -> SalaryInfo | None:
    field = str(item.get("salary") or "")
    for text, from_text in ((field, False), (item.get("title") or "", True), (item.get("description") or "", True)):
        found = parse_salary_text(str(text))
        if found is not None:
            low, high, snippet = found
            return SalaryInfo(low, high, field.strip() if not from_text else snippet, from_text)
    return None


def parse_salary_text(text: str) -> tuple[int | None, int | None, str] | None:
    normalized = _SPACES.sub(" ", text)
    for match in _RANGE.finditer(normalized):
        if _hourly(normalized, match.end()):
            continue
        currency = _currency(match, "a") or _currency(match, "b")
        mult_b = match.group("bmult")
        low = _value(match.group("anum"), match.group("amult") or mult_b, currency)
        high = _value(match.group("bnum"), mult_b, currency)
        if low is not None and high is not None and low <= high:
            return low, high, match.group(0).strip()
    for match in _SINGLE.finditer(normalized):
        if _hourly(normalized, match.end()):
            continue
        currency = _currency(match, "a")
        value = _value(match.group("anum"), match.group("amult"), currency)
        if value is None:
            continue
        kind = (match.group("kind") or "").lower()
        if kind == "от":
            return value, None, match.group(0).strip()
        if kind == "до":
            return None, value, match.group(0).strip()
        return value, value, match.group(0).strip()
    hashtag = _HASHTAG.search(normalized)
    if hashtag is not None:
        value = int(hashtag.group(1)) * 1000
        return value, value, hashtag.group(0)
    return None


def salary_below_minimum(info: SalaryInfo | None, minimum: int) -> bool:
    if minimum <= 0 or info is None or info.ceiling is None:
        return False
    return info.ceiling < minimum


def _currency(match: re.Match, prefix: str) -> str | None:
    raw = (match.group(f"{prefix}pre") or match.group(f"{prefix}cur") or "").lower()
    if not raw:
        return None
    if raw in ("rub", "rur"):
        return "RUB"
    if raw in ("$", "usd") or raw.startswith("долл"):
        return "USD"
    if raw in ("€", "eur", "евро"):
        return "EUR"
    return "RUB"


def _value(number: str, mult: str | None, currency: str | None) -> int | None:
    if currency is None and not mult:
        return None
    rate = CURRENCY_RATES.get(currency or "RUB")
    if rate is None:
        return None
    amount = int(re.sub(r"[ .]", "", number))
    if mult:
        amount *= 1000
    rub = int(amount * rate)
    return rub if rub >= MIN_PLAUSIBLE_RUB else None


def _hourly(text: str, end: int) -> bool:
    return _HOURLY.match(text, end) is not None
