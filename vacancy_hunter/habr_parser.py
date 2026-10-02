"""Разбор RSS Хабр Карьеры через feedparser."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

from config import USER_AGENT

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10


def fetch_habr(url: str) -> list[dict]:
    if not url:
        logger.warning("HABR_RSS пуст, источник Хабр пропущен")
        return []
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Habr RSS недоступен: %s", exc)
        return []

    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        logger.warning("Habr RSS не разобран: %s", getattr(parsed, "bozo_exception", ""))
        return []

    vacancies: list[dict] = []
    for entry in parsed.entries:
        item = _parse_entry(entry)
        if item is not None:
            vacancies.append(item)
    return vacancies


def _parse_entry(entry) -> dict | None:
    title = (getattr(entry, "title", "") or "").strip()
    url = (getattr(entry, "link", "") or "").strip()
    if not title or not url:
        return None
    raw_description = getattr(entry, "description", "") or getattr(entry, "summary", "") or ""
    description = BeautifulSoup(raw_description, "html.parser").get_text(" ", strip=True)
    guid = (getattr(entry, "guid", "") or getattr(entry, "id", "") or url).strip()
    return {
        "source": "habr",
        "id": guid,
        "title": title,
        "url": url,
        "salary": "",
        "description": description,
        "published_at": _parse_published(entry),
    }


def _parse_published(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        logger.warning("Habr дата не разобрана")
        return None
