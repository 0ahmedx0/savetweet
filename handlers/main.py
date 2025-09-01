# main.py
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode

# استيراد الراوتر من ملف المعالجات
from handlers import twitter
# استيراد الإعدادات
import config

async def main():
    # التحقق من توفر التوكن قبل البدء
    if not config.BOT_TOKEN:
        logging.critical("لا يمكن بدء البوت. لم يتم تعيين متغير البيئة BOT_TOKEN.")
        sys.exit(1)

    # تهيئة البوت والـ Dispatcher
    bot = Bot(token=config.BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()

    # تضمين الراوتر الخاص بمعالجة روابط تويتر
    dp.include_router(twitter.router)

    # حذف أي Webhook قديم والبدء في Polling
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped manually")
