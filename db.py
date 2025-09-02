8# db.py
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
    default_settings = {
        "send_text": True,
        "delete_original": False
    }
    user_doc = {
        "first_name": first_name,
        "username": username,
    }
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

# --- Settings Management ---
async def get_user_settings(user_id: int) -> Dict[str, Any]:
    """الحصول على إعدادات المستخدم. إذا لم توجد، يتم إرجاع الإعدادات الافتراضية."""
    user = await users_collection.find_one({"_id": user_id})
    # Handle both new users (with settings) and legacy users (without)
    if user and "settings" in user:
        # Ensure all default keys exist
        defaults = {"send_text": True, "delete_original": False}
        user['settings'].update({k: v for k, v in defaults.items() if k not in user['settings']})
        return user["settings"]
    
    return {"send_text": True, "delete_original": False}

async def update_user_setting(user_id: int, setting: str, value: bool):
    """تحديث إعداد محدد للمستخدم"""
    await users_collection.update_one(
        {"_id": user_id},
        {"$set": {f"settings.{setting}": value}}
    )
