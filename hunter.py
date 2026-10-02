"""Один проход сбора, фильтрации, дайджеста и отправки в Telegram."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

from config import get_settings
from vacancy_hunter.database import SeenStore, default_db_path
from vacancy_hunter.filter_and_score import score_text
from vacancy_hunter.habr_parser import fetch_habr
from vacancy_hunter.hh_parser import fetch_hh
from vacancy_hunter.report import build_stats, relevance_label, render_markdown, telegram_blocks
from vacancy_hunter.tg_scraper import fetch_telegram

logger = logging.getLogger(__name__)

TOP_N = 10
TELEGRAM_LIMIT = 3500
ROOT = Path(__file__).resolve().parent


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    settings = get_settings()
    collected = _collect(settings)
    passed, rejected = _apply_filter(collected)
    passed.sort(key=lambda item: (-item["score"], item["title"]))

    if args.stats:
        new_items = _unseen(passed, _open_store_if_exists())
        _print_stats(collected, rejected, passed, new_items)
        shown = passed if args.all else passed[:TOP_N]
        _print_vacancies(shown, "Прошедшие фильтр")
        return

    store = SeenStore()
    new_items = _unseen(passed, store)
    selected = new_items if args.all else new_items[:TOP_N]
    stats = build_stats(collected, rejected, passed, new_items)

    for item in selected:
        store.remember(item["source"], item["url"], item["title"], item["score"])

    digest_path = _write_digest(selected, stats)
    logger.info("Дайджест записан: %s", digest_path.name)
    _send_telegram(settings.telegram_bot_token, settings.chat_id, selected, stats)
    _print_vacancies(selected, "Новые вакансии")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сбор и фильтрация удалённых вакансий")
    parser.add_argument(
        "--all",
        action="store_true",
        help="взять все новые вакансии, прошедшие фильтр, а не только топ",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="показать счётчики и таблицу, без записи в базу, дайджеста и Telegram",
    )
    return parser.parse_args()


def _collect(settings) -> dict[str, list[dict]]:
    return {
        "hh": _safe("hh", lambda: fetch_hh(settings.hh_queries)),
        "telegram": _safe("telegram", lambda: fetch_telegram(settings.tg_channels)),
        "habr": _safe("habr", lambda: fetch_habr(settings.habr_rss)),
    }


def _safe(label: str, func):
    try:
        return func()
    except Exception:
        logger.exception("%s: сбор прерван, источник пропущен", label)
        return []


def _open_store_if_exists() -> SeenStore | None:
    if default_db_path().exists():
        return SeenStore()
    return None


def _unseen(items: list[dict], store: SeenStore | None) -> list[dict]:
    if store is None:
        return list(items)
    return [item for item in items if not store.has(item["source"], item["url"])]


def _apply_filter(collected: dict[str, list[dict]]) -> tuple[list[dict], int]:
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


def _write_digest(items: list[dict], stats) -> Path:
    directory = ROOT / "digests"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"digest_{date.today().isoformat()}.md"
    path.write_text(
        render_markdown(items, stats, day=date.today().isoformat()),
        encoding="utf-8",
    )
    return path


def _send_telegram(token: str, chat_id: str, items: list[dict], stats) -> None:
    if not items:
        logger.info("Новых вакансий нет, Telegram пропущен")
        return
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN или CHAT_ID пуст, отправка пропущена")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in _chunk_messages(telegram_blocks(items, stats)):
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


def _chunk_messages(blocks: list[str]) -> list[str]:
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


def _print_vacancies(items: list[dict], title: str) -> None:
    table = Table(title=title)
    table.add_column("Уровень")
    table.add_column("Источник")
    table.add_column("Канал")
    table.add_column("Совпадения")
    table.add_column("Название")
    table.add_column("Оценка", justify="right")
    for item in items:
        words = ", ".join(item.get("matched_whitelist_words") or [])
        table.add_row(
            relevance_label(int(item.get("score") or 0)),
            str(item.get("source_type") or item.get("source") or ""),
            str(item.get("channel_name") or "—"),
            words,
            str(item.get("title")),
            str(item.get("score")),
        )
    Console().print(table)


def _print_stats(
    collected: dict[str, list[dict]],
    rejected: int,
    passed: list[dict],
    new_items: list[dict],
) -> None:
    table = Table(title="Сводка")
    table.add_column("Показатель")
    table.add_column("Значение", justify="right")
    for source, items in collected.items():
        table.add_row(f"Получено: {source}", str(len(items)))
    table.add_row("Отклонено фильтром", str(rejected))
    table.add_row("Прошли фильтр", str(len(passed)))
    table.add_row("Уже в базе", str(len(passed) - len(new_items)))
    table.add_row("Новые", str(len(new_items)))
    Console().print(table)


if __name__ == "__main__":
    main()
