# utils.py
from aiogram.filters import Filter
from aiogram.types import Message
import config

class AdminFilter(Filter):
    """مرشح (Filter) للتحقق مما إذا كان المستخدم هو المالك"""
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == config.ADMIN_ID
