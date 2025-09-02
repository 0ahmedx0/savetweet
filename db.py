# db.py
import motor.motor_asyncio
from datetime import datetime
from typing import Dict, Any
import config

# --- Database Setup ---
client = motor.motor_asyncio.AsyncIOMotorClient(config.MONGO_DB_URL)
db = client.xDownloaderBot
users_collection = db.users

# --- User Management ---
async def add_user(user_id: int, first_name: str, username: str | None):
    """إضافة مستخدم جديد أو تحديث بياناته مع إعدادات افتراضية"""
    # <<< جديد: الإعدادات الافتراضية للمستخدم الجديد
    default_settings = {
        "send_text": True,
        "delete_original": False
    }
    user_doc = {
        "first_name": first_name,
        "username": username,
    }
    # upsert=True: إذا كان المستخدم موجودًا، قم بتحديث بياناته فقط.
    # $setOnInsert: لن يتم تنفيذ هذا الجزء إلا عند إضافة مستخدم جديد تمامًا.
    await users_collection.update_one(
        {"_id": user_id},
        {
            "$set": user_doc,
            "$setOnInsert": {
                "join_date": datetime.utcnow(),
                "settings": default_settings
            }
        },
        upsert=True
    )

async def get_users_count() -> int:
    return await users_collection.count_documents({})

# --- Settings Management --- <<< جديد بالكامل
async def get_user_settings(user_id: int) -> Dict[str, Any]:
    """الحصول على إعدادات المستخدم. إذا لم توجد، يتم إرجاع الإعدادات الافتراضية."""
    user = await users_collection.find_one({"_id": user_id})
    if user and "settings" in user:
        return user["settings"]
    # في حال كان مستخدم قديم جدًا وليس لديه قسم للإعدادات
    return {"send_text": True, "delete_original": False}

async def update_user_setting(user_id: int, setting: str, value: bool):
    """تحديث إعداد محدد للمستخدم"""
    await users_collection.update_one(
        {"_id": user_id},
        {"$set": {f"settings.{setting}": value}}
    )
