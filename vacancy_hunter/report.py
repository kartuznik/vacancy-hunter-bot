"""Единый текст дайджеста для файла и Telegram."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape


SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("hh", "🟢 HH.ru"),
    ("habr", "🔵 Habr Career"),
    ("telegram", "🟣 Telegram"),
)


@dataclass(frozen=True)
class DigestStats:
    collected_hh: int
    collected_habr: int
    collected_telegram: int
    rejected: int
    passed: int
    new_count: int
    top: tuple[dict, ...]

    @property
    def collected_total(self) -> int:
        return self.collected_hh + self.collected_habr + self.collected_telegram


def relevance_label(score: int) -> str:
    if score > 100:
        return "🔥 HIGH"
    if score >= 50:
        return "⚡ MEDIUM"
    return "📄 LOW"


def build_stats(
    collected: dict[str, list],
    rejected: int,
    passed: list[dict],
    new_items: list[dict],
) -> DigestStats:
    ranked = sorted(passed, key=_sort_key)
    return DigestStats(
        collected_hh=len(collected.get("hh") or []),
        collected_habr=len(collected.get("habr") or []),
        collected_telegram=len(collected.get("telegram") or []),
        rejected=rejected,
        passed=len(passed),
        new_count=len(new_items),
        top=tuple(ranked[:3]),
    )


def render_markdown(items: list[dict], stats: DigestStats, *, day: str) -> str:
    lines = [f"# Дайджест вакансий {day}", ""]
    if not items:
        lines.extend(["Новых вакансий нет.", ""])
    for title, source, payload in _sections(items):
        lines.extend([f"## {title}", ""])
        if source == "telegram":
            for channel, rows in payload:
                lines.extend([f"### {channel}", ""])
                for item in rows:
                    lines.extend(_markdown_card(item, "####"))
        else:
            for item in payload:
                lines.extend(_markdown_card(item, "###"))
    lines.extend(_markdown_summary(stats))
    return "\n".join(lines).rstrip() + "\n"


def telegram_blocks(items: list[dict], stats: DigestStats) -> list[str]:
    blocks: list[str] = []
    for title, source, payload in _sections(items):
        blocks.append(f"<b>{escape(title)}</b>")
        if source == "telegram":
            for channel, rows in payload:
                blocks.append(f"<b>{escape(channel)}</b>")
                blocks.extend(_telegram_card(item) for item in rows)
        else:
            blocks.extend(_telegram_card(item) for item in payload)
    blocks.append(_telegram_summary(stats))
    return blocks


def _sections(items: list[dict]) -> list[tuple]:
    grouped: dict[str, list[dict]] = {key: [] for key, _title in SECTION_ORDER}
    for item in items:
        source = item.get("source_type") or item.get("source")
        if source in grouped:
            grouped[source].append(item)
    sections = []
    for source, title in SECTION_ORDER:
        rows = sorted(grouped[source], key=_sort_key)
        if not rows:
            continue
        if source == "telegram":
            by_channel: dict[str, list[dict]] = {}
            for item in rows:
                channel = (item.get("channel_name") or "").strip() or "без канала"
                by_channel.setdefault(channel, []).append(item)
            channels = [
                (channel, sorted(channel_rows, key=_sort_key))
                for channel, channel_rows in sorted(by_channel.items(), key=lambda pair: pair[0].casefold())
            ]
            sections.append((title, source, channels))
        else:
            sections.append((title, source, rows))
    return sections


def _markdown_card(item: dict, heading: str) -> list[str]:
    title = " ".join((item.get("title") or "").split())
    label = relevance_label(int(item.get("score") or 0))
    salary = item.get("salary") or "не указана"
    excerpt = ((item.get("description") or "").strip())[:500]
    return [
        f"{heading} {label} {title}",
        "",
        f"- Оценка: {item.get('score')}",
        f"- Зарплата: {salary}",
        f"- Ссылка: {item.get('url')}",
        f"- {_matches(item)}",
        "",
        excerpt or "Описание отсутствует.",
        "",
    ]


def _telegram_card(item: dict) -> str:
    title = escape(" ".join((item.get("title") or "").split()))
    label = escape(relevance_label(int(item.get("score") or 0)))
    salary = escape(item.get("salary") or "не указана")
    link = escape(item.get("url") or "")
    matches = escape(_matches(item))
    return f"{label} <b>{title}</b>\nЗарплата: {salary}\nСсылка: {link}\n{matches}"


def _markdown_summary(stats: DigestStats) -> list[str]:
    lines = [
        "## Сводка",
        "",
        f"- Всего собрано: {stats.collected_total}",
        f"- Отсеяно фильтром: {stats.rejected}",
        f"- Прошло фильтр: {stats.passed}",
        f"- Новых: {stats.new_count}",
        f"- HH.ru — {stats.collected_hh}",
        f"- Habr — {stats.collected_habr}",
        f"- Telegram — {stats.collected_telegram}",
        "",
        "### Топ-3 по скору",
        "",
    ]
    if not stats.top:
        lines.append("Нет вакансий, прошедших фильтр.")
        lines.append("")
        return lines
    for index, item in enumerate(stats.top, start=1):
        title = " ".join((item.get("title") or "").split())
        label = relevance_label(int(item.get("score") or 0))
        source = item.get("source_type") or item.get("source") or ""
        lines.append(f"{index}. {label} {title} — {item.get('score')} ({source})")
    lines.append("")
    return lines


def _telegram_summary(stats: DigestStats) -> str:
    lines = [
        "<b>Сводка</b>",
        f"Всего собрано: {stats.collected_total}",
        f"Отсеяно фильтром: {stats.rejected}",
        f"Прошло фильтр: {stats.passed}",
        f"Новых: {stats.new_count}",
        f"HH.ru — {stats.collected_hh}",
        f"Habr — {stats.collected_habr}",
        f"Telegram — {stats.collected_telegram}",
        "",
        "<b>Топ-3 по скору</b>",
    ]
    if not stats.top:
        lines.append("Нет вакансий, прошедших фильтр.")
        return "\n".join(lines)
    for index, item in enumerate(stats.top, start=1):
        title = escape(" ".join((item.get("title") or "").split()))
        label = escape(relevance_label(int(item.get("score") or 0)))
        source = escape(str(item.get("source_type") or item.get("source") or ""))
        lines.append(f"{index}. {label} {title} — {item.get('score')} ({source})")
    return "\n".join(lines)


def _matches(item: dict) -> str:
    words = item.get("matched_whitelist_words") or ()
    return "Совпадения: " + ", ".join(str(word) for word in words)


def _sort_key(item: dict) -> tuple:
    return (-int(item.get("score") or 0), str(item.get("title") or ""))
