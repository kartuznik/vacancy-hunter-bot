"""Долгоживущий бот с командным меню и самодиагностикой."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import get_settings
from vacancy_hunter.core import (
    SEND_PAUSE_SECONDS,
    backfill_runs_from_digests,
    chunk_messages,
    digest_by_name,
    digest_moment,
    download_note,
    export_csv,
    is_empty_digest,
    latest_digest,
    newest_digest,
    run_search,
)
from vacancy_hunter.database import SeenStore, default_db_path, normalize_channel
from vacancy_hunter.feedback import (
    accept_suggestion,
    assign_reason,
    decline_suggestion,
    feedback_history,
    feedback_report,
    overrides_report,
    pending_views,
    record_verdict,
    reset_journal,
    reset_override,
    undo_event,
    undo_latest_junk,
)
from vacancy_hunter.health import build_health_report
from vacancy_hunter.letter import compose_letter
from vacancy_hunter.report import DOWNLOAD_PREFIX, FAVORITE_PREFIX, LABEL_PREFIX, relevance_label

logger = logging.getLogger(__name__)

STARTED_AT = time.monotonic()
NEUTRAL_TEXT = "Это частный бот."
OWNER_ONLY_TEXT = "Команда доступна только владельцу."
HISTORY_LIMIT = 10
EXPORT_DEFAULT_DAYS = 30
EXPORT_MAX_DAYS = 3650
LABEL_CODES = {"r": ("relevant", "Отмечено: релевантно"), "j": ("junk", "Отмечено: мусор")}
REASON_PREFIX = "rsn:"
SUGGEST_PREFIX = "sg:"
UNDO_PREFIX = "fbu:"
RESET_PREFIX = "fbx:"
HISTORY_DEFAULT = 10
HISTORY_MAX = 30

COMMANDS = (
    BotCommand(command="search", description="Запустить поиск вакансий"),
    BotCommand(command="download", description="Скачать последний непустой дайджест"),
    BotCommand(command="history", description="Последние 10 прогонов"),
    BotCommand(command="saved", description="Избранные вакансии"),
    BotCommand(command="export", description="CSV со всеми вакансиями за период"),
    BotCommand(command="parked", description="Вакансии на парковке"),
    BotCommand(command="parked_clear", description="Очистить парковку"),
    BotCommand(command="blacklist", description="Блэклист: add, remove, list"),
    BotCommand(command="feedback", description="Сводка оценок за 30 дней"),
    BotCommand(command="feedback_history", description="Последние оценки"),
    BotCommand(command="suggestions", description="Предложения по фильтрам"),
    BotCommand(command="overrides", description="Подмены настроек из оценок"),
    BotCommand(command="override_reset", description="Откатить подмену настройки"),
    BotCommand(command="feedback_reset", description="Очистить журнал оценок"),
    BotCommand(command="letter", description="Сопроводительное письмо к вакансии"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="health", description="Самодиагностика системы"),
    BotCommand(command="stats", description="Статистика поиска"),
    BotCommand(command="add_channel", description="Добавить канал поиска"),
    BotCommand(command="remove_channel", description="Удалить канал из поиска"),
    BotCommand(command="list_channels", description="Показать список каналов"),
)

HELP_TEXT = (
    "Команды:\n"
    "/search — запустить поиск вакансий\n"
    "/download — скачать последний непустой дайджест\n"
    "/history — последние 10 прогонов, тап открывает топ-3 и файл\n"
    "/saved — избранные вакансии\n"
    "/export [дней] — CSV со всеми вакансиями за период, по умолчанию 30 дней\n"
    "/parked — вакансии на парковке по убыванию скора\n"
    "/parked_clear — очистить парковку\n"
    "/blacklist add|remove паттерн, /blacklist list — блэклист компаний и каналов\n"
    "/feedback — сводка оценок за 30 дней\n"
    "/feedback_history [N] — последние оценки, N от 1 до 30\n"
    "/suggestions — предложения по фильтрам, применяются только после подтверждения\n"
    "/overrides — подмены настроек, принятые из предложений\n"
    "/override_reset КЛЮЧ — вернуть MIN_SALARY_RUB, PENALTY_SENIOR или EXCLUDED_GRADES_EXTRA к config.py\n"
    "/feedback_reset — очистить журнал и непринятые предложения\n"
    "/letter — список вакансий последнего дайджеста; /letter номер или ссылка — письмо\n"
    "/help — эта справка\n"
    "/health — самодиагностика системы\n"
    "/stats — статистика поиска по базе\n"
    "/add_channel имя [имя ...] — добавить один или несколько каналов через пробел\n"
    "/remove_channel имя — удалить канал из поиска\n"
    "/list_channels — показать список каналов и находки за 7 дней\n"
    "\n"
    "Под карточкой: ⭐ — в избранное, 👍 Релевантно, 👎 Мусор.\n"
    "После 👎 бот спрашивает причину. Причина только записывается: фильтр меняется из /suggestions."
)

dispatcher = Dispatcher()
busy_chats: set[int] = set()


def digest_keyboard(digest_file: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Скачать этот дайджест", callback_data=f"{DOWNLOAD_PREFIX}{digest_file}")]
        ]
    )


def _owner_id() -> str:
    return get_settings().chat_id.strip()


def _is_owner(message: Message) -> bool:
    owner = _owner_id()
    return bool(owner) and str(message.chat.id) == owner


def _is_owner_query(query: CallbackQuery) -> bool:
    owner = _owner_id()
    return bool(owner) and str(query.from_user.id) == owner


async def _deny(message: Message) -> bool:
    if _is_owner(message):
        return False
    await message.answer(OWNER_ONLY_TEXT)
    return True


@dispatcher.message(CommandStart())
async def start(message: Message) -> None:
    if not _is_owner(message):
        await message.answer(NEUTRAL_TEXT)
        return
    await message.answer("Vacancy hunter. Список команд: /help")


@dispatcher.message(Command("help"))
async def help_command(message: Message) -> None:
    if not _is_owner(message):
        await message.answer(NEUTRAL_TEXT)
        return
    await message.answer(HELP_TEXT)


@dispatcher.message(Command("download"))
async def download_command(message: Message) -> None:
    if await _deny(message):
        return
    path = await asyncio.to_thread(latest_digest)
    if path is None:
        await message.answer("Дайджестов с вакансиями пока нет.")
        return
    note = download_note(path, await asyncio.to_thread(newest_digest))
    if note:
        await message.answer(note)
    await message.answer_document(FSInputFile(path))


@dispatcher.message(Command("search"))
async def search_command(message: Message) -> None:
    if await _deny(message):
        return
    chat_id = message.chat.id
    if chat_id in busy_chats:
        await message.answer("Поиск уже идёт.")
        return
    busy_chats.add(chat_id)
    await message.answer("Ищу вакансии.")
    try:
        result = await asyncio.to_thread(run_search, False)
    except Exception:
        logger.exception("Ручной поиск не выполнен")
        await message.answer("Поиск завершился с ошибкой. Подробности в журнале сервиса.")
        return
    finally:
        busy_chats.discard(chat_id)
    download = digest_keyboard(result.digest_path.name)
    if result.selected and result.messages:
        for index, (text, markup) in enumerate(result.messages):
            if index:
                await asyncio.sleep(SEND_PAUSE_SECONDS)
            if markup is not None:
                keyboard = InlineKeyboardMarkup.model_validate(markup)
            elif index == len(result.messages) - 1:
                keyboard = download
            else:
                keyboard = None
            await answer_with_retry(message, text, parse_mode="HTML", reply_markup=keyboard)
        if result.suggestion_text:
            await message.answer(result.suggestion_text)
        return
    await message.answer("Новых вакансий нет. Файл дайджеста записан.", reply_markup=download)
    if result.suggestion_text:
        await message.answer(result.suggestion_text)


async def answer_with_retry(message: Message, text: str, **kwargs) -> None:
    try:
        await message.answer(text, **kwargs)
    except TelegramRetryAfter as exc:
        logger.warning("Telegram ответил 429, повтор через %s с", exc.retry_after)
        await asyncio.sleep(exc.retry_after)
        await message.answer(text, **kwargs)


@dispatcher.callback_query(F.data.startswith(DOWNLOAD_PREFIX))
async def download_digest(query: CallbackQuery) -> None:
    if not _is_owner_query(query) or query.message is None:
        await query.answer("Доступно только владельцу.", show_alert=True)
        return
    name = (query.data or "")[len(DOWNLOAD_PREFIX) :]
    path = digest_by_name(name)
    if path is None:
        await query.answer("Файл дайджеста не найден.", show_alert=True)
        return
    await query.answer()
    text, send_file = await asyncio.to_thread(digest_tap_text, path)
    if text:
        await query.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    if send_file:
        await query.message.answer_document(FSInputFile(path))


def digest_tap_text(path, store: SeenStore | None = None) -> tuple[str, bool]:
    store = store or SeenStore()
    run = store.run_by_digest(path.name)
    moment = digest_moment(path.name)
    when = f"{moment:%d.%m %H:%M}" if moment else path.name
    if is_empty_digest(path):
        if run is None:
            return f"Прогон {when} пустой.", False
        text = (
            f"Прогон {when} пустой: собрано {run['collected_total']}, "
            f"прошло {run['passed']}, новых {run['new_count']}."
        )
        return text, False
    if run is None:
        return "", True
    cards = store.top_cards(int(run["run_id"]))
    if not cards:
        return f"Прогон {when}: карточки этого прогона в базе не сохранялись, вот файл.", True
    lines = [f"<b>Топ-3 прогона {escape(when)}</b>"]
    for index, card in enumerate(cards, start=1):
        title = escape(" ".join((card["title"] or "").split()))
        label = escape(relevance_label(int(card["score"] or 0)))
        lines.append(f"{index}. {label} {title} — {card['score']}\n{escape(card['url'])}")
    return "\n".join(lines), True


@dispatcher.callback_query(F.data.startswith(FAVORITE_PREFIX))
async def favorite_card(query: CallbackQuery) -> None:
    if not _is_owner_query(query):
        await query.answer("Доступно только владельцу.", show_alert=True)
        return
    prefix = (query.data or "")[len(FAVORITE_PREFIX) :]
    await query.answer(await asyncio.to_thread(favorite_reply, prefix))


def favorite_reply(prefix: str, store: SeenStore | None = None) -> str:
    store = store or SeenStore()
    digest = store.resolve_hash(prefix)
    if digest is None:
        return "Карточка не найдена в базе."
    saved = store.save_vacancy(digest)
    record_verdict(store, digest, "favorite")
    return "Добавлено в избранное" if saved else "Уже в избранном"


@dispatcher.callback_query(F.data.startswith(LABEL_PREFIX))
async def label_card(query: CallbackQuery) -> None:
    if not _is_owner_query(query):
        await query.answer("Доступно только владельцу.", show_alert=True)
        return
    payload = (query.data or "")[len(LABEL_PREFIX) :]
    reply = await asyncio.to_thread(label_reply, payload)
    await query.answer(reply)
    code, _, prefix = payload.partition(":")
    if code == "j" and reply == "Отмечено: мусор":
        await _edit_markup(query, reason_keyboard(prefix))


def label_reply(payload: str, store: SeenStore | None = None) -> str:
    code, _, prefix = payload.partition(":")
    if code not in LABEL_CODES:
        return "Неизвестная метка."
    store = store or SeenStore()
    digest = store.resolve_hash(prefix)
    if digest is None:
        return "Карточка не найдена в базе."
    label, reply = LABEL_CODES[code]
    store.set_label(digest, label)
    record_verdict(store, digest, label)
    return reply


@dispatcher.message(Command("history"))
async def history_command(message: Message) -> None:
    if await _deny(message):
        return
    text, keyboard = await asyncio.to_thread(history_view)
    await message.answer(text, reply_markup=keyboard)


def history_view(store: SeenStore | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    store = store or SeenStore()
    runs = store.recent_runs(HISTORY_LIMIT)
    if not runs:
        return "История прогонов пуста.", None
    lines = [f"Последние прогоны ({len(runs)}):"]
    buttons = []
    for run in runs:
        when = _run_time(run)
        if run["finished_at"] is None and run["digest_file"] is None:
            lines.append(f"• {when} — не завершён")
            continue
        lines.append(
            f"• {when} — собрано {run['collected_total']}, прошло {run['passed']}, новых {run['new_count']}"
        )
        if run["digest_file"]:
            mark = "📄" if run["new_count"] else "▫️"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{mark} {when} · новых {run['new_count']}",
                        callback_data=f"{DOWNLOAD_PREFIX}{run['digest_file']}",
                    )
                ]
            )
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n".join(lines), keyboard


def _run_time(run: dict) -> str:
    moment = digest_moment(run["digest_file"] or "")
    if moment is None:
        moment = datetime.fromisoformat(run["started_at"]).astimezone()
    return f"{moment:%d.%m %H:%M}"


@dispatcher.message(Command("saved"))
async def saved_command(message: Message) -> None:
    if await _deny(message):
        return
    chunks = await asyncio.to_thread(saved_messages)
    for index, chunk in enumerate(chunks):
        if index:
            await asyncio.sleep(SEND_PAUSE_SECONDS)
        await answer_with_retry(message, chunk, parse_mode="HTML", disable_web_page_preview=True)


def saved_messages(store: SeenStore | None = None) -> list[str]:
    store = store or SeenStore()
    rows = store.saved_list()
    if not rows:
        return ["Избранное пусто. Нажмите ⭐ под карточкой, чтобы сохранить вакансию."]
    blocks = [f"<b>⭐ Избранное ({len(rows)})</b>"]
    for row in rows:
        title = escape(" ".join((row["title"] or "без названия").split()))
        saved_at = datetime.fromisoformat(row["saved_at"]).astimezone()
        blocks.append(
            f"<b>{title}</b> — {row['score']}\n{escape(row['url'] or '')}\nдобавлено {saved_at:%d.%m.%Y %H:%M}"
        )
    return chunk_messages(blocks)


@dispatcher.message(Command("export"))
async def export_command(message: Message) -> None:
    if await _deny(message):
        return
    days = export_days(_command_argument(message.text))
    if days is None:
        await message.answer(f"Укажите число дней от 1 до {EXPORT_MAX_DAYS}: /export 30")
        return
    payload = await asyncio.to_thread(export_csv, SeenStore(), days)
    name = f"vacancies_{datetime.now():%Y-%m-%d}_{days}d.csv"
    await message.answer_document(BufferedInputFile(payload, filename=name), caption=f"Вакансии за {days} дн.")


def export_days(argument: str) -> int | None:
    if not argument:
        return EXPORT_DEFAULT_DAYS
    if not argument.isdigit():
        return None
    days = int(argument)
    return days if 1 <= days <= EXPORT_MAX_DAYS else None


@dispatcher.message(Command("add_channel"))
async def add_channel_command(message: Message) -> None:
    if await _deny(message):
        return
    argument = _command_argument(message.text)
    if not argument:
        await message.answer("Укажите имена: /add_channel канал1 канал2")
        return
    text = await asyncio.to_thread(format_add_channels_report, argument.split())
    await message.answer(text)


@dispatcher.message(Command("remove_channel"))
async def remove_channel_command(message: Message) -> None:
    if await _deny(message):
        return
    argument = _command_argument(message.text)
    if not argument:
        await message.answer("Укажите имя: /remove_channel имя_канала")
        return
    status = SeenStore().deactivate_channel(argument)
    if status == "invalid":
        await message.answer("Имя канала не прошло проверку.")
        return
    if status == "missing":
        await message.answer("Такого канала в базе нет.")
        return
    if status == "inactive":
        await message.answer("Канал уже отключён.")
        return
    await message.answer(f"Канал {normalize_channel(argument)} отключён.")


@dispatcher.message(Command("list_channels"))
async def list_channels_command(message: Message) -> None:
    if await _deny(message):
        return
    text = await asyncio.to_thread(_channel_list_text)
    await message.answer(text)


def format_add_channels_report(raw_tokens: list[str], store: SeenStore | None = None) -> str:
    store = store or SeenStore()
    added = 0
    skipped = 0
    invalid = 0
    lines: list[str] = []
    for raw in raw_tokens:
        status = store.add_channel(raw)
        name = normalize_channel(raw)
        if status == "added":
            added += 1
            lines.append(f"{name} — добавлен")
        elif status == "reactivated":
            added += 1
            lines.append(f"{name} — снова активен")
        elif status == "exists":
            skipped += 1
            lines.append(f"{name} — пропущен: уже активен")
        else:
            invalid += 1
            lines.append(f"{raw.strip()} — невалидно")
    lines.append(f"Добавлено: {added}. Пропущено: {skipped}. Невалидно: {invalid}.")
    return "\n".join(lines)


def _command_argument(text: str | None) -> str:
    parts = (text or "").split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _channel_list_text() -> str:
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = SeenStore().list_active_with_recent_counts(since)
    if not rows:
        return "Активных каналов нет."
    lines = ["Активные каналы, находки за 7 дней:"]
    for name, count in rows:
        lines.append(f"• {name} — {count}")
    return "\n".join(lines)


@dispatcher.callback_query(F.data.startswith(REASON_PREFIX))
async def reason_card(query: CallbackQuery) -> None:
    if not _is_owner_query(query):
        await query.answer("Доступно только владельцу.", show_alert=True)
        return
    payload = (query.data or "")[len(REASON_PREFIX) :]
    text, keyboard = await asyncio.to_thread(reason_reply, payload)
    await query.answer(text)
    if keyboard is not None:
        await _edit_markup(query, keyboard)


def reason_reply(payload: str, store: SeenStore | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    code, _, prefix = payload.partition(":")
    store = store or SeenStore()
    if code == "u":
        text = undo_latest_junk(store, prefix)
    else:
        text = assign_reason(store, prefix, code)
    if text.startswith("Карточка") or text.startswith("Неизвестная") or text.startswith("Оценку не"):
        return text, None
    return text, card_actions_keyboard(prefix)


def reason_keyboard(prefix: str) -> InlineKeyboardMarkup:
    def button(text: str, code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"{REASON_PREFIX}{code}:{prefix}")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("🏢 Спам-компания/канал", "c")],
            [button("💸 Низкая зарплата", "s"), button("🎓 Не тот грейд", "g")],
            [button("🧰 Не мой стек", "t"), button("🔁 Дубль/уже видел", "d")],
            [button("🤐 Без причины", "n"), button("↩️ Отменить оценку", "u")],
        ]
    )


def card_actions_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐", callback_data=f"{FAVORITE_PREFIX}{prefix}"),
                InlineKeyboardButton(text="👍 Релевантно", callback_data=f"{LABEL_PREFIX}r:{prefix}"),
                InlineKeyboardButton(text="👎 Мусор", callback_data=f"{LABEL_PREFIX}j:{prefix}"),
            ]
        ]
    )


@dispatcher.message(Command("feedback"))
async def feedback_command(message: Message) -> None:
    if await _deny(message):
        return
    await message.answer(await asyncio.to_thread(feedback_report, SeenStore()))


@dispatcher.message(Command("feedback_history"))
async def feedback_history_command(message: Message) -> None:
    if await _deny(message):
        return
    limit = history_limit(_command_argument(message.text))
    if limit is None:
        await message.answer(f"Формат: /feedback_history или /feedback_history N, N от 1 до {HISTORY_MAX}.")
        return
    text, active = await asyncio.to_thread(feedback_history, SeenStore(), limit)
    buttons = [
        [InlineKeyboardButton(text=f"↩️ Отменить #{item}", callback_data=f"{UNDO_PREFIX}{item}")] for item in active
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer(text, reply_markup=keyboard)


def history_limit(argument: str) -> int | None:
    text = argument.strip()
    if not text:
        return HISTORY_DEFAULT
    if text.isdigit() and 1 <= int(text) <= HISTORY_MAX:
        return int(text)
    return None


@dispatcher.callback_query(F.data.startswith(UNDO_PREFIX))
async def undo_feedback_card(query: CallbackQuery) -> None:
    if not _is_owner_query(query):
        await query.answer("Доступно только владельцу.", show_alert=True)
        return
    raw = (query.data or "")[len(UNDO_PREFIX) :]
    if not raw.isdigit():
        await query.answer("Оценка не найдена.")
        return
    text = await asyncio.to_thread(undo_event, SeenStore(), int(raw))
    await query.answer(text)


@dispatcher.message(Command("suggestions"))
async def suggestions_command(message: Message) -> None:
    if await _deny(message):
        return
    views = await asyncio.to_thread(pending_views, SeenStore())
    if not views:
        await message.answer("Предложений нет.")
        return
    for index, view in enumerate(views):
        if index:
            await asyncio.sleep(SEND_PAUSE_SECONDS)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Применить", callback_data=f"{SUGGEST_PREFIX}a:{view['id']}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{SUGGEST_PREFIX}d:{view['id']}"),
                ]
            ]
        )
        await answer_with_retry(message, view["text"], reply_markup=keyboard)


@dispatcher.callback_query(F.data.startswith(SUGGEST_PREFIX))
async def suggestion_card(query: CallbackQuery) -> None:
    if not _is_owner_query(query):
        await query.answer("Доступно только владельцу.", show_alert=True)
        return
    action, _, raw = (query.data or "")[len(SUGGEST_PREFIX) :].partition(":")
    if action not in {"a", "d"} or not raw.isdigit():
        await query.answer("Предложение не найдено.")
        return
    suggestion_id = int(raw)
    if action == "a":
        text = await asyncio.to_thread(accept_suggestion, SeenStore(), suggestion_id)
    else:
        text = await asyncio.to_thread(decline_suggestion, SeenStore(), suggestion_id)
    await query.answer(text[:200])
    if query.message is not None:
        try:
            await query.message.edit_text(text)
        except TelegramBadRequest:
            logger.info("Сообщение предложения не обновилось")


@dispatcher.message(Command("overrides"))
async def overrides_command(message: Message) -> None:
    if await _deny(message):
        return
    await message.answer(await asyncio.to_thread(overrides_report, SeenStore()))


@dispatcher.message(Command("override_reset"))
async def override_reset_command(message: Message) -> None:
    if await _deny(message):
        return
    text = await asyncio.to_thread(reset_override, SeenStore(), _command_argument(message.text))
    await message.answer(text)


@dispatcher.message(Command("feedback_reset"))
async def feedback_reset_command(message: Message) -> None:
    if await _deny(message):
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сбросить журнал", callback_data=f"{RESET_PREFIX}yes"),
                InlineKeyboardButton(text="❌ Не сбрасывать", callback_data=f"{RESET_PREFIX}no"),
            ]
        ]
    )
    await message.answer(
        "Сбросить журнал оценок и непринятые предложения? Применённые блэклист и настройки останутся.",
        reply_markup=keyboard,
    )


@dispatcher.callback_query(F.data.startswith(RESET_PREFIX))
async def feedback_reset_card(query: CallbackQuery) -> None:
    if not _is_owner_query(query) or query.message is None:
        await query.answer("Доступно только владельцу.", show_alert=True)
        return
    if (query.data or "")[len(RESET_PREFIX) :] != "yes":
        await query.answer("Сброс отменён.")
        await query.message.edit_text("Сброс отменён.")
        return
    text = await asyncio.to_thread(reset_journal, SeenStore())
    await query.answer("Журнал очищен.")
    await query.message.edit_text(text)


async def _edit_markup(query: CallbackQuery, keyboard: InlineKeyboardMarkup) -> None:
    if query.message is None:
        return
    try:
        await query.message.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest:
        logger.info("Клавиатуру карточки не обновил")


@dispatcher.message(Command("parked"))
async def parked_command(message: Message) -> None:
    if await _deny(message):
        return
    for chunk in await asyncio.to_thread(parked_messages):
        await answer_with_retry(message, chunk, parse_mode="HTML", disable_web_page_preview=True)


def parked_messages(store: SeenStore | None = None) -> list[str]:
    rows = (store or SeenStore()).parked_list()
    if not rows:
        return ["Парковка пуста."]
    blocks = [f"<b>🅿️ Парковка ({len(rows)})</b>"]
    for row in rows:
        title = escape(" ".join((row["title"] or "").split()))
        salary = escape(row["salary"] or "не указана")
        expires = datetime.fromisoformat(row["expires_at"]).astimezone()
        blocks.append(
            f"<b>{title}</b> — {row['score']}\nЗарплата: {salary}\n{escape(row['url'])}\nдо {expires:%d.%m %H:%M}"
        )
    return chunk_messages(blocks)


@dispatcher.message(Command("parked_clear"))
async def parked_clear_command(message: Message) -> None:
    if await _deny(message):
        return
    removed = await asyncio.to_thread(lambda: SeenStore().clear_parked())
    await message.answer(f"Парковка очищена, удалено записей: {removed}.")


@dispatcher.message(Command("blacklist"))
async def blacklist_command(message: Message) -> None:
    if await _deny(message):
        return
    text = await asyncio.to_thread(blacklist_reply, _command_argument(message.text))
    await message.answer(text)


def blacklist_reply(argument: str, store: SeenStore | None = None) -> str:
    action, _, pattern = argument.partition(" ")
    action = action.lower()
    pattern = pattern.strip()
    store = store or SeenStore()
    if action == "list":
        patterns = store.blacklist_patterns()
        if not patterns:
            return "Блэклист пуст."
        return "Блэклист:\n" + "\n".join(f"• {item}" for item in patterns)
    if action == "add" and pattern:
        status = store.blacklist_add(pattern)
        return {
            "added": f"Добавлено в блэклист: {pattern}",
            "exists": f"Уже в блэклисте: {pattern}",
        }.get(status, "Паттерн должен быть не короче двух символов.")
    if action == "remove" and pattern:
        if store.blacklist_remove(pattern):
            return f"Удалено из блэклиста: {pattern}"
        return f"Такого паттерна нет: {pattern}"
    return "Формат: /blacklist add паттерн, /blacklist remove паттерн, /blacklist list"


@dispatcher.message(Command("letter"))
async def letter_command(message: Message) -> None:
    if await _deny(message):
        return
    text = await asyncio.to_thread(letter_reply, _command_argument(message.text))
    await message.answer(text, disable_web_page_preview=True)


def letter_reply(argument: str, store: SeenStore | None = None, settings=None) -> str:
    cards = (store or SeenStore()).latest_digest_cards()
    if not cards:
        return "В последнем дайджесте с находками нет сохранённых карточек."
    if not argument:
        lines = ["Вакансии последнего дайджеста. Письмо: /letter номер или /letter ссылка"]
        for index, card in enumerate(cards, start=1):
            title = " ".join((card["title"] or "").split())
            lines.append(f"{index}. {title} — {card['score']}\n{card['url']}")
        return "\n".join(lines)
    card = _pick_card(cards, argument)
    if card is None:
        return "Вакансия не найдена в последнем дайджесте. Список: /letter"
    settings = settings or get_settings()
    result = compose_letter(card, settings.profile_facts, settings.whitelist_links)
    if result.text is None:
        return "Письмо не собрано, гвард нашёл нарушения:\n" + "\n".join(f"• {item}" for item in result.violations)
    return result.text


def _pick_card(cards: list[dict], argument: str) -> dict | None:
    if argument.isdigit():
        index = int(argument)
        return cards[index - 1] if 1 <= index <= len(cards) else None
    wanted = argument.strip().rstrip("/")
    return next((card for card in cards if (card["url"] or "").rstrip("/") == wanted), None)


@dispatcher.message(Command("stats"))
async def stats_command(message: Message) -> None:
    if await _deny(message):
        return
    text = await asyncio.to_thread(_stats_text)
    await message.answer(text)


@dispatcher.message(Command("health"))
async def health_command(message: Message) -> None:
    if await _deny(message):
        return
    await message.answer("Проверяю источники, базу и диск.")
    report = await asyncio.to_thread(build_health_report, STARTED_AT)
    await message.answer(report)


def _stats_text() -> str:
    path = default_db_path()
    if not path.is_file():
        return "В базе пока нет вакансий."
    rows = SeenStore(path).stats_by_source()
    if not rows:
        return "В базе пока нет вакансий."
    lines = ["Статистика поиска"]
    for source, count, seen_at in rows:
        stamp = seen_at or "нет даты"
        lines.append(f"{source}: {count}, последнее обновление {stamp}")
    return "\n".join(lines)


async def _run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN пуст")
    restored = await asyncio.to_thread(backfill_runs_from_digests, SeenStore())
    if restored:
        logger.info("История прогонов восстановлена из дайджестов: %s", restored)
    bot = Bot(settings.telegram_bot_token)
    commands = list(COMMANDS)
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run())
