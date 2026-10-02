"""Долгоживущий бот с командным меню и самодиагностикой."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import get_settings
from vacancy_hunter.core import latest_digest, run_search
from vacancy_hunter.database import SeenStore, default_db_path, normalize_channel
from vacancy_hunter.health import build_health_report

logger = logging.getLogger(__name__)

DOWNLOAD_CURRENT = "download_current"
STARTED_AT = time.monotonic()

COMMANDS = (
    BotCommand(command="search", description="Запустить поиск вакансий"),
    BotCommand(command="download", description="Скачать последний дайджест"),
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
    "/download — скачать последний дайджест\n"
    "/help — эта справка\n"
    "/health — самодиагностика системы\n"
    "/stats — статистика поиска по базе\n"
    "/add_channel имя [имя ...] — добавить один или несколько каналов через пробел\n"
    "/remove_channel имя — удалить канал из поиска\n"
    "/list_channels — показать список каналов и находки за 7 дней"
)

dispatcher = Dispatcher()
last_digest: dict[int, str] = {}
busy_chats: set[int] = set()


def current_digest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Скачать этот дайджест", callback_data=DOWNLOAD_CURRENT)]
        ]
    )


@dispatcher.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Vacancy hunter. Список команд: /help")


@dispatcher.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP_TEXT)


@dispatcher.message(Command("download"))
async def download_command(message: Message) -> None:
    path = await asyncio.to_thread(latest_digest)
    if path is None:
        await message.answer("Файлов дайджеста пока нет.")
        return
    await message.answer_document(FSInputFile(path))


@dispatcher.message(Command("search"))
async def search_command(message: Message) -> None:
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
    last_digest[chat_id] = str(result.digest_path)
    if result.selected and result.blocks:
        for chunk in result.blocks[:-1]:
            await message.answer(chunk, parse_mode="HTML")
        await message.answer(result.blocks[-1], parse_mode="HTML", reply_markup=current_digest_keyboard())
        return
    await message.answer(
        "Новых вакансий нет. Файл дайджеста обновлён.",
        reply_markup=current_digest_keyboard(),
    )


@dispatcher.callback_query(F.data == DOWNLOAD_CURRENT)
async def download_current(query: CallbackQuery) -> None:
    if query.message is None:
        await query.answer()
        return
    stored = last_digest.get(query.message.chat.id)
    if not stored:
        await query.answer("Сначала запустите /search", show_alert=True)
        return
    await query.answer()
    await query.message.answer_document(FSInputFile(stored))


def _is_owner(message: Message) -> bool:
    owner = get_settings().chat_id.strip()
    return bool(owner) and str(message.chat.id) == owner


@dispatcher.message(Command("add_channel"))
async def add_channel_command(message: Message) -> None:
    if not _is_owner(message):
        await message.answer("Команда доступна только владельцу.")
        return
    argument = _command_argument(message.text)
    if not argument:
        await message.answer("Укажите имена: /add_channel канал1 канал2")
        return
    text = await asyncio.to_thread(format_add_channels_report, argument.split())
    await message.answer(text)


@dispatcher.message(Command("remove_channel"))
async def remove_channel_command(message: Message) -> None:
    if not _is_owner(message):
        await message.answer("Команда доступна только владельцу.")
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
    if not _is_owner(message):
        await message.answer("Команда доступна только владельцу.")
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


@dispatcher.message(Command("stats"))
async def stats_command(message: Message) -> None:
    text = await asyncio.to_thread(_stats_text)
    await message.answer(text)


@dispatcher.message(Command("health"))
async def health_command(message: Message) -> None:
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
    bot = Bot(settings.telegram_bot_token)
    commands = list(COMMANDS)
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run())
