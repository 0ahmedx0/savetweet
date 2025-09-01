# handlers/general.py
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
    await message.reply("⚙️ قائمة الإعدادات ستكون متاحة قريبًا لتخصيص تجربتك!")

from aiogram import Router, types
from aiogram.filters import Command
from utils import AdminFilter
from db import get_users_count

router = Router()
# تطبيق الفلتر على مستوى الراوتر بأكمله
# هذا يعني أن كل الأوامر هنا لا يمكن استدعاؤها إلا من قبل الأدمن
router.message.filter(AdminFilter())

@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """إرسال إحصائيات البوت للمالك"""
    total_users = await get_users_count()
    stats_text = (
        f"📊 **إحصائيات البوت**\n\n"
        f"👤 **إجمالي المستخدمين:** {total_users}"
    )
    await message.reply(stats_text, parse_mode="Markdown")
