"""Журнал оценок и предложения по фильтрам. Сеть и модели не используются.

Одна оценка ничего не меняет. Предложение появляется только после нескольких
разных карточек и применяется после подтверждения владельца.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import MIN_SALARY_RUB, PENALTY_SENIOR
from vacancy_hunter.core import GateSettings, default_gates, evaluate
from vacancy_hunter.database import SeenStore, content_hash
from vacancy_hunter.filter_and_score import excluded_grade, senior_penalty
from vacancy_hunter.salary import parse_salary

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
COMPANY_VOTES = 2
CHANNEL_VOTES = 3
SALARY_VOTES = 3
SENIOR_VOTES = 3
GRADE_VOTES = 2
PATTERN_MIN_CHARS = 4
SALARY_FLOOR = 50_000
SALARY_CEILING = 400_000
PENALTY_FLOOR = 0
PENALTY_CEILING = 60
PENALTY_STEP = 10
OVERRIDE_KEYS = ("MIN_SALARY_RUB", "PENALTY_SENIOR", "EXCLUDED_GRADES_EXTRA")
GRADE_DICTIONARY = ("architect", "архитектор", "staff")
REASONS = {
    "c": "спам-компания или канал",
    "s": "низкая зарплата",
    "g": "не тот грейд",
    "t": "не мой стек",
    "d": "дубль или уже видел",
    "n": "без причины",
}
VERDICT_LABELS = {"relevant": "релевантно", "junk": "мусор", "favorite": "избранное"}
POSITIVE = frozenset({"relevant", "favorite"})
STOP_WORDS = frozenset(
    {
        "ооо",
        "ooo",
        "зао",
        "пао",
        "ао",
        "ип",
        "ltd",
        "llc",
        "inc",
        "gmbh",
        "corp",
        "group",
        "groups",
        "it",
        "hr",
        "the",
        "company",
        "компания",
    }
)
_TOKEN = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True)
class Proposal:
    kind: str
    payload: dict
    evidence: list[int]


def pattern_allowed(pattern: str) -> bool:
    folded = _fold(pattern)
    if len(folded) < PATTERN_MIN_CHARS or folded in STOP_WORDS:
        return False
    tokens = [token for token in _TOKEN.split(folded) if token]
    return bool(tokens) and not all(token in STOP_WORDS for token in tokens)


def record_verdict(store: SeenStore, digest: str, verdict: str, *, now: datetime | None = None) -> int | None:
    card = store.card_snapshot(digest)
    if card is None:
        return None
    item = card_to_item(card)
    salary = parse_salary(item)
    return store.add_feedback_event(
        content_hash=digest,
        verdict=verdict,
        reason=None,
        source=str(card.get("source") or ""),
        channel_name=str(card.get("channel_name") or ""),
        company=str(card.get("company") or ""),
        salary_ceiling=salary.ceiling if salary is not None else None,
        title=str(card.get("title") or ""),
        created_at=_stamp(now),
    )


def assign_reason(store: SeenStore, prefix: str, code: str, *, now: datetime | None = None) -> str:
    if code not in REASONS:
        return "Неизвестная причина."
    digest = store.resolve_hash(prefix)
    if digest is None:
        return "Карточка не найдена в базе."
    event = store.latest_junk_without_reason(digest) or store.latest_active_junk(digest)
    if event is None:
        event_id = record_verdict(store, digest, "junk", now=now)
        if event_id is None:
            return "Карточка не найдена в базе."
    else:
        event_id = int(event["id"])
    store.set_event_reason(event_id, code)
    store.set_label(digest, "junk")
    return f"Причина сохранена: {REASONS[code]}."


def undo_latest_junk(store: SeenStore, prefix: str, *, now: datetime | None = None) -> str:
    digest = store.resolve_hash(prefix)
    if digest is None:
        return "Карточка не найдена в базе."
    event = store.latest_active_junk(digest)
    if event is None:
        return "Оценку не нашёл."
    return undo_event(store, int(event["id"]), now=now)


def undo_event(store: SeenStore, event_id: int, *, now: datetime | None = None) -> str:
    event = store.feedback_event(event_id)
    if event is None:
        return "Оценка не найдена."
    if event["undone_at"]:
        return "Оценка уже отменена."
    store.mark_feedback_undone(event_id, _stamp(now))
    verdict = str(event["verdict"])
    digest = str(event["content_hash"])
    if verdict in {"junk", "relevant"} and store.active_verdict_count(digest, verdict) == 0:
        store.clear_label(digest, verdict)
    return "Оценка отменена."


def effective_gates(store: SeenStore | None) -> GateSettings:
    base = default_gates()
    if store is None:
        return base
    rows = store.override_map()
    salary = _bounded_int(rows.get("MIN_SALARY_RUB"), SALARY_FLOOR, SALARY_CEILING, base.min_salary_rub)
    penalty = _bounded_int(rows.get("PENALTY_SENIOR"), PENALTY_FLOOR, PENALTY_CEILING, base.penalty_senior)
    extra_row = rows.get("EXCLUDED_GRADES_EXTRA")
    extra = _safe_extra(_extra_grades(extra_row["value"]) if extra_row else ())
    grades = tuple(dict.fromkeys([*base.excluded_grades, *extra]))
    return GateSettings(salary, penalty, grades)


def refresh_suggestions(store: SeenStore, *, now: datetime | None = None) -> list[dict]:
    moment = now or datetime.now(timezone.utc)
    votes = _votes(store, moment)
    gates = effective_gates(store)
    proposals = _blacklist_proposals(votes)
    for proposal in (_salary_proposal(votes, gates), _senior_proposal(votes, gates)):
        if proposal is not None:
            proposals.append(proposal)
    proposals.extend(_grade_proposals(votes, gates))
    listed = {item.casefold() for item in store.blacklist_patterns()}
    proposals = [
        proposal
        for proposal in proposals
        if proposal.kind != "blacklist" or str(proposal.payload["pattern"]).casefold() not in listed
    ]
    active = {(proposal.kind, _payload(proposal.payload)) for proposal in proposals}
    for row in store.pending_suggestions():
        if (str(row["kind"]), str(row["payload"])) not in active:
            store.delete_pending(int(row["id"]))
    created: list[dict] = []
    for proposal in proposals:
        if _blocked(store, proposal, moment):
            continue
        suggestion_id = store.insert_suggestion(
            kind=proposal.kind,
            payload=_payload(proposal.payload),
            evidence=json.dumps(proposal.evidence),
            created_at=_stamp(moment),
        )
        logger.info("Новое предложение %s id=%s", proposal.kind, suggestion_id)
        created.append(
            {"id": suggestion_id, "kind": proposal.kind, "payload": proposal.payload, "evidence": proposal.evidence}
        )
    return created


def format_created(store: SeenStore, created: list[dict]) -> str:
    if not created:
        return ""
    lines = [f"Новые предложения по фильтрам: {len(created)}. Подтверждение: /suggestions"]
    for item in created:
        delta = archive_delta(store, Proposal(item["kind"], item["payload"], item["evidence"]))
        lines.append(f"• {describe(item['kind'], item['payload'])}. В архиве отсеет ещё {delta}.")
    return "\n".join(lines)


def pending_views(store: SeenStore, *, now: datetime | None = None) -> list[dict]:
    refresh_suggestions(store, now=now)
    views = []
    for row in store.pending_suggestions():
        payload = json.loads(row["payload"])
        evidence = json.loads(row["evidence"])
        delta = archive_delta(store, Proposal(str(row["kind"]), payload, evidence))
        views.append(
            {
                "id": int(row["id"]),
                "text": (
                    f"{describe(str(row['kind']), payload)}\n"
                    f"Свидетельств: {len(evidence)} за {WINDOW_DAYS} дней.\n"
                    f"В текущем архиве отсеет ещё {delta} карточек."
                ),
            }
        )
    return views


def feedback_report(store: SeenStore, *, now: datetime | None = None) -> str:
    votes = _votes(store, now or datetime.now(timezone.utc))
    if not votes:
        return f"За {WINDOW_DAYS} дней оценок нет."
    counts = {name: 0 for name in VERDICT_LABELS}
    reasons: dict[str, int] = {}
    sources: dict[str, int] = {}
    for event in votes:
        counts[str(event["verdict"])] = counts.get(str(event["verdict"]), 0) + 1
        if event["verdict"] != "junk":
            continue
        if event["reason"]:
            reasons[str(event["reason"])] = reasons.get(str(event["reason"]), 0) + 1
        source = _source_name(event)
        if source:
            sources[source] = sources.get(source, 0) + 1
    lines = [
        f"Обратная связь за {WINDOW_DAYS} дней",
        (
            f"Карточек: {len(votes)} (релевантно {counts['relevant']}, "
            f"мусор {counts['junk']}, избранное {counts['favorite']})"
        ),
    ]
    if reasons:
        lines.append("Причины мусора:")
        for code, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"• {REASONS.get(code, code)} — {count}")
    if sources:
        lines.append("Компании и каналы по мусору:")
        for name, count in sorted(sources.items(), key=lambda item: (-item[1], item[0].casefold()))[:5]:
            lines.append(f"• {name} — {count}")
    return "\n".join(lines)


def feedback_history(store: SeenStore, limit: int) -> tuple[str, list[int]]:
    rows = store.feedback_recent(limit)
    if not rows:
        return "Оценок пока нет.", []
    lines = [f"Последние оценки ({len(rows)}):"]
    active: list[int] = []
    for event in rows:
        when = _aware(str(event["created_at"])).astimezone()
        verdict = VERDICT_LABELS.get(str(event["verdict"]), str(event["verdict"]))
        reason = f", {REASONS[event['reason']]}" if event["reason"] in REASONS else ""
        mark = "отменено, " if event["undone_at"] else ""
        title = " ".join(str(event["title"] or "").split()) or "без названия"
        lines.append(f"#{event['id']} {when:%d.%m %H:%M} — {mark}{verdict}{reason} — {title}")
        if not event["undone_at"]:
            active.append(int(event["id"]))
    return "\n".join(lines), active


def accept_suggestion(store: SeenStore, suggestion_id: int, *, now: datetime | None = None) -> str:
    row = store.suggestion(suggestion_id)
    if row is None:
        return "Предложение не найдено."
    if row["status"] != "pending":
        return "Предложение уже обработано."
    payload = json.loads(row["payload"])
    moment = now or datetime.now(timezone.utc)
    kind = str(row["kind"])
    if kind == "blacklist":
        status = store.blacklist_add(str(payload["pattern"]), suggestion_id=suggestion_id)
        store.set_suggestion_status(suggestion_id, "accepted", _stamp(moment))
        if status == "exists":
            return f"Уже в блэклисте: {payload['pattern']}. Предложение закрыто."
        return f"Применено: {describe(kind, payload)}."
    if kind == "excluded_grade":
        word = str(payload["word"])
        gates = effective_gates(store)
        if word.casefold() in {item.casefold() for item in gates.excluded_grades}:
            store.set_suggestion_status(suggestion_id, "accepted", _stamp(moment))
            return "Такой грейд уже исключён. Предложение закрыто."
        if word.casefold() not in {item.casefold() for item in GRADE_DICTIONARY}:
            return "Грейд вне словаря, предложение не применено."
        current = _current_extra(store)
        store.upsert_override(
            key="EXCLUDED_GRADES_EXTRA",
            value=json.dumps([*current, word], ensure_ascii=False),
            suggestion_id=suggestion_id,
            previous_value=json.dumps(list(current), ensure_ascii=False),
            applied_at=_stamp(moment),
        )
    elif kind == "min_salary":
        value = int(payload["value"])
        if not SALARY_FLOOR <= value <= SALARY_CEILING:
            return "Значение вне допустимых границ, предложение не применено."
        _write_number(store, "MIN_SALARY_RUB", value, suggestion_id, effective_gates(store).min_salary_rub, moment)
    elif kind == "penalty_senior":
        value = int(payload["value"])
        if not PENALTY_FLOOR <= value <= PENALTY_CEILING:
            return "Значение вне допустимых границ, предложение не применено."
        _write_number(store, "PENALTY_SENIOR", value, suggestion_id, effective_gates(store).penalty_senior, moment)
    else:
        return "Неизвестное предложение."
    store.set_suggestion_status(suggestion_id, "accepted", _stamp(moment))
    return f"Применено: {describe(kind, payload)}."


def decline_suggestion(store: SeenStore, suggestion_id: int, *, now: datetime | None = None) -> str:
    row = store.suggestion(suggestion_id)
    if row is None:
        return "Предложение не найдено."
    if row["status"] != "pending":
        return "Предложение уже обработано."
    store.set_suggestion_status(suggestion_id, "declined", _stamp(now))
    return "Предложение отклонено. Повтор возможен через 30 дней или когда свидетельств станет вдвое больше."


def overrides_report(store: SeenStore) -> str:
    rows = store.override_map()
    if not rows:
        return "Подмен настроек нет, действуют значения из config.py."
    defaults = {
        "MIN_SALARY_RUB": str(MIN_SALARY_RUB),
        "PENALTY_SENIOR": str(PENALTY_SENIOR),
        "EXCLUDED_GRADES_EXTRA": "пусто",
    }
    lines = ["Подмены настроек:"]
    for key in OVERRIDE_KEYS:
        row = rows.get(key)
        if row is None:
            continue
        shown = row["value"]
        if key == "EXCLUDED_GRADES_EXTRA":
            shown = ", ".join(_extra_grades(row["value"])) or "пусто"
        when = _aware(str(row["applied_at"])).astimezone()
        lines.append(f"• {key} = {shown} (в config.py: {defaults[key]}), с {when:%d.%m %H:%M}")
    lines.append("Откат: /override_reset КЛЮЧ")
    return "\n".join(lines)


def reset_override(store: SeenStore, raw_key: str, *, now: datetime | None = None) -> str:
    key = raw_key.strip().upper()
    if key not in OVERRIDE_KEYS:
        return "Ключ не найден. Доступны: MIN_SALARY_RUB, PENALTY_SENIOR, EXCLUDED_GRADES_EXTRA."
    row = store.delete_override(key)
    if row is None:
        return f"Подмены {key} нет."
    if row["suggestion_id"]:
        store.set_suggestion_status(int(row["suggestion_id"]), "reverted", _stamp(now))
    return f"{key} снова берётся из config.py."


def reset_journal(store: SeenStore) -> str:
    events, suggestions = store.reset_feedback_journal()
    return (
        f"Журнал очищен: оценок {events}, предложений {suggestions}. "
        "Применённые блэклист и настройки на месте."
    )


def describe(kind: str, payload: dict) -> str:
    if kind == "blacklist":
        return f"Блэклист: «{payload['pattern']}»"
    if kind == "min_salary":
        return f"MIN_SALARY_RUB: {payload['value']}"
    if kind == "penalty_senior":
        return f"PENALTY_SENIOR: {payload['value']}"
    return f"Исключить грейд: {payload['word']}"


def archive_delta(store: SeenStore, proposal: Proposal) -> int:
    gates = effective_gates(store)
    patterns = store.blacklist_patterns()
    changed_gates, changed_patterns = _with_proposal(gates, patterns, proposal)
    extra = 0
    for card in store.all_cards():
        item = card_to_item(card)
        before, _ = evaluate(item, patterns, gates)
        after, _ = evaluate(item, changed_patterns, changed_gates)
        if before != "reject" and after == "reject":
            extra += 1
    return extra


def card_to_item(card: dict) -> dict:
    return {
        "source": card.get("source") or "",
        "url": card.get("url") or "",
        "title": card.get("title") or "",
        "description": card.get("description") or "",
        "salary": card.get("salary") or "",
        "channel_name": card.get("channel_name") or "",
        "company": card.get("company") or "",
        "published_at": _published(card.get("published_at")),
    }


def _votes(store: SeenStore, now: datetime) -> list[dict]:
    since = (now - timedelta(days=WINDOW_DAYS)).isoformat()
    latest: dict[str, dict] = {}
    for event in store.feedback_since(since):
        if event["undone_at"]:
            continue
        latest[str(event["content_hash"])] = event
    return list(latest.values())


def _blacklist_proposals(votes: list[dict]) -> list[Proposal]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for event in votes:
        target = _target(event)
        if target is None:
            continue
        grouped.setdefault(target, []).append(event)
    proposals = []
    for (kind, key), events in grouped.items():
        threshold = COMPANY_VOTES if kind == "company" else CHANNEL_VOTES
        spam = [event for event in events if event["verdict"] == "junk" and event["reason"] == "c"]
        if len(spam) < threshold or _has_positive(events) or not _ratio_ok(events):
            continue
        pattern = _spelling(spam)
        if not pattern_allowed(pattern):
            continue
        proposals.append(Proposal("blacklist", {"pattern": pattern}, [int(event["id"]) for event in spam]))
    return proposals


def _salary_proposal(votes: list[dict], gates: GateSettings) -> Proposal | None:
    if gates.min_salary_rub <= 0:
        return None
    lows = [
        event
        for event in votes
        if event["verdict"] == "junk" and event["reason"] == "s" and event["salary_ceiling"] is not None
    ]
    if len(lows) < SALARY_VOTES:
        return None
    proposed = max(int(event["salary_ceiling"]) for event in lows)
    proposed = min(proposed, gates.min_salary_rub + gates.min_salary_rub // 4, SALARY_CEILING)
    if proposed < SALARY_FLOOR or proposed <= gates.min_salary_rub:
        return None
    liked = [int(event["salary_ceiling"]) for event in votes if event["verdict"] in POSITIVE and event["salary_ceiling"] is not None]
    if liked and proposed > min(liked):
        return None
    return Proposal("min_salary", {"value": proposed}, [int(event["id"]) for event in lows])


def _senior_proposal(votes: list[dict], gates: GateSettings) -> Proposal | None:
    junk = [event for event in votes if event["verdict"] == "junk" and event["reason"] == "g" and _senior(event)]
    if len(junk) < SENIOR_VOTES or gates.penalty_senior >= PENALTY_CEILING:
        return None
    liked = [event for event in votes if event["verdict"] in POSITIVE and _senior(event)]
    if len(liked) >= len(junk):
        return None
    proposed = min(PENALTY_CEILING, gates.penalty_senior + PENALTY_STEP)
    return Proposal("penalty_senior", {"value": proposed}, [int(event["id"]) for event in junk])


def _grade_proposals(votes: list[dict], gates: GateSettings) -> list[Proposal]:
    existing = {item.casefold() for item in gates.excluded_grades}
    wrong = [event for event in votes if event["verdict"] == "junk" and event["reason"] == "g" and not _senior(event)]
    proposals = []
    for word in GRADE_DICTIONARY:
        if word.casefold() in existing:
            continue
        hits = [event for event in wrong if excluded_grade(str(event["title"] or ""), (word,)) is not None]
        if len(hits) < GRADE_VOTES:
            continue
        liked = [
            event
            for event in votes
            if event["verdict"] in POSITIVE and excluded_grade(str(event["title"] or ""), (word,)) is not None
        ]
        if liked:
            continue
        proposals.append(Proposal("excluded_grade", {"word": word}, [int(event["id"]) for event in hits]))
    return proposals


def _blocked(store: SeenStore, proposal: Proposal, now: datetime) -> bool:
    payload = _payload(proposal.payload)
    if proposal.kind == "blacklist":
        current = {item.casefold() for item in store.blacklist_patterns()}
        if str(proposal.payload["pattern"]).casefold() in current:
            return True
    pending = store.latest_suggestion(proposal.kind, payload, "pending")
    if pending is not None:
        evidence = json.dumps(proposal.evidence)
        if pending["evidence"] != evidence:
            store.update_suggestion_evidence(int(pending["id"]), evidence)
        return True
    declined = store.latest_suggestion(proposal.kind, payload, "declined")
    if declined is None or not declined["decided_at"]:
        return False
    decided = _aware(str(declined["decided_at"]))
    if decided < now - timedelta(days=WINDOW_DAYS):
        return False
    previous = json.loads(declined["evidence"])
    return len(proposal.evidence) < 2 * len(previous)


def _with_proposal(gates: GateSettings, patterns: list[str], proposal: Proposal) -> tuple[GateSettings, list[str]]:
    if proposal.kind == "blacklist":
        return gates, [*patterns, str(proposal.payload["pattern"])]
    if proposal.kind == "min_salary":
        return GateSettings(int(proposal.payload["value"]), gates.penalty_senior, gates.excluded_grades), list(patterns)
    if proposal.kind == "penalty_senior":
        return GateSettings(gates.min_salary_rub, int(proposal.payload["value"]), gates.excluded_grades), list(patterns)
    grades = tuple(dict.fromkeys([*gates.excluded_grades, str(proposal.payload["word"])]))
    return GateSettings(gates.min_salary_rub, gates.penalty_senior, grades), list(patterns)


def _write_number(store: SeenStore, key: str, value: int, suggestion_id: int, previous: int, now: datetime) -> None:
    store.upsert_override(
        key=key,
        value=str(value),
        suggestion_id=suggestion_id,
        previous_value=str(previous),
        applied_at=_stamp(now),
    )


def _current_extra(store: SeenStore) -> tuple[str, ...]:
    row = store.override_map().get("EXCLUDED_GRADES_EXTRA")
    if row is None:
        return ()
    return _safe_extra(_extra_grades(row["value"]))


def _bounded_int(row: dict | None, low: int, high: int, fallback: int) -> int:
    if row is None:
        return fallback
    try:
        value = int(row["value"])
    except (TypeError, ValueError):
        return fallback
    if value < low or value > high:
        return fallback
    return value


def _extra_grades(raw: str) -> tuple[str, ...]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    words = []
    for item in data:
        word = " ".join(str(item).split())
        if word and word not in words:
            words.append(word)
    return tuple(words)


def _safe_extra(words: tuple[str, ...]) -> tuple[str, ...]:
    allowed = {word.casefold(): word for word in GRADE_DICTIONARY}
    return tuple(allowed[word.casefold()] for word in words if word.casefold() in allowed)


def _target(event: dict) -> tuple[str, str] | None:
    company = _fold(str(event.get("company") or ""))
    channel = _fold(str(event.get("channel_name") or ""))
    if company:
        return "company", company
    if channel:
        return "channel", channel
    return None


def _spelling(events: list[dict]) -> str:
    counts: dict[str, int] = {}
    for event in events:
        raw = " ".join(str(event.get("company") or event.get("channel_name") or "").split())
        if raw:
            counts[raw] = counts.get(raw, 0) + 1
    return sorted(counts, key=lambda item: (-counts[item], -len(item), item.casefold()))[0]


def _source_name(event: dict) -> str:
    return " ".join(str(event.get("company") or event.get("channel_name") or "").split())


def _has_positive(events: list[dict]) -> bool:
    return any(event["verdict"] in POSITIVE for event in events)


def _ratio_ok(events: list[dict]) -> bool:
    junk = sum(1 for event in events if event["verdict"] == "junk")
    return junk * 3 >= len(events) * 2


def _senior(event: dict) -> bool:
    return senior_penalty(str(event.get("title") or ""), 1) > 0


def _fold(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def _payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _published(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def hash_prefix(source: str, url: str) -> str:
    from vacancy_hunter.database import HASH_PREFIX_CHARS

    return content_hash(source, url)[:HASH_PREFIX_CHARS]
