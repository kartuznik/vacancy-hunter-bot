"""MCP-клиент RVC без сетевых запросов: requests.post подменён."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from vacancy_hunter import core, source_rvc
from vacancy_hunter.report import build_stats, render_markdown, telegram_blocks
from vacancy_hunter.source_rvc import fetch_rvc, map_teaser

TEASER = {
    "masked_id": "acme-python-backend-developer-1",
    "title": "Python Backend Developer",
    "company": "Acme",
    "attributed_url": "https://app.rvc.global/vacancy/view/acme-python-backend-developer-1?utm_source=openai_plugin",
    "work_arrangement": "FULLY_REMOTE",
    "work_arrangement_display": "Worldwide Remote",
    "relative_age": "~2 дня назад",
    "salary": {"display": "USD 2000–3000/month"},
    "skills": ["python", "django", "postgresql"],
}
REJECTED_TEASER = {
    **TEASER,
    "masked_id": "acme-react-developer-2",
    "title": "React Developer",
    "attributed_url": "https://app.rvc.global/vacancy/view/acme-react-developer-2",
    "skills": ["react", "typescript"],
}


class _Response:
    def __init__(self, status_code: int = 200, payload=None, *, sse: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        content_type = "text/event-stream" if sse else "application/json"
        self.headers = {"Content-Type": content_type}
        body = json.dumps(payload, ensure_ascii=False)
        self.text = f"event: message\ndata: {body}\n\n" if sse else body

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _rpc(structured: dict, *, is_error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": "..."}], "structuredContent": structured}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _search(*teasers: dict) -> dict:
    return _rpc({"status": "results", "results": list(teasers)})


def _job(seniority: str | None, published_at: str = "2026-10-08T06:00:00.000Z") -> dict:
    return _rpc(
        {
            "status": "detail",
            "job": {"masked_id": TEASER["masked_id"], "seniority_level": seniority, "published_at": published_at},
        }
    )


def _install(monkeypatch, responses: list) -> list[dict]:
    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(source_rvc.requests, "post", fake_post)
    return calls


def test_map_teaser_builds_internal_vacancy() -> None:
    item = map_teaser(TEASER)
    assert item == {
        "source": "rvc",
        "source_type": "mcp",
        "channel_name": "",
        "id": "acme-python-backend-developer-1",
        "title": "Python Backend Developer",
        "url": TEASER["attributed_url"],
        "salary": "USD 2000–3000/month",
        "description": "Компания: Acme. Формат: Worldwide Remote. Навыки: python, django, postgresql",
        "published_at": None,
    }
    assert map_teaser({**TEASER, "attributed_url": ""}) is None


def test_search_request_uses_mcp_headers_and_arguments(monkeypatch) -> None:
    calls = _install(monkeypatch, [_Response(payload=_search())])
    assert fetch_rvc() == []
    call = calls[0]
    assert call["url"] == "https://app.rvc.global/mcp"
    assert call["timeout"] == 15
    assert call["headers"]["Accept"] == "application/json, text/event-stream"
    assert call["headers"]["MCP-Protocol-Version"] == "2025-06-18"
    assert call["headers"]["User-Agent"]
    assert call["json"]["method"] == "tools/call"
    assert call["json"]["params"] == {
        "name": "rvc_search_jobs",
        "arguments": {
            "query": "Python backend developer",
            "conversation_language_code": "ru",
            "allow_english": True,
            "work_arrangements": ["FULLY_REMOTE"],
            "target_country_codes": [],
            "limit": 10,
        },
    }


def test_sse_response_is_parsed(monkeypatch) -> None:
    _install(
        monkeypatch,
        [_Response(payload=_search(TEASER), sse=True), _Response(payload=_job("MIDDLE"), sse=True)],
    )
    items = fetch_rvc()
    assert [item["id"] for item in items] == [TEASER["masked_id"]]


def test_get_job_only_for_teasers_passing_filter(monkeypatch) -> None:
    calls = _install(
        monkeypatch,
        [_Response(payload=_search(TEASER, REJECTED_TEASER)), _Response(payload=_job("JUNIOR"))],
    )
    items = fetch_rvc()
    tools = [call["json"]["params"]["name"] for call in calls]
    assert tools == ["rvc_search_jobs", "get_job"]
    assert calls[1]["json"]["params"]["arguments"] == {"masked_id": TEASER["masked_id"]}
    by_id = {item["id"]: item for item in items}
    assert by_id[TEASER["masked_id"]]["published_at"] == datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
    assert by_id[TEASER["masked_id"]]["seniority_level"] == "JUNIOR"
    assert by_id[REJECTED_TEASER["masked_id"]]["published_at"] is None


def test_senior_from_get_job_is_dropped(monkeypatch) -> None:
    _install(monkeypatch, [_Response(payload=_search(TEASER)), _Response(payload=_job("SENIOR"))])
    assert fetch_rvc() == []


def test_failed_get_job_keeps_teaser(monkeypatch) -> None:
    _install(monkeypatch, [_Response(payload=_search(TEASER)), requests.Timeout("slow")])
    items = fetch_rvc()
    assert len(items) == 1
    assert items[0]["published_at"] is None


def test_is_error_returns_empty(monkeypatch, caplog) -> None:
    _install(monkeypatch, [_Response(payload=_rpc({}, is_error=True))])
    assert fetch_rvc() == []
    assert "isError" in caplog.text


def test_network_errors_return_empty(monkeypatch) -> None:
    _install(monkeypatch, [requests.ConnectionError("down")])
    assert fetch_rvc() == []
    _install(monkeypatch, [_Response(status_code=500, payload={})])
    assert fetch_rvc() == []


def test_jsonrpc_error_and_wrong_status_return_empty(monkeypatch) -> None:
    error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}}
    _install(monkeypatch, [_Response(payload=error)])
    assert fetch_rvc() == []
    clarification = _rpc({"status": "clarification_required", "results": [TEASER]})
    _install(monkeypatch, [_Response(payload=clarification)])
    assert fetch_rvc() == []
    _install(monkeypatch, [_Response(payload=None)])
    assert fetch_rvc() == []


def test_collect_keeps_other_sources_when_rvc_fails(monkeypatch) -> None:
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(core, "fetch_hh", lambda queries: [{"id": "hh"}])
    monkeypatch.setattr(core, "fetch_telegram", lambda: [{"id": "tg"}])
    monkeypatch.setattr(core, "fetch_habr", lambda url: [{"id": "habr"}])
    monkeypatch.setattr(core, "fetch_rvc", broken)
    settings = type("S", (), {"hh_queries": (), "habr_rss": ""})()
    collected = core.collect(settings)
    assert list(collected) == ["hh", "telegram", "habr", "rvc"]
    assert collected["rvc"] == []
    assert collected["hh"] == [{"id": "hh"}]


def test_rvc_section_and_summary_in_reports() -> None:
    item = {**map_teaser(TEASER), "score": 105, "matched_whitelist_words": ["django"]}
    stats = build_stats({"hh": [], "habr": [], "telegram": [], "rvc": [item]}, 0, [item], [item])
    assert stats.collected_rvc == 1
    assert stats.collected_total == 1
    markdown = render_markdown([item], stats, day="2026-10-08")
    assert "## 🟠 RVC" in markdown
    assert "- RVC — 1" in markdown
    telegram = "\n".join(telegram_blocks([item], stats))
    assert "<b>🟠 RVC</b>" in telegram
    assert "RVC — 1" in telegram
