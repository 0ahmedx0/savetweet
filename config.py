# config.py
import os
from pathlib import Path
from dotenv import load_dotenv

# تحميل متغيرات البيئة من ملف .env
# سيبحث عن ملف .env في المجلد الحالي والمجلدات الأصلية
env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

# --- Aiogram Bot Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set!")

# --- Pyrogram UserBot Configuration ---
API_ID = os.getenv("ID")
API_HASH = os.getenv("HASH")
PYRO_SESSION_STRING = os.getenv("PYRO_SESSION_STRING")
if not all([API_ID, API_HASH, PYRO_SESSION_STRING]):
    raise ValueError("Pyrogram API_ID, API_HASH, or PYRO_SESSION_STRING is missing!")

# --- Channel & Directories ---
# يجب أن يكون المعرّف رقمًا صحيحًا
CHANNEL_ID_STR = os.getenv("CHANNEL_IDtwiter")
if not CHANNEL_ID_STR:
    raise ValueError("CHANNEL_IDtwiter environment variable not set!")
CHANNEL_ID = int(CHANNEL_ID_STR)

OUTPUT_DIR_STR = os.getenv("OUTPUT_DIR", "/content/downloads")
OUTPUT_DIR = Path(OUTPUT_DIR_STR)
# إنشاء مجلد الإخراج إذا لم يكن موجودًا
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --- Optional Cookies for protected tweets ---
X_COOKIES_PATH = os.getenv("X_COOKIES")
X_COOKIES = Path(X_COOKIES_PATH) if X_COOKIES_PATH and Path(X_COOKIES_PATH).is_file() else None

# --- Constants ---
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
