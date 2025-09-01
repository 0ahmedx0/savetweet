from aiogram import Router, types, F
from aiogram.filters import CommandStart, Command
from db import add_user

router = Router()

WELCOME_MESSAGE = """
أهلاً بك في بوت تحميل وسائط X (تويتر سابقًا)! 🚀

أرسل لي أي رابط لتغريدة تحتوي على صور أو فيديوهات، وسأقوم بتنزيلها وإرسالها لك مباشرة.

**الميزات:**
- دعم روابط متعددة في رسالة واحدة.
- إرسال الصور في ألبومات.
- التعامل مع الفيديوهات كبيرة الحجم.

/settings - لضبط إعداداتك (قريبًا!)
/help - لعرض هذه الرسالة
"""

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    """معالج أمر /start"""
    # إضافة المستخدم إلى قاعدة البيانات عند البدء
    await add_user(
        user_id=message.from_user.id,
        first_name=message.from_user.first_name,
        username=message.from_user.username
    )
    await message.reply(WELCOME_MESSAGE)

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    """معالج أمر /help"""
    await message.reply(WELCOME_MESSAGE)

@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    """معالج أمر /settings (ميزة مستقبلية)"""
    await message.reply("⚙️ قائمة الإعدادات ستكون متاحة قريبًا لتخصيص تجربتك!")```
