# main.py
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties  # <-- مهم

import config
from handlers import general, admin, twitter

async def main():
    # Logging
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    
    # Bot and Dispatcher setup (التعديل هنا)
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.MARKDOWN
            # لو حاب، تقدر تضيف:
            # disable_web_page_preview=True,
            # protect_content=False,
        )
    )
    dp = Dispatcher()

    # Include routers
    dp.include_router(general.router)
    dp.include_router(admin.router)
    dp.include_router(twitter.router)

    # Start polling
    await bot.delete_webhook(drop_pending_updates=True)
    print("Bot is starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
