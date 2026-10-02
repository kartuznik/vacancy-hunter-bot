"""Настройки окружения и жёсткие списки фильтра."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

WHITELIST: tuple[str, ...] = (
    "python",
    "aiogram",
    "fastapi",
    "django",
    "flask",
    "postgresql",
    "redis",
    "docker",
    "llm",
    "rag",
    "langchain",
    "langgraph",
    "chromadb",
)

BLACKLIST_HARD: tuple[str, ...] = (
    "salebot",
    "n8n",
    "make.com",
    "zapier",
    "react",
    "vue",
    "angular",
    "frontend",
    "фронтенд",
    "верстка",
    "wordpress",
    "bitrix",
    "битрикс",
    "php",
    "laravel",
    "node.js",
    "nodejs",
    "flutter",
    "ios",
    "android",
    "unity",
    "1c",
    "1с",
    "amocrm",
    "seo",
    "smm",
    "дизайн",
    "figma",
    "data scientist",
    "ml engineer",
    "machine learning",
    "devops",
    "qa",
    "тестиров",
    "москва",
    "санкт-петербург",
    "офис",
    "гибрид",
)

BLACKLIST_SOFT: tuple[str, ...] = (
    "senior",
    "middle+",
    "team lead",
    "3+ года",
    "5 лет",
    "английский b2",
    "english fluent",
)


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _split_list(value: str) -> tuple[str, ...]:
    items: list[str] = []
    for chunk in value.replace("\n", ",").split(","):
        item = chunk.strip()
        if item:
            items.append(item)
    return tuple(items)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    chat_id: str
    hh_queries: tuple[str, ...]
    tg_channels: tuple[str, ...]
    habr_rss: str


def get_settings() -> Settings:
    _load_env_file(Path(__file__).resolve().parent / ".env")
    return Settings(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.environ.get("CHAT_ID", "").strip(),
        hh_queries=_split_list(os.environ.get("HH_QUERIES", "")),
        tg_channels=_split_list(os.environ.get("TG_CHANNELS", "")),
        habr_rss=os.environ.get("HABR_RSS", "").strip(),
    )
