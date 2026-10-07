"""CLI-обёртка одного прогона vacancy-hunter."""

from __future__ import annotations

import argparse
import logging

from rich.console import Console
from rich.table import Table

from vacancy_hunter.core import TOP_N, gather, open_store_if_exists, run_search, split_duplicates, unseen
from vacancy_hunter.report import duplicate_note, relevance_label

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    if args.stats:
        collected, passed, rejected = gather()
        store = open_store_if_exists()
        new_items = unseen(passed, store)
        uniques, duplicates = split_duplicates(new_items, store)
        _print_stats(collected, rejected, passed, uniques, duplicates)
        by_url = {item["url"]: item for item in duplicates}
        shown = passed if args.all else passed[:TOP_N]
        _print_vacancies([by_url.get(item["url"], item) for item in shown], "Прошедшие фильтр")
        if duplicates:
            _print_vacancies(duplicates, "Повторы")
        return

    result = run_search(notify=not args.no_notify, show_all=args.all)
    logger.info("Дайджест записан: %s", result.digest_path.name)
    _print_vacancies(result.selected, "Новые вакансии")


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
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="записать файл и базу без отправки текста в Telegram",
    )
    return parser.parse_args()


def _print_vacancies(items: list[dict], title: str) -> None:
    table = Table(title=title)
    table.add_column("Уровень")
    table.add_column("Источник")
    table.add_column("Канал")
    table.add_column("Совпадения")
    table.add_column("Название")
    table.add_column("Оценка", justify="right")
    table.add_column("Повтор")
    for item in items:
        words = ", ".join(item.get("matched_whitelist_words") or [])
        repeat = duplicate_note(item) if item.get("duplicate_of") else ""
        table.add_row(
            relevance_label(int(item.get("score") or 0)),
            str(item.get("source_type") or item.get("source") or ""),
            str(item.get("channel_name") or "—"),
            words,
            str(item.get("title")),
            str(item.get("score")),
            repeat,
        )
    Console().print(table)


def _print_stats(
    collected: dict[str, list[dict]],
    rejected: int,
    passed: list[dict],
    new_items: list[dict],
    duplicates: list[dict],
) -> None:
    table = Table(title="Сводка")
    table.add_column("Показатель")
    table.add_column("Значение", justify="right")
    for source, items in collected.items():
        table.add_row(f"Получено: {source}", str(len(items)))
    table.add_row("Отклонено фильтром", str(rejected))
    table.add_row("Прошли фильтр", str(len(passed)))
    table.add_row("Уже в базе", str(len(passed) - len(new_items) - len(duplicates)))
    table.add_row("Новые", str(len(new_items)))
    table.add_row("Дубликаты", str(len(duplicates)))
    Console().print(table)


if __name__ == "__main__":
    main()
