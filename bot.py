"""Долгоживущий бот с командным меню и самодиагностикой."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import get_settings
from vacancy_hunter.core import latest_digest, run_search
from vacancy_hunter.database import SeenStore, default_db_path
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
)

HELP_TEXT = (
    "Команды:\n"
    "/search — запустить поиск вакансий\n"
    "/download — скачать последний дайджест\n"
    "/help — эта справка\n"
    "/health — самодиагностика системы\n"
    "/stats — статистика поиска по базе"
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
    await bot.set_my_commands(list(COMMANDS))
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run())
