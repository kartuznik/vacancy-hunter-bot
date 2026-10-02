"""Один прогон сбора: его вызывают CLI и долгоживущий бот."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import NamedTuple

import requests

from config import get_settings
from vacancy_hunter.database import SeenStore, default_db_path
from vacancy_hunter.filter_and_score import score_text
from vacancy_hunter.habr_parser import fetch_habr
from vacancy_hunter.hh_parser import fetch_hh
from vacancy_hunter.report import build_stats, render_markdown, telegram_blocks
from vacancy_hunter.tg_scraper import fetch_telegram

logger = logging.getLogger(__name__)

TOP_N = 10
TELEGRAM_LIMIT = 3500


class SearchResult(NamedTuple):
    digest_path: Path
    blocks: list[str]
    selected: list[dict]


def digests_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "digests"


def latest_digest() -> Path | None:
    directory = digests_dir()
    if not directory.is_dir():
        return None
    files = [path for path in directory.glob("digest_*.md") if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def run_search(notify: bool, *, show_all: bool = False) -> SearchResult:
    settings = get_settings()
    collected, passed, rejected = gather()
    store = SeenStore()
    new_items = unseen(passed, store)
    selected = new_items if show_all else new_items[:TOP_N]
    stats = build_stats(collected, rejected, passed, new_items)
    for item in selected:
        store.remember(item["source"], item["url"], item["title"], item["score"])
    digest_path = write_digest(selected, stats)
    blocks = telegram_blocks(selected, stats)
    if notify:
        send_telegram(settings.telegram_bot_token, settings.chat_id, selected, blocks)
    return SearchResult(digest_path, blocks, selected)


def gather() -> tuple[dict[str, list[dict]], list[dict], int]:
    settings = get_settings()
    collected = collect(settings)
    passed, rejected = apply_filter(collected)
    passed.sort(key=lambda item: (-item["score"], item["title"]))
    return collected, passed, rejected


def collect(settings) -> dict[str, list[dict]]:
    return {
        "hh": safe("hh", lambda: fetch_hh(settings.hh_queries)),
        "telegram": safe("telegram", fetch_telegram),
        "habr": safe("habr", lambda: fetch_habr(settings.habr_rss)),
    }


def safe(label: str, func):
    try:
        return func()
    except Exception:
        logger.exception("%s: сбор прерван, источник пропущен", label)
        return []


def open_store_if_exists() -> SeenStore | None:
    if default_db_path().exists():
        return SeenStore()
    return None


def unseen(items: list[dict], store: SeenStore | None) -> list[dict]:
    if store is None:
        return list(items)
    return [item for item in items if not store.has(item["source"], item["url"])]


def apply_filter(collected: dict[str, list[dict]]) -> tuple[list[dict], int]:
    passed: list[dict] = []
    rejected = 0
    for items in collected.values():
        for item in items:
            if not item.get("url"):
                rejected += 1
                continue
            text = "\n".join(
                part
                for part in (item.get("title") or "", item.get("description") or "", item.get("salary") or "")
                if part
            )
            scored = score_text(text, item.get("published_at"))
            if scored is None:
                rejected += 1
                continue
            enriched = dict(item)
            enriched["score"] = scored.score
            enriched["source_type"] = enriched.get("source_type") or enriched.get("source")
            enriched["channel_name"] = enriched.get("channel_name") or ""
            enriched["matched_whitelist_words"] = list(scored.whitelist_hits)
            passed.append(enriched)
    return passed, rejected


def write_digest(items: list[dict], stats) -> Path:
    directory = digests_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"digest_{date.today().isoformat()}.md"
    path.write_text(render_markdown(items, stats, day=date.today().isoformat()), encoding="utf-8")
    return path


def send_telegram(token: str, chat_id: str, items: list[dict], blocks: list[str]) -> None:
    if not items:
        logger.info("Новых вакансий нет, Telegram пропущен")
        return
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN или CHAT_ID пуст, отправка пропущена")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in chunk_messages(blocks):
        try:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.warning("Telegram не принял сообщение: %s", exc)
            return
        if not response.ok:
            logger.warning("Telegram ответил статусом %s", response.status_code)
            return


def chunk_messages(blocks: list[str]) -> list[str]:
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if current and len(candidate) > TELEGRAM_LIMIT:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
