"""Настройки окружения и жёсткие списки фильтра."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HIDE_DUPLICATES = False

VACANCY_MARKERS: tuple[str, ...] = (
    "вакансия",
    "ищем",
    "требуется",
    "job",
    "hiring",
    "открыта позиция",
    "резюме",
    "сотрудник",
    "специалист",
    "работа",
    "position",
    "open",
    "разработчик",
    "инженер",
    "developer",
    "engineer",
)

CORE_PYTHON: tuple[str, ...] = (
    "python",
    "питон",
    "пайтон",
)

WHITELIST: tuple[str, ...] = (
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
)

BLACKLIST_LOCATION: tuple[str, ...] = (
    "москва",
    "санкт-петербург",
    "офис",
    "гибрид",
)

# Грейды, которые отсеиваются по названию вакансии. Латинские слова сравниваются
# целиком, кириллические записаны основой и ловят любые окончания.
EXCLUDED_GRADES: tuple[str, ...] = (
    "lead",
    "principal",
    "head",
    "owner",
    "director",
    "руководител",
    "главн",
    "лидер",
    "тимлид",
    "директор",
)

# Слова в названии, за которые вычитается PENALTY_SENIOR. 0 выключает штраф.
SENIOR_WORDS: tuple[str, ...] = ("senior", "сеньор")
PENALTY_SENIOR = 30

# Зарплатный гейт: известная зарплата строго ниже порога отсеивается до скоринга.
# Сравнивается верхняя граница вилки, при «от X» — X. 0 выключает гейт.
MIN_SALARY_RUB = 80_000
CURRENCY_RATES: dict[str, float] = {"RUB": 1.0, "USD": 95.0, "EUR": 105.0}

# Карточка моложе этого возраста получает пометку «🆕 свежая».
FRESH_MAX_MINUTES = 240

# Скор не ниже PASS_SCORE проходит в дайджест, от PARK_FLOOR до PASS_SCORE
# уходит на парковку на PARK_DAYS суток, ниже PARK_FLOOR отсеивается.
PASS_SCORE = 25
PARK_FLOOR = 10
PARK_DAYS = 7

# Заглушки профиля для /letter. Реальные значения задаются в .env:
# PROFILE_YEARS, PROFILE_STACK, PROFILE_LINKS и WHITELIST_LINKS (через запятую).
PROFILE_FACTS: dict[str, object] = {
    "years": "N лет",
    "stack": "Python",
    "links": (),
}
WHITELIST_LINKS: tuple[str, ...] = ("https://github.com/kartuznik",)

LETTER_TEMPLATE = (
    "Здравствуйте!\n"
    "\n"
    "Меня заинтересовала вакансия «{title}» в {company}.\n"
    "Опыт разработки на Python: {years}.\n"
    "Стек: {stack}.\n"
    "{links}"
    "\n"
    "Буду рад обсудить детали."
)
LETTER_COMPANY_FALLBACK = "вашей компании"

ROLE_MARKERS: tuple[str, ...] = (
    "разработчик",
    "developer",
    "engineer",
    "инженер",
    "программист",
)

BLACKLIST_SOFT: tuple[str, ...] = (
    "middle+",
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
    profile_facts: dict
    whitelist_links: tuple[str, ...]


def get_settings() -> Settings:
    _load_env_file(Path(__file__).resolve().parent / ".env")
    return Settings(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.environ.get("CHAT_ID", "").strip(),
        hh_queries=_split_list(os.environ.get("HH_QUERIES", "")),
        tg_channels=_split_list(os.environ.get("TG_CHANNELS", "")),
        habr_rss=os.environ.get("HABR_RSS", "").strip(),
        profile_facts=_profile_facts(),
        whitelist_links=_split_list(os.environ.get("WHITELIST_LINKS", "")) or WHITELIST_LINKS,
    )


def _profile_facts() -> dict:
    return {
        "years": os.environ.get("PROFILE_YEARS", "").strip() or PROFILE_FACTS["years"],
        "stack": os.environ.get("PROFILE_STACK", "").strip() or PROFILE_FACTS["stack"],
        "links": _split_list(os.environ.get("PROFILE_LINKS", "")) or tuple(PROFILE_FACTS["links"]),
    }
