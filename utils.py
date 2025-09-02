# utils.py
from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import Message
import config

class AdminFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == config.ADMIN_ID

# For the settings menu
class SettingsCallback(CallbackData, prefix="settings"):
    action: str
    setting: str

# For buttons under media
class TweetActionCallback(CallbackData, prefix="tweet"):
    action: str
    tweet_id: str
    user_msg_id: int
