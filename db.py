# db.py
import motor.motor_asyncio
from datetime import datetime
import config

# --- Database Setup ---
client = motor.motor_asyncio.AsyncIOMotorClient(config.MONGO_DB_URL)
db = client.xDownloaderBot # اسم قاعدة البيانات
users_collection = db.users # اسم الـ collection

async def add_user(user_id: int, first_name: str, username: str | None):
    """إضافة مستخدم جديد أو تحديث بياناته عند البدء"""
    user_doc = {
        "_id": user_id,
        "first_name": first_name,
        "username": username,
        "join_date": datetime.utcnow(),
    }
    # upsert=True تعني: إذا كان المستخدم موجودًا، قم بتحديثه. إذا لم يكن، قم بإضافته.
    await users_collection.update_one(
        {"_id": user_id},
        {"$set": user_doc, "$setOnInsert": {"join_date": datetime.utcnow()}},
        upsert=True
    )

async def get_users_count() -> int:
    """الحصول على العدد الإجمالي للمستخدمين"""
    return await users_collection.count_documents({})

async def is_admin(user_id: int) -> bool:
    """التحقق مما إذا كان المستخدم هو المالك"""
    return user_id == config.ADMIN_ID
