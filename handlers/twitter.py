# handlers/twitter.py
import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import List, Dict, Optional

import aiohttp
from aiogram import Bot, Router, F, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (FSInputFile, InputMediaPhoto, Message,
                           ReactionTypeEmoji, InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pyrogram import Client as PyroClient
from pyrogram.errors import FloodWait

import config
from db import get_user_settings
from utils import TweetActionCallback

router = Router()
chat_queues: Dict[int, asyncio.Queue] = {}
active_workers: set[int] = set()
download_semaphore = asyncio.Semaphore(4)

# --- Helper Functions (No changes here) ---
def _get_session() -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(total=45)
    return aiohttp.ClientSession(timeout=timeout)

async def extract_tweet_ids(text: str) -> Optional[List[str]]:
    url_pattern = r'https?://(?:(?:www\.)?(?:twitter|x)\.com/\S+/status/\d+|t\.co/\S+)'
    urls = re.findall(url_pattern, text)
    if not urls: return None
    ordered_unique_ids, seen_ids = [], set()
    async with _get_session() as session:
        for url in urls:
            current_tweet_id = None
            if 't.co/' in url:
                try:
                    async with session.head(url, allow_redirects=True) as response:
                        match = re.search(r'/status/(\d+)', str(response.url))
                        if match: current_tweet_id = match.group(1)
                except Exception as e: print(f"Could not resolve {url}: {e}")
            else:
                match = re.search(r'/status/(\d+)', url)
                if match: current_tweet_id = match.group(1)
            if current_tweet_id and current_tweet_id not in seen_ids:
                ordered_unique_ids.append(current_tweet_id)
                seen_ids.add(current_tweet_id)
    return ordered_unique_ids if ordered_unique_ids else None

async def ytdlp_download_tweet_video(tweet_id: str, out_dir: Path) -> Optional[Path]:
    tweet_url = f"https://x.com/i/status/{tweet_id}"
    output_path = out_dir / f"{tweet_id}.mp4"
    cmd = ['yt-dlp', '--quiet', '-f', 'bv*+ba/best', '--merge-output-format', 'mp4',
           '--retries', '3', '--fragment-retries', '3', '-o', str(output_path), tweet_url]
    if config.X_COOKIES: cmd.extend(['--cookies', str(config.X_COOKIES)])
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    if process.returncode == 0 and output_path.exists(): return output_path
    if "No video could be found" not in stderr.decode(): print(f"yt-dlp failed for tweet {tweet_id}: {stderr.decode()}")
    return None

async def scrape_media(tweet_id: str) -> Optional[dict]:
    api_url = f"https://api.vxtwitter.com/i/status/{tweet_id}"
    try:
        async with _get_session() as session, session.get(api_url) as response:
            if response.status == 200: return await response.json()
            return None
    except Exception as e:
        print(f"vxtwitter scrape failed for {tweet_id}: {e}")
        return None

async def download_media(session: aiohttp.ClientSession, media_url: str, file_path: Path) -> bool:
    async with download_semaphore:
        try:
            async with session.get(media_url) as response:
                if response.status == 200:
                    with open(file_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(8192): f.write(chunk)
                    return True
        except Exception: pass
    return False

async def send_large_file_pyro(file_path: Path, caption: Optional[str] = None, parse_mode: str = "Markdown", markup: Optional[InlineKeyboardMarkup] = None):
    app = PyroClient("user_bot", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.PYRO_SESSION_STRING, in_memory=True)
    try:
        await app.start()
        from pyrogram.enums import ParseMode as PyroParseMode
        pyro_parse_mode = PyroParseMode.MARKDOWN if parse_mode and parse_mode.lower() == "markdown" else PyroParseMode.HTML
        await app.send_video(config.CHANNEL_ID, str(file_path), caption=caption, parse_mode=pyro_parse_mode, reply_markup=markup)
        await app.stop()
    except FloodWait as e:
        await asyncio.sleep(e.value); await send_large_file_pyro(file_path, caption, parse_mode, markup)
    except Exception as e:
        print(f"Pyrogram failed: {e}")
        if await app.is_connected: await app.stop()


# --- Functions with CRUCIAL FIX ---
def escape_markdown(text: str) -> str:
    """
    <<< الإصلاح الحاسم هنا: إعادة دالة تهريب الرموز >>>
    """
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def format_caption(tweet_data: dict) -> str:
    user_name = escape_markdown(tweet_data.get("user_name", "Unknown"))
    user_screen_name = tweet_data.get("user_screen_name", "unknown")
    return f"🐦 **بواسطة:** {user_name} (`@{user_screen_name}`)"

def create_inline_keyboard(tweet_data: dict, user_msg_id: int) -> InlineKeyboardMarkup:
    tweet_url = tweet_data.get("tweetURL", "")
    tweet_id = tweet_data.get("id", "0")
    if not tweet_url: return None
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 الرابط الأصلي", url=tweet_url)
    builder.button(text="🗑️ حذف", callback_data=TweetActionCallback(action="delete", tweet_id=tweet_id, user_msg_id=user_msg_id))
    builder.adjust(2)
    return builder.as_markup()

async def send_tweet_text_reply(original_message: Message, last_media_message: Message, tweet_data: dict):
    tweet_text = tweet_data.get("text")
    if tweet_text:
        # <<< تطبيق الإصلاح هنا قبل الإرسال >>>
        safe_text = escape_markdown(tweet_text)
        await last_media_message.reply(f"📝 **نص التغريدة:**\n\n{safe_text}", parse_mode="Markdown", disable_web_page_preview=True)

async def process_single_tweet(message: Message, tweet_id: str, settings: Dict):
    temp_dir = config.OUTPUT_DIR / str(uuid.uuid4())
    temp_dir.mkdir()
    bot: Bot = message.bot
    last_sent_message: Optional[Message] = None
    
    try:
        video_path = await ytdlp_download_tweet_video(tweet_id, temp_dir)
        if video_path:
            tweet_url = f"https://x.com/i/status/{tweet_id}"
            caption = f"🐦 [فيديو من تويتر]({tweet_url})"
            keyboard = create_inline_keyboard({"tweetURL": tweet_url, "id": tweet_id}, user_msg_id=message.message_id)
            if video_path.stat().st_size > config.MAX_FILE_SIZE:
                await send_large_file_pyro(video_path, caption, "Markdown", keyboard)
                last_sent_message = await message.reply("✅ تم رفع الفيديو بنجاح للقناة.", reply_markup=keyboard)
            else:
                last_sent_message = await message.reply_video(FSInputFile(video_path), caption=caption, parse_mode="Markdown", reply_markup=keyboard)
            
            # Since yt-dlp doesn't reliably fetch text, we will fetch it now if needed.
            if settings.get("send_text"):
                tweet_data = await scrape_media(tweet_id)
                if tweet_data: await send_tweet_text_reply(message, last_sent_message, tweet_data)
            return

        tweet_data = await scrape_media(tweet_id)
        if not tweet_data or not tweet_data.get("media_extended"):
            await message.reply(f"لم أتمكن من العثور على وسائط للتغريدة:\nhttps://x.com/i/status/{tweet_id}")
            return
        
        caption = format_caption(tweet_data)
        keyboard = create_inline_keyboard(tweet_data, user_msg_id=message.message_id)

        media_items, photos, videos = tweet_data.get("media_extended", []), [], []
        async with _get_session() as session:
            tasks = [download_media(session, item['url'], temp_dir / Path(item['url']).name.split('?')[0]) for item in media_items]
            await asyncio.gather(*tasks)

        for item in media_items:
            path = temp_dir / Path(item['url']).name.split('?')[0]
            if path.exists():
                if item['type'] == 'image': photos.append(path)
                elif item['type'] in ['video', 'gif']: videos.append(path)
        
        if photos:
            photo_groups = [photos[i:i + 5] for i in range(0, len(photos), 5)]
            for i, group in enumerate(photo_groups):
                media_group = [InputMediaPhoto(media=FSInputFile(p), caption=caption if i == 0 and j == 0 else None, parse_mode="Markdown") for j, p in enumerate(group)]
                if not media_group: continue
                sent_messages = await message.reply_media_group(media_group)
                last_sent_message = sent_messages[-1]
                if keyboard: await bot.edit_message_reply_markup(chat_id=last_sent_message.chat.id, message_id=last_sent_message.message_id, reply_markup=keyboard)
        
        for video_path in videos:
            if video_path.stat().st_size > config.MAX_FILE_SIZE:
                await send_large_file_pyro(video_path, caption, "Markdown", keyboard)
                last_sent_message = await message.reply("✅ تم رفع الفيديو بنجاح للقناة.", reply_markup=keyboard)
            else:
                last_sent_message = await message.reply_video(FSInputFile(video_path), caption=caption, parse_mode="Markdown", reply_markup=keyboard)

        if last_sent_message and settings.get("send_text"):
            await send_tweet_text_reply(message, last_sent_message, tweet_data)

    finally:
        if temp_dir.exists(): shutil.rmtree(temp_dir)

# --- Queue and Handler Logic with Fixes ---
async def process_chat_queue(chat_id: int, bot: Bot):
    queue = chat_queues.get(chat_id)
    if not queue: active_workers.discard(chat_id); return
    while not queue.empty():
        message, tweet_ids, progress_msg = await queue.get()
        settings = await get_user_settings(message.from_user.id)
        try:
            total = len(tweet_ids)
            for i, tweet_id in enumerate(tweet_ids, 1):
                try:
                    try:
                        await progress_msg.edit_text(f"⏳ جاري معالجة الرابط **{i}** من **{total}**...", parse_mode="Markdown")
                    except TelegramBadRequest as e:
                        if "message is not modified" not in e.message: raise
                    await process_single_tweet(message, tweet_id, settings)
                except Exception as e: print(f"Error processing tweet {tweet_id}: {e}")
            await progress_msg.edit_text(f"✅ اكتملت معالجة **{total}** روابط!", parse_mode="Markdown")
            await asyncio.sleep(5); await progress_msg.delete()
            if settings.get("delete_original"):
                try: await message.delete()
                except Exception: pass
        finally:
            queue.task_done()
    active_workers.discard(chat_id)

@router.message(F.text & (F.text.contains("twitter.com") | F.text.contains("x.com") | F.text.contains("t.co")))
async def handle_twitter_links(message: types.Message, bot: Bot):
    tweet_ids = await extract_tweet_ids(message.text)
    if not tweet_ids: return
    progress_msg = await message.reply(f"تم استلام **{len(tweet_ids)}** روابط...", parse_mode="Markdown")
    chat_id = message.chat.id
    if chat_id not in chat_queues: chat_queues[chat_id] = asyncio.Queue()
    await chat_queues[chat_id].put((message, tweet_ids, progress_msg))
    try: await bot.set_message_reaction(chat_id, message.message_id, reaction=[ReactionTypeEmoji(emoji='👨‍💻')])
    except Exception: pass
    if chat_id not in active_workers:
        active_workers.add(chat_id)
        asyncio.create_task(process_chat_queue(chat_id, bot))

@router.callback_query(TweetActionCallback.filter(F.action == "delete"))
async def handle_delete_media(callback: types.CallbackQuery, callback_data: TweetActionCallback):
    try: await callback.message.delete()
    except Exception: pass
    try: 
        if callback.message.reply_to_message: await callback.message.reply_to_message.delete()
    except Exception: pass
    try: 
        await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=callback_data.user_msg_id)
    except Exception: pass
    await callback.answer("تم الحذف.")
