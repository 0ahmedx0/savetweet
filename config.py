# config.py
import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

# --- Bot & Admin ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_STR = os.getenv("ADMIN_ID")
if not BOT_TOKEN or not ADMIN_ID_STR:
    raise ValueError("BOT_TOKEN and ADMIN_ID environment variables are required!")
ADMIN_ID = int(ADMIN_ID_STR)

# --- Pyrogram ---
API_ID = os.getenv("ID")
API_HASH = os.getenv("HASH")
PYRO_SESSION_STRING = os.getenv("PYRO_SESSION_STRING")
CHANNEL_ID_STR = os.getenv("CHANNEL_IDtwiter")
if not all([API_ID, API_HASH, PYRO_SESSION_STRING, CHANNEL_ID_STR]):
    raise ValueError("Pyrogram configuration is incomplete!")
CHANNEL_ID = int(CHANNEL_ID_STR)

# --- Database ---
MONGO_DB_URL = os.getenv("MONGO_DB")
if not MONGO_DB_URL:
    raise ValueError("MONGO_DB environment variable is required!")

# --- Bot Paths & Constants ---
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/content/downloads"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
X_COOKIES_PATH = os.getenv("X_COOKIES")
X_COOKIES = Path(X_COOKIES_PATH) if X_COOKIES_PATH and Path(X_COOKIES_PATH).is_file() else None
MAX_FILE_SIZE = 50 * 1024 * 1024
