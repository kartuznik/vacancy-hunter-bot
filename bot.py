"""Долгоживущий бот: ручной поиск и отдача файла дайджеста."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import get_settings
from vacancy_hunter.core import latest_digest, run_search

logger = logging.getLogger(__name__)

FIND_NOW = "find_now"
DOWNLOAD_LATEST = "download_latest"
DOWNLOAD_CURRENT = "download_current"

dispatcher = Dispatcher()
last_digest: dict[int, str] = {}
busy_chats: set[int] = set()


def keyboard(include_current: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔍 Найти сейчас", callback_data=FIND_NOW)],
        [InlineKeyboardButton(text="📥 Скачать дайджест", callback_data=DOWNLOAD_LATEST)],
    ]
    if include_current:
        rows.append(
            [InlineKeyboardButton(text="📥 Скачать этот дайджест", callback_data=DOWNLOAD_CURRENT)]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dispatcher.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Vacancy hunter. Поиск можно запустить кнопкой или дождаться фонового прогона.",
        reply_markup=keyboard(message.chat.id in last_digest),
    )


@dispatcher.callback_query(F.data == DOWNLOAD_LATEST)
async def download_latest(query: CallbackQuery) -> None:
    path = latest_digest()
    if path is None or query.message is None:
        await query.answer("Файлов дайджеста пока нет", show_alert=True)
        return
    await query.answer()
    await query.message.answer_document(FSInputFile(path))


@dispatcher.callback_query(F.data == DOWNLOAD_CURRENT)
async def download_current(query: CallbackQuery) -> None:
    if query.message is None:
        await query.answer()
        return
    stored = last_digest.get(query.message.chat.id)
    if not stored:
        await query.answer("Сначала запустите поиск", show_alert=True)
        return
    await query.answer()
    await query.message.answer_document(FSInputFile(stored))


@dispatcher.callback_query(F.data == FIND_NOW)
async def find_now(query: CallbackQuery) -> None:
    if query.message is None:
        await query.answer()
        return
    chat_id = query.message.chat.id
    if chat_id in busy_chats:
        await query.answer("Поиск уже идёт", show_alert=True)
        return
    busy_chats.add(chat_id)
    await query.answer("Ищу вакансии")
    try:
        result = await asyncio.to_thread(run_search, False)
    except Exception:
        logger.exception("Ручной поиск не выполнен")
        await query.message.answer("Поиск завершился с ошибкой. Подробности в журнале сервиса.")
        return
    finally:
        busy_chats.discard(chat_id)
    last_digest[chat_id] = str(result.digest_path)
    if result.selected:
        for chunk in result.blocks:
            await query.message.answer(chunk, parse_mode="HTML")
    else:
        await query.message.answer("Новых вакансий нет. Файл дайджеста обновлён.")
    await query.message.edit_reply_markup(reply_markup=keyboard(True))


async def _run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN пуст")
    bot = Bot(settings.telegram_bot_token)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run())
