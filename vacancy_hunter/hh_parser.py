"""Загрузка вакансий с api.hh.ru. Ошибки 403/429 не останавливают пайплайн."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from config import USER_AGENT

logger = logging.getLogger(__name__)

HH_URL = "https://api.hh.ru/vacancies"
REQUEST_TIMEOUT = 10


def fetch_hh(queries: list[str] | tuple[str, ...]) -> list[dict]:
    vacancies: list[dict] = []
    if not queries:
        logger.warning("HH_QUERIES пуст, источник HH пропущен")
        return vacancies

    for query in queries:
        try:
            response = requests.get(
                HH_URL,
                params={
                    "text": query,
                    "schedule": "remote",
                    "order_by": "publication_time",
                    "per_page": 50,
                    "period": 3,
                },
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("HH запрос %r не выполнен: %s", query, exc)
            continue

        if response.status_code in (403, 429):
            logger.warning(
                "HH вернул %s для запроса %r, источник по этому запросу пропущен",
                response.status_code,
                query,
            )
            continue

        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("HH ответ для %r не разобран: %s", query, exc)
            continue

        for item in payload.get("items") or []:
            parsed = _parse_item(item)
            if parsed is not None:
                vacancies.append(parsed)

    return vacancies


def _parse_item(item: dict) -> dict | None:
    url = (item.get("alternate_url") or "").strip()
    title = (item.get("name") or "").strip()
    if not url or not title:
        return None
    snippet = item.get("snippet") or {}
    description = " ".join(
        part
        for part in (
            snippet.get("requirement") or "",
            snippet.get("responsibility") or "",
        )
        if part
    ).strip()
    return {
        "source": "hh",
        "source_type": "hh",
        "channel_name": "",
        "id": str(item.get("id") or ""),
        "title": title,
        "url": url,
        "salary": _format_salary(item.get("salary")),
        "description": description,
        "published_at": _parse_hh_time(item.get("published_at")),
    }


def _format_salary(salary: dict | None) -> str:
    if not isinstance(salary, dict):
        return ""
    currency = str(salary.get("currency") or "").strip()
    amount_from = salary.get("from")
    amount_to = salary.get("to")
    if amount_from and amount_to:
        text = f"{amount_from}–{amount_to}"
    elif amount_from:
        text = f"от {amount_from}"
    elif amount_to:
        text = f"до {amount_to}"
    else:
        return ""
    gross = salary.get("gross")
    suffix = " до вычета налогов" if gross else ""
    return f"{text} {currency}{suffix}".strip()


def _parse_hh_time(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("HH дата не разобрана: %s", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
