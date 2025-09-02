# utils.py
from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import Message
import config

class AdminFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == config.ADMIN_ID

# <<< جديد: لتنظيم أزرار الإعدادات
class SettingsCallback(CallbackData, prefix="settings"):
    action: str # e.g., 'toggle'
    setting: str # e.g., 'send_text'

# <<< جديد: لتنظيم أزرار التغريدة
class TweetActionCallback(CallbackData, prefix="tweet"):
    action: str # 'delete' or 'show_text'
    tweet_id: str
    user_msg_id: int```

---

### 3. تحديث `handlers/general.py` (لجعل `/settings` يعمل)

هنا سنبني قائمة الإعدادات التفاعلية بالكامل.

```python
# handlers/general.py
from aiogram import Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import add_user, get_user_settings, update_user_setting
from utils import SettingsCallback # <<< جديد

router = Router()

WELCOME_MESSAGE = "أهلاً بك..." # (يبقى كما هو)

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    await add_user(
        user_id=message.from_user.id,
        first_name=message.from_user.first_name,
        username=message.from_user.username
    )
    await message.reply(WELCOME_MESSAGE)

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.reply(WELCOME_MESSAGE)

# --- قسم الإعدادات المطور بالكامل ---
async def build_settings_keyboard(user_id: int):
    """بناء لوحة مفاتيح الإعدادات بناءً على حالة المستخدم الحالية"""
    settings = await get_user_settings(user_id)
    builder = InlineKeyboardBuilder()
    
    # زر إرسال النص
    send_text_status = "✅" if settings.get("send_text") else "❌"
    builder.button(
        text=f"إرسال نص التغريدة: {send_text_status}",
        callback_data=SettingsCallback(action="toggle", setting="send_text")
    )
    # زر حذف الرابط
    delete_original_status = "✅" if settings.get("delete_original") else "❌"
    builder.button(
        text=f"حذف رسالة الرابط: {delete_original_status}",
        callback_data=SettingsCallback(action="toggle", setting="delete_original")
    )
    builder.adjust(1) # كل زر في سطر
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
    
    # 1. الحصول على الحالة الحالية وعكسها
    current_settings = await get_user_settings(user_id)
    current_value = current_settings.get(setting_to_toggle, False)
    new_value = not current_value
    
    # 2. تحديث القيمة في قاعدة البيانات
    await update_user_setting(user_id, setting_to_toggle, new_value)
    
    # 3. تحديث شكل الأزرار لتعكس التغيير
    new_keyboard = await build_settings_keyboard(user_id)
    await callback.message.edit_reply_markup(reply_markup=new_keyboard)
    
    # 4. إظهار تنبيه بسيط للمستخدم
    await callback.answer(f"تم {'تفعيل' if new_value else 'تعطيل'} الميزة بنجاح!")
