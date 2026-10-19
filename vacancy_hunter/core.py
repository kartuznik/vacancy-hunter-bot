"""Один прогон сбора: его вызывают CLI и долгоживущий бот."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import requests

from config import HIDE_DUPLICATES, MIN_SALARY_RUB, PARK_DAYS, PARK_FLOOR, PASS_SCORE, get_settings
from vacancy_hunter.database import SeenStore, default_db_path, title_key
from vacancy_hunter.filter_and_score import excluded_grade, score_text, senior_penalty
from vacancy_hunter.salary import parse_salary, salary_below_minimum
from vacancy_hunter.habr_parser import fetch_habr
from vacancy_hunter.hh_parser import fetch_hh
from vacancy_hunter.report import (
    EMPTY_DIGEST_MARKER,
    build_stats,
    render_markdown,
    telegram_card_messages,
    telegram_tail_blocks,
)
from vacancy_hunter.source_rvc import fetch_rvc
from vacancy_hunter.tg_scraper import fetch_telegram

logger = logging.getLogger(__name__)

TOP_N = 10
TELEGRAM_LIMIT = 3500
SEND_PAUSE_SECONDS = 1.0
EXPORT_FIELDS = ("title", "url", "score", "saved", "label", "seen_at", "source")
DIGEST_NAME = re.compile(r"^digest_[0-9_-]+\.md$")
SUMMARY_SOURCES = (("HH.ru", "hh"), ("Habr", "habr"), ("Telegram", "telegram"), ("RVC", "rvc"))


class SearchResult(NamedTuple):
    digest_path: Path
    messages: list[tuple[str, dict | None]]
    selected: list[dict]


def digests_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "digests"


def _digest_files() -> list[Path]:
    directory = digests_dir()
    if not directory.is_dir():
        return []
    files = [path for path in directory.glob("digest_*.md") if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime)


def is_empty_digest(path: Path) -> bool:
    return EMPTY_DIGEST_MARKER in path.read_text(encoding="utf-8")


def latest_digest() -> Path | None:
    for path in reversed(_digest_files()):
        if not is_empty_digest(path):
            return path
    return None


def newest_digest() -> Path | None:
    files = _digest_files()
    return files[-1] if files else None


def digest_by_name(name: str) -> Path | None:
    if not DIGEST_NAME.fullmatch(name or ""):
        return None
    directory = digests_dir().resolve()
    path = (directory / name).resolve()
    if path.parent != directory or not path.is_file():
        return None
    return path


def digest_moment(name: str) -> datetime | None:
    match = re.match(r"^digest_(\d{4}-\d{2}-\d{2})(?:_(\d{4}))?", name)
    if match is None:
        return None
    day, hhmm = match.groups()
    return datetime.strptime(f"{day} {hhmm or '0000'}", "%Y-%m-%d %H%M")


def download_note(latest: Path, newest: Path | None) -> str | None:
    if newest is None or newest == latest:
        return None
    empty_at = digest_moment(newest.name)
    found_at = digest_moment(latest.name)
    if empty_at is None or found_at is None:
        return None
    when = f"{empty_at:%H:%M}" if empty_at.date() == datetime.now().date() else f"{empty_at:%d.%m %H:%M}"
    return f"Последний прогон в {when} был пустой, вот находки от {found_at:%d.%m %H:%M}"


def parse_digest_summary(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")

    def number(label: str) -> int:
        match = re.search(rf"^- {re.escape(label)}: (\d+)", text, re.M)
        return int(match.group(1)) if match else 0

    by_source = {}
    for label, key in SUMMARY_SOURCES:
        match = re.search(rf"^- {re.escape(label)} — (\d+)", text, re.M)
        if match:
            by_source[key] = int(match.group(1))
    moment = digest_moment(path.name)
    return {
        "started_at": moment.astimezone().isoformat() if moment else "",
        "collected_by_source": by_source,
        "passed": number("Прошло фильтр"),
        "filtered": number("Отсеяно фильтром"),
        "new_count": number("Новых"),
        "duplicates_count": number("Дубликаты из других каналов"),
        "digest_file": path.name,
    }


def backfill_runs_from_digests(store: SeenStore) -> int:
    files = sorted(_digest_files(), key=lambda path: (digest_moment(path.name) or datetime.min, path.name))
    if not files or store.recent_runs(1):
        return 0
    return store.backfill_runs([parse_digest_summary(path) for path in files])


def run_search(notify: bool, *, show_all: bool = False) -> SearchResult:
    settings = get_settings()
    store = SeenStore()
    backfill_runs_from_digests(store)
    run_id = store.start_run()
    store.purge_expired_parked()
    collected, passed, rejected, parked = gather()
    store.park(parked, PARK_DAYS)
    for item in passed:
        store.remember_card(item, run_id)
    new_items = unseen(passed, store)
    uniques, duplicates = split_duplicates(new_items, store)
    selected = uniques if show_all else uniques[:TOP_N]
    selected_keys = {item["title_key"] for item in selected}
    duplicates = [
        item for item in duplicates if item["duplicate_of"][1] is not None or item["title_key"] in selected_keys
    ]
    stats = build_stats(collected, rejected, passed, uniques, duplicates, parked=len(parked))
    for item in selected:
        store.remember(
            item["source"],
            item["url"],
            item["title"],
            item["score"],
            title_key=item["title_key"],
            original_source=source_label(item),
            run_id=run_id,
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
            run_id=run_id,
        )
    shown_duplicates = [] if HIDE_DUPLICATES else duplicates
    digest_path = write_digest(selected, stats, duplicates=shown_duplicates)
    messages = telegram_messages(selected, stats, shown_duplicates)
    if notify:
        send_telegram(settings.telegram_bot_token, settings.chat_id, selected, messages)
    store.finish_run(
        run_id,
        collected_by_source={source: len(items) for source, items in collected.items()},
        passed=len(passed),
        filtered=rejected,
        new_count=len(uniques),
        duplicates_count=len(duplicates),
        digest_file=digest_path.name,
    )
    return SearchResult(digest_path, messages, selected)


def telegram_messages(items: list[dict], stats, duplicates: list[dict]) -> list[tuple[str, dict | None]]:
    messages: list[tuple[str, dict | None]] = list(telegram_card_messages(items))
    messages.extend((chunk, None) for chunk in chunk_messages(telegram_tail_blocks(stats, duplicates)))
    return messages


def gather() -> tuple[dict[str, list[dict]], list[dict], int, list[dict]]:
    settings = get_settings()
    collected = collect(settings)
    store = open_store_if_exists()
    patterns = store.blacklist_patterns() if store is not None else []
    passed, rejected, parked = apply_filter(collected, patterns)
    passed.sort(key=lambda item: (-item["score"], item["title"]))
    return collected, passed, rejected, parked


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


def apply_filter(
    collected: dict[str, list[dict]], blacklist: list[str] | tuple[str, ...] = ()
) -> tuple[list[dict], int, list[dict]]:
    passed: list[dict] = []
    parked: list[dict] = []
    rejected = 0
    for items in collected.values():
        for item in items:
            verdict, enriched = evaluate(item, blacklist)
            if verdict == "pass":
                passed.append(enriched)
            elif verdict == "park":
                parked.append(enriched)
            else:
                rejected += 1
    return passed, rejected, parked


def evaluate(item: dict, blacklist: list[str] | tuple[str, ...] = ()) -> tuple[str, dict | None]:
    """Гейты по порядку: ссылка, блэклист, грейд, зарплата, скоринг, штраф senior, порог."""
    if not item.get("url"):
        return "reject", None
    if blacklist_match(item, blacklist) is not None:
        return "reject", None
    title = item.get("title") or ""
    if excluded_grade(title) is not None:
        return "reject", None
    salary = parse_salary(item)
    if salary_below_minimum(salary, MIN_SALARY_RUB):
        return "reject", None
    text = "\n".join(
        part for part in (title, item.get("description") or "", item.get("salary") or "") if part
    )
    scored = score_text(text, item.get("published_at"))
    if scored is None:
        return "reject", None
    score = scored.score - senior_penalty(title)
    if score < PARK_FLOOR:
        return "reject", None
    enriched = dict(item)
    enriched["score"] = score
    enriched["source_type"] = enriched.get("source_type") or enriched.get("source")
    enriched["channel_name"] = enriched.get("channel_name") or ""
    enriched["company"] = enriched.get("company") or ""
    enriched["matched_whitelist_words"] = list(scored.whitelist_hits)
    if salary is not None and salary.from_text:
        enriched["salary"] = salary.text
        enriched["salary_from_text"] = True
    return ("pass" if score >= PASS_SCORE else "park"), enriched


def blacklist_match(item: dict, patterns: list[str] | tuple[str, ...]) -> str | None:
    fields = [str(item.get(name) or "").casefold() for name in ("channel_name", "company")]
    for pattern in patterns:
        needle = pattern.casefold().strip()
        if needle and any(needle in field for field in fields):
            return pattern
    return None


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


def send_telegram(
    token: str,
    chat_id: str,
    items: list[dict],
    messages: list[tuple[str, dict | None]],
) -> None:
    if not items:
        logger.info("Новых вакансий нет, Telegram пропущен")
        return
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN или CHAT_ID пуст, отправка пропущена")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    for index, (text, markup) in enumerate(messages):
        if index:
            time.sleep(SEND_PAUSE_SECONDS)
        data = {**payload, "text": text}
        if markup is not None:
            data["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        if not _post_chunk(url, data):
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


def exports_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "exports"


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def export_csv(store: SeenStore, days: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    for row in store.export_rows(_since(days)):
        writer.writerow(
            {
                "title": row["title"],
                "url": row["url"],
                "score": row["score"],
                "saved": 1 if row["saved_at"] else 0,
                "label": row["label"] or "",
                "seen_at": row["seen_at"],
                "source": row["source"],
            }
        )
    return buffer.getvalue().encode("utf-8-sig")


def export_records(store: SeenStore, days: int) -> list[dict]:
    records = []
    for row in store.export_rows(_since(days)):
        record = dict(row)
        record["matched_words"] = json.loads(row["matched_words"]) if row["matched_words"] else []
        record["has_card"] = bool(row["has_card"])
        record["saved"] = row["saved_at"] is not None
        records.append(record)
    return records


def write_export_jsonl(days: int, store: SeenStore | None = None) -> Path:
    store = store or SeenStore()
    directory = exports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"vacancies_{datetime.now():%Y-%m-%d}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in export_records(store, days):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
