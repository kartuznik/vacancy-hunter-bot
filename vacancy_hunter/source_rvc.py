"""Публичный MCP-сервер RVC (Streamable HTTP, без ключей). Только чтение."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from itertools import count

import requests

from config import USER_AGENT
from vacancy_hunter.filter_and_score import score_text

logger = logging.getLogger(__name__)

MCP_URL = "https://app.rvc.global/mcp"
PROTOCOL_VERSION = "2025-06-18"
REQUEST_TIMEOUT = 15
SEARCH_ARGUMENTS = {
    "query": "Python backend developer",
    "conversation_language_code": "ru",
    "allow_english": True,
    "work_arrangements": ["FULLY_REMOTE"],
    "target_country_codes": [],
    "limit": 10,
}

_request_ids = count(1)


def fetch_rvc() -> list[dict]:
    search = _call_tool("rvc_search_jobs", SEARCH_ARGUMENTS, expected_status="results")
    if search is None:
        return []
    vacancies: list[dict] = []
    for teaser in search.get("results") or []:
        item = map_teaser(teaser)
        if item is None:
            continue
        # get_job сервер разрешает только точечно: зовём его лишь для прошедших фильтр.
        if score_text(_filter_text(item), item["published_at"]) is None:
            vacancies.append(item)
            continue
        detail = _call_tool("get_job", {"masked_id": item["id"]}, expected_status="detail")
        job = (detail or {}).get("job") or {}
        if str(job.get("seniority_level") or "").upper() == "SENIOR":
            logger.info("RVC: %s отброшена, seniority_level=SENIOR", item["id"])
            continue
        vacancies.append(apply_job_detail(item, job))
    return vacancies


def map_teaser(teaser: dict) -> dict | None:
    masked_id = str(teaser.get("masked_id") or "").strip()
    title = " ".join(str(teaser.get("title") or "").split())
    url = str(teaser.get("attributed_url") or "").strip()
    if not masked_id or not title or not url:
        return None
    parts = []
    if teaser.get("company"):
        parts.append(f"Компания: {teaser['company']}")
    if teaser.get("work_arrangement_display"):
        parts.append(f"Формат: {teaser['work_arrangement_display']}")
    skills = [str(skill) for skill in teaser.get("skills") or [] if skill]
    if skills:
        parts.append("Навыки: " + ", ".join(skills))
    return {
        "source": "rvc",
        "source_type": "mcp",
        "channel_name": "",
        "id": masked_id,
        "title": title,
        "url": url,
        "salary": str((teaser.get("salary") or {}).get("display") or ""),
        "description": ". ".join(parts),
        "published_at": None,
    }


def apply_job_detail(item: dict, job: dict) -> dict:
    enriched = dict(item)
    enriched["published_at"] = _parse_published(job.get("published_at"))
    enriched["seniority_level"] = job.get("seniority_level")
    return enriched


def _filter_text(item: dict) -> str:
    return "\n".join(part for part in (item["title"], item["description"], item["salary"]) if part)


def _call_tool(name: str, arguments: dict, *, expected_status: str) -> dict | None:
    payload = {
        "jsonrpc": "2.0",
        "id": next(_request_ids),
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "User-Agent": USER_AGENT,
    }
    try:
        response = requests.post(MCP_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("RVC %s: сервер недоступен: %s", name, type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.warning("RVC %s: HTTP %s", name, response.status_code)
        return None
    try:
        message = _parse_message(response)
    except ValueError as exc:
        logger.warning("RVC %s: ответ не разобран: %s", name, exc)
        return None
    if message.get("error"):
        logger.warning("RVC %s: ошибка JSON-RPC %s", name, message["error"].get("code"))
        return None
    result = message.get("result") or {}
    if result.get("isError"):
        logger.warning("RVC %s: инструмент вернул isError", name)
        return None
    content = result.get("structuredContent") or {}
    if content.get("status") != expected_status:
        logger.warning("RVC %s: status=%s", name, content.get("status"))
        return None
    return content


def _parse_message(response) -> dict:
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "text/event-stream" in content_type:
        return _parse_sse(response.text)
    try:
        message = response.json()
    except ValueError as exc:
        raise ValueError("не JSON") from exc
    if not isinstance(message, dict):
        raise ValueError("не объект JSON-RPC")
    return message


def _parse_sse(text: str) -> dict:
    last_data = ""
    current: list[str] = []
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current:
                last_data = "\n".join(current)
                current = []
            continue
        if line.startswith("data:"):
            current.append(line[5:].lstrip())
    if not last_data:
        raise ValueError("в SSE нет data")
    try:
        message = json.loads(last_data)
    except json.JSONDecodeError as exc:
        raise ValueError("data в SSE не JSON") from exc
    if not isinstance(message, dict):
        raise ValueError("не объект JSON-RPC")
    return message


def _parse_published(value) -> datetime | None:
    if not value:
        return None
    try:
        published = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("RVC: дата не разобрана")
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published
