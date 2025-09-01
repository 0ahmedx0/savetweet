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
