"""Один прогон сбора: его вызывают CLI и долгоживущий бот."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import requests

from config import HIDE_DUPLICATES, get_settings
from vacancy_hunter.database import SeenStore, default_db_path, title_key
from vacancy_hunter.filter_and_score import score_text
from vacancy_hunter.habr_parser import fetch_habr
from vacancy_hunter.hh_parser import fetch_hh
from vacancy_hunter.report import build_stats, render_markdown, telegram_blocks
from vacancy_hunter.source_rvc import fetch_rvc
from vacancy_hunter.tg_scraper import fetch_telegram

logger = logging.getLogger(__name__)

TOP_N = 10
TELEGRAM_LIMIT = 3500
SEND_PAUSE_SECONDS = 1.0


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
    uniques, duplicates = split_duplicates(new_items, store)
    selected = uniques if show_all else uniques[:TOP_N]
    selected_keys = {item["title_key"] for item in selected}
    duplicates = [
        item for item in duplicates if item["duplicate_of"][1] is not None or item["title_key"] in selected_keys
    ]
    stats = build_stats(collected, rejected, passed, uniques, duplicates)
    for item in selected:
        store.remember(
            item["source"],
            item["url"],
            item["title"],
            item["score"],
            title_key=item["title_key"],
            original_source=source_label(item),
        )
    for item in duplicates:
        store.remember(
            item["source"],
            item["url"],
            item["title"],
            item["score"],
            title_key=item["title_key"],
            is_duplicate=True,
            original_source=item["duplicate_of"][0],
        )
    shown_duplicates = [] if HIDE_DUPLICATES else duplicates
    digest_path = write_digest(selected, stats, duplicates=shown_duplicates)
    blocks = telegram_blocks(selected, stats, duplicates=shown_duplicates)
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
    sources = {
        "hh": lambda: fetch_hh(settings.hh_queries),
        "telegram": fetch_telegram,
        "habr": lambda: fetch_habr(settings.habr_rss),
        "rvc": fetch_rvc,
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {label: pool.submit(safe, label, func) for label, func in sources.items()}
        return {label: future.result() for label, future in futures.items()}


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


def source_label(item: dict) -> str:
    return str(item.get("channel_name") or item.get("source_type") or item.get("source") or "")


def split_duplicates(items: list[dict], store: SeenStore | None) -> tuple[list[dict], list[dict]]:
    uniques: list[dict] = []
    duplicates: list[dict] = []
    first_in_run: dict[str, str] = {}
    for item in items:
        enriched = dict(item)
        key = title_key(enriched.get("title") or "", enriched.get("description") or "")
        enriched["title_key"] = key
        original = store.find_original(key) if store is not None else None
        if original is not None:
            enriched["duplicate_of"] = original
            duplicates.append(enriched)
        elif key in first_in_run:
            enriched["duplicate_of"] = (first_in_run[key], None)
            duplicates.append(enriched)
        else:
            first_in_run[key] = source_label(enriched)
            uniques.append(enriched)
    return uniques, duplicates


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


def write_digest(
    items: list[dict],
    stats,
    *,
    now: datetime | None = None,
    duplicates: list[dict] | None = None,
) -> Path:
    directory = digests_dir()
    directory.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now()
    stem = f"digest_{moment:%Y-%m-%d_%H%M}"
    path = directory / f"{stem}.md"
    suffix = 2
    while path.exists():
        path = directory / f"{stem}_{suffix}.md"
        suffix += 1
    text = render_markdown(items, stats, day=moment.date().isoformat(), duplicates=duplicates or [])
    path.write_text(text, encoding="utf-8")
    return path


def send_telegram(token: str, chat_id: str, items: list[dict], blocks: list[str]) -> None:
    if not items:
        logger.info("Новых вакансий нет, Telegram пропущен")
        return
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN или CHAT_ID пуст, отправка пропущена")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    for index, chunk in enumerate(chunk_messages(blocks)):
        if index:
            time.sleep(SEND_PAUSE_SECONDS)
        if not _post_chunk(url, {**payload, "text": chunk}):
            return


def _post_chunk(url: str, data: dict) -> bool:
    for attempt in range(2):
        try:
            response = requests.post(url, data=data, timeout=10)
        except requests.RequestException as exc:
            logger.warning("Telegram не принял сообщение: %s", type(exc).__name__)
            return False
        if response.ok:
            return True
        if response.status_code == 429 and attempt == 0:
            delay = _retry_after(response)
            logger.warning("Telegram ответил 429, повтор через %s с", delay)
            time.sleep(delay)
            continue
        logger.warning("Telegram ответил статусом %s", response.status_code)
        return False
    return False


def _retry_after(response) -> int:
    try:
        value = response.json().get("parameters", {}).get("retry_after")
    except ValueError:
        value = None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 5


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
