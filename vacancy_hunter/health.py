"""Проверки /health. Сеть и диск вызываются из потока, чтобы не стопорить polling."""

from __future__ import annotations

import time

import requests

from config import USER_AGENT, get_settings
from vacancy_hunter.core import digests_dir
from vacancy_hunter.database import SeenStore, default_db_path

HH_URL = "https://api.hh.ru/vacancies"
TELEGRAM_URL = "https://t.me/s/job_python"
CHECK_TIMEOUT = 5


def format_uptime(started_at: float) -> str:
    total = max(0, int(time.monotonic() - started_at))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}ч")
    parts.append(f"{minutes}м")
    parts.append(f"{seconds}с")
    return " ".join(parts)


def build_health_report(started_at: float) -> str:
    lines = [
        _check_hh(),
        _check_habr(),
        _check_telegram(),
        _check_sqlite(),
        _check_channels(),
        _check_digest_write(),
        f"✅ Uptime — {format_uptime(started_at)}",
    ]
    return "Самодиагностика\n" + "\n".join(lines)


def _check_hh() -> str:
    try:
        response = requests.get(
            HH_URL,
            params={"text": "python", "per_page": 1},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=CHECK_TIMEOUT,
        )
    except requests.RequestException as exc:
        return f"❌ HH API — {type(exc).__name__}"
    if response.status_code == 200:
        return "✅ HH API — HTTP 200"
    if response.status_code == 403:
        return "✅ HH API — HTTP 403, для этого хоста это норма"
    return f"❌ HH API — HTTP {response.status_code}"


def _check_habr() -> str:
    url = get_settings().habr_rss or "https://career.habr.com/vacancies/rss?remote=true&q=python"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"},
            timeout=CHECK_TIMEOUT,
        )
    except requests.RequestException as exc:
        return f"❌ Habr RSS — {type(exc).__name__}"
    body = response.text.lstrip()
    if response.status_code == 200 and (body.startswith("<?xml") or "<rss" in body[:300]):
        return "✅ Habr RSS — HTTP 200, XML"
    return f"❌ Habr RSS — HTTP {response.status_code}"


def _check_telegram() -> str:
    try:
        response = requests.get(
            TELEGRAM_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=CHECK_TIMEOUT,
        )
    except requests.RequestException as exc:
        return f"❌ Telegram — {type(exc).__name__}"
    if response.status_code == 200:
        return "✅ Telegram t.me/s/job_python — HTTP 200"
    return f"❌ Telegram t.me/s/job_python — HTTP {response.status_code}"


def _check_sqlite() -> str:
    path = default_db_path()
    if not path.is_file():
        return "❌ SQLite — файл базы не найден"
    try:
        count = SeenStore(path).count_vacancies()
    except Exception as exc:
        return f"❌ SQLite — {type(exc).__name__}"
    return f"✅ SQLite — {count} записей"


def _check_channels() -> str:
    store = SeenStore()
    active = len(store.active_channels())
    if active:
        return f"✅ Каналы поиска: ✅ {active} активных"
    fallback = len(store.channels_for_search())
    if fallback:
        return f"⚠️ Каналы поиска: 0 активных, запасной список {fallback}"
    return "❌ Каналы поиска: 0 активных"


def _check_digest_write() -> str:
    directory = digests_dir()
    probe = directory / ".health_write_probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"❌ Запись в digests/ — {exc.strerror or type(exc).__name__}"
    if probe.exists():
        return "❌ Запись в digests/ — тестовый файл не удалён"
    return "✅ Запись в digests/ — файл создан и удалён"
