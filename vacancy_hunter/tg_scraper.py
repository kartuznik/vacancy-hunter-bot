"""Сбор текста постов с публичных страниц https://t.me/s/<channel>."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
MAX_ATTEMPTS = 3
MIN_TEXT_LENGTH = 80


def fetch_telegram(channels: list[str] | tuple[str, ...] | None = None) -> list[dict]:
    vacancies: list[dict] = []
    if channels is None:
        from vacancy_hunter.database import SeenStore

        channels = SeenStore().channels_for_search()
    if not channels:
        logger.warning("Активных Telegram-каналов нет, источник пропущен")
        return vacancies

    for channel in channels:
        name = channel.strip().lstrip("@")
        if not name:
            continue
        html = _download(f"https://t.me/s/{name}")
        if html is None:
            continue
        vacancies.extend(_parse_channel(name, html))
    return vacancies


def _download(url: str) -> str | None:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Telegram %s попытка %s: %s", url, attempt, exc)
            time.sleep(attempt)
            continue
        if response.status_code in (429, 503):
            logger.warning("Telegram %s вернул %s, повтор", url, response.status_code)
            time.sleep(attempt)
            continue
        if response.status_code >= 400:
            logger.warning("Telegram %s вернул %s, канал пропущен", url, response.status_code)
            return None
        return response.text
    logger.warning("Telegram %s не ответил после повторов", url)
    return None


def _parse_channel(channel: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[dict] = []
    for message in soup.select("div.tgme_widget_message"):
        parsed = _parse_message(channel, message)
        if parsed is not None:
            vacancies.append(parsed)
    return vacancies


def _parse_message(channel: str, message) -> dict | None:
    text_node = message.select_one("div.tgme_widget_message_text")
    if text_node is None:
        return None
    text = text_node.get_text("\n", strip=True)
    if len(text) < MIN_TEXT_LENGTH:
        return None
    link = message.select_one("a.tgme_widget_message_date")
    url = ""
    if link is not None:
        url = (link.get("href") or "").strip()
    if not url or url.rstrip("/").endswith(f"/{channel}"):
        return None
    published_at = None
    time_node = message.select_one("time")
    if time_node is not None:
        published_at = _parse_time(time_node.get("datetime"))
    title = _title_from_text(text)
    return {
        "source": "telegram",
        "source_type": "telegram",
        "channel_name": channel,
        "id": (message.get("data-post") or "").strip(),
        "title": title[:180],
        "url": url,
        "salary": "",
        "description": text,
        "published_at": published_at,
    }


def _title_from_text(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip()
        letters = [char for char in cleaned if char.isalpha()]
        if len(letters) < 4:
            continue
        if cleaned.startswith("#") and " " not in cleaned:
            continue
        return cleaned[:180]
    compact = " ".join(text.split())
    return compact[:180]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Telegram дата не разобрана: %s", value)
        return None
    return parsed
