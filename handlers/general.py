# handlers/general.py
from aiogram import Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import add_user, get_user_settings, update_user_setting
from utils import SettingsCallback

router = Router()

WELCOME_MESSAGE = """
أهلاً بك في بوت تحميل وسائط X (تويتر سابقًا)! 🚀

أرسل لي أي رابط لتغريدة تحتوي على صور أو فيديوهات، وسأقوم بتنزيلها وإرسالها لك مباشرة.

**الميزات:**
- دعم روابط متعددة في رسالة واحدة.
- إرسال الصور في ألبومات.
- التعامل مع الفيديوهات كبيرة الحجم.

/settings - لضبط إعداداتك.
/help - لعرض هذه الرسالة.
"""

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    """معالج أمر /start"""
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

# --- Full Interactive Settings Menu ---
async def build_settings_keyboard(user_id: int):
    """بناء لوحة مفاتيح الإعدادات بناءً على حالة المستخدم الحالية"""
    settings = await get_user_settings(user_id)
    builder = InlineKeyboardBuilder()
    
    send_text_status = "✅ تفعيل" if settings.get("send_text") else "❌ تعطيل"
    builder.button(
        text=f"إرسال نص التغريدة: {send_text_status}",
        callback_data=SettingsCallback(action="toggle", setting="send_text")
    )
    
    delete_original_status = "✅ تفعيل" if settings.get("delete_original") else "❌ تعطيل"
    builder.button(
        text=f"حذف رسالة الرابط: {delete_original_status}",
        callback_data=SettingsCallback(action="toggle", setting="delete_original")
    )
    builder.adjust(1)
    return builder.as_markup()

@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    """عرض قائمة الإعدادات التفاعلية"""
    keyboard = await build_settings_keyboard(message.from_user.id)
    await message.reply("⚙️ **قائمة الإعدادات**\n\nقم بتخصيص سلوك البوت من هنا:", reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(SettingsCallback.filter(F.action == "toggle"))
async def handle_settings_toggle(callback: types.CallbackQuery, callback_data: SettingsCallback):
    """معالجة الضغط على أزرار الإعدادات"""
    user_id = callback.from_user.id
    setting_to_toggle = callback_data.setting
    
    current_settings = await get_user_settings(user_id)
    current_value = current_settings.get(setting_to_toggle, False)
    new_value = not current_value
    
    await update_user_setting(user_id, setting_to_toggle, new_value)
    
    new_keyboard = await build_settings_keyboard(user_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=new_keyboard)
    except Exception as e:
        print(f"Error updating settings keyboard: {e}")
        
    status_text = "تفعيل" if new_value else "تعطيل"
    setting_name = "إرسال النص" if setting_to_toggle == "send_text" else "حذف الرابط الأصلي"
    await callback.answer(f"تم {status_text} ميزة '{setting_name}'")
