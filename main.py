"""Entry point: run the Telegram moderation bot."""

import asyncio
import logging
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from adapters.telegram.bot import TelegramAdapter
from core.jev_client import JevClient
from core.moderation import Moderator
from core.storage import Storage


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    storage = Storage(os.getenv("DB_PATH", "bot.db"))
    await storage.open()
    jev = JevClient(os.environ["TYPESAFE_API_KEY"], model=os.getenv("JEV_MODEL", "jev-latest"))
    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    adapter = TelegramAdapter(bot, Moderator(jev), storage)
    digest_task = asyncio.create_task(adapter.digest_loop())
    try:
        await adapter.dispatcher().start_polling(
            bot, allowed_updates=["message", "callback_query", "my_chat_member"]
        )
    finally:
        digest_task.cancel()
        await jev.close()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
