# handlers/twitter.py
import asyncio
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import List, Dict, Optional

import aiohttp
from aiogram import Bot, Router, F, types
from aiogram.enums import ChatAction, ParseMode
from aiogram.types import (FSInputFile, InputMediaPhoto, Message,
                           ReactionTypeEmoji, InlineKeyboardMarkup, InlineKeyboardButton)
from pyrogram import Client as PyroClient
from pyrogram.errors import FloodWait

import config

router = Router()
chat_queues: Dict[int, asyncio.Queue] = {}
active_workers: set[int] = set()
download_semaphore = asyncio.Semaphore(4)

# --- Helper Functions ---

def _get_session() -> aiohttp.ClientSession:
    """إنشاء وإرجاع جلسة aiohttp مع مهلة محددة."""
    timeout = aiohttp.ClientTimeout(total=45)
    return aiohttp.ClientSession(timeout=timeout)


async def extract_tweet_ids(text: str) -> Optional[List[str]]:
    """
    استخراج جميع معرفات التغريدات الفريدة من النص، مع فك روابط t.co،
    والحفاظ على ترتيب ظهورها الأصلي في الرسالة.
    """
    url_pattern = r'https?://(?:(?:www\.)?(?:twitter|x)\.com/\S+/status/\d+|t\.co/\S+)'
    urls = re.findall(url_pattern, text)
    if not urls:
        return None

    ordered_unique_ids = []
    seen_ids = set()
    
    async with _get_session() as session:
        for url in urls:
            current_tweet_id = None
            
            if 't.co/' in url:
                try:
                    async with session.head(url, allow_redirects=True) as response:
                        final_url = str(response.url)
                        match = re.search(r'/status/(\d+)', final_url)
                        if match:
                            current_tweet_id = match.group(1)
                except Exception as e:
                    print(f"Could not resolve t.co link {url}: {e}")
                    continue
            else:
                match = re.search(r'/status/(\d+)', url)
                if match:
                    current_tweet_id = match.group(1)

            if current_tweet_id and current_tweet_id not in seen_ids:
                ordered_unique_ids.append(current_tweet_id)
                seen_ids.add(current_tweet_id)

    return ordered_unique_ids if ordered_unique_ids else None


async def ytdlp_download_tweet_video(tweet_id: str, out_dir: Path) -> Optional[Path]:
    """محاولة تنزيل الفيديو باستخدام yt-dlp."""
    tweet_url = f"https://x.com/i/status/{tweet_id}"
    output_path = out_dir / f"{tweet_id}.mp4"
    
    cmd = ['yt-dlp', '--quiet', '-f', 'bv*+ba/best', '--merge-output-format', 'mp4',
           '--retries', '3', '--fragment-retries', '3', '-o', str(output_path), tweet_url]

    if config.X_COOKIES:
        cmd.extend(['--cookies', str(config.X_COOKIES)])

    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()

    if process.returncode == 0 and output_path.exists():
        print(f"yt-dlp successfully downloaded video for tweet {tweet_id}")
        return output_path
    else:
        # Don't print error if it's just "no video found", it's expected behavior
        if "No video could be found" not in stderr.decode():
            print(f"yt-dlp failed for tweet {tweet_id}: {stderr.decode()}")
        return None

async def scrape_media(tweet_id: str) -> Optional[dict]:
    """جلب بيانات الوسائط من vxtwitter API."""
    api_url = f"https://api.vxtwitter.com/i/status/{tweet_id}"
    try:
        async with _get_session() as session, session.get(api_url) as response:
            if response.status == 200:
                data = await response.json()
                return data if data.get("media_extended") else None
            return None
    except Exception as e:
        print(f"vxtwitter scrape failed for {tweet_id}: {e}")
        return None

async def download_media(session: aiohttp.ClientSession, media_url: str, file_path: Path) -> bool:
    """تنزيل ملف واحد بشكل غير متزامن."""
    async with download_semaphore:
        try:
            async with session.get(media_url) as response:
                if response.status == 200:
                    with open(file_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                    return True
        except Exception:
            pass
    return False


async def send_large_file_pyro(file_path: Path, caption: Optional[str] = None, parse_mode: str = "Markdown", markup: Optional[InlineKeyboardMarkup] = None):
    """إرسال الملفات الكبيرة إلى القناة المحددة باستخدام Pyrogram."""
    print(f"File > 50MB. Uploading via Pyrogram...")
    app = PyroClient("user_bot", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.PYRO_SESSION_STRING, in_memory=True)
    try:
        await app.start()
        # Pyrogram uses different parse mode enums, so we adapt
        from pyrogram.enums import ParseMode as PyroParseMode
        pyro_parse_mode = PyroParseMode.MARKDOWN if parse_mode and parse_mode.lower() == "markdown" else PyroParseMode.HTML

        await app.send_video(
            chat_id=config.CHANNEL_ID,
            video=str(file_path),
            caption=caption,
            parse_mode=pyro_parse_mode,
            reply_markup=markup
        )
        await app.stop()
        print("Successfully uploaded large file to channel.")
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await send_large_file_pyro(file_path, caption, parse_mode, markup)
    except Exception as e:
        print(f"Pyrogram failed to send file: {e}")
        if await app.is_connected:
            await app.stop()

def format_caption(tweet_data: dict) -> str:
    """تنسيق الكابشن الجديد"""
    user_name = tweet_data.get("user_name", "Unknown")
    user_screen_name = tweet_data.get("user_screen_name", "unknown")
    
    # Simple escape for markdown characters in names
    user_name = user_name.replace('_', r'\_').replace('*', r'\*').replace('[', r'\[').replace('`', r'\`')

    return f"🐦 **من حساب:** {user_name} (`@{user_screen_name}`)"


def create_inline_keyboard(tweet_data: dict) -> InlineKeyboardMarkup:
    """إنشاء أزرار تفاعلية"""
    tweet_url = tweet_data.get("tweetURL", "")
    if not tweet_url: return None
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 الرابط الأصلي", url=tweet_url)]
    ])
    return keyboard


async def process_single_tweet(message: Message, tweet_id: str):
    """المعالجة الكاملة لتغريدة واحدة."""
    temp_dir = config.OUTPUT_DIR / str(uuid.uuid4())
    temp_dir.mkdir()
    bot: Bot = message.bot
    
    try:
        # 1. Try yt-dlp first for videos (best quality & protected tweets)
        video_path = await ytdlp_download_tweet_video(tweet_id, temp_dir)
        if video_path:
            # yt-dlp doesn't give us user data, so we build a simple caption and keyboard
            tweet_url = f"https://x.com/i/status/{tweet_id}"
            caption = f"🐦 [تغريدة الفيديو]({tweet_url})"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 الرابط الأصلي", url=tweet_url)]])
            
            if video_path.stat().st_size > config.MAX_FILE_SIZE:
                await send_large_file_pyro(video_path, caption, "Markdown", keyboard)
                await message.reply("✅ تم رفع الفيديو بنجاح للقناة.", reply_markup=keyboard)
            else:
                await message.reply_video(FSInputFile(video_path), caption=caption, parse_mode="Markdown", reply_markup=keyboard)
            return

        # 2. Fallback to vxtwitter API for everything else
        tweet_data = await scrape_media(tweet_id)
        if not tweet_data:
            await message.reply(f"لم أتمكن من العثور على وسائط للتغريدة:\nhttps://x.com/i/status/{tweet_id}")
            return
        
        caption = format_caption(tweet_data)
        keyboard = create_inline_keyboard(tweet_data)

        # Download all media
        media_items = tweet_data.get("media_extended", [])
        photos, videos = [], []
        async with _get_session() as session:
            tasks = []
            for item in media_items:
                file_path = temp_dir / Path(item['url']).name.split('?')[0]
                if item['type'] == 'image': photos.append(file_path)
                elif item['type'] in ['video', 'gif']: videos.append(file_path)
                tasks.append(download_media(session, item['url'], file_path))
            await asyncio.gather(*tasks)

        # Send Photos
        if photos:
            photo_groups = [photos[i:i + 5] for i in range(0, len(photos), 5)]
            for i, group in enumerate(photo_groups):
                media_group = [InputMediaPhoto(media=FSInputFile(p)) for p in group if p.exists()]
                if not media_group: continue
                # Add caption only to the first photo of the first group
                if i == 0:
                    media_group[0].caption = caption
                    media_group[0].parse_mode = "Markdown"
                sent_message = await message.reply_media_group(media_group)
                # Add keyboard to the last message of the media group
                if keyboard:
                    await bot.edit_message_reply_markup(chat_id=sent_message[0].chat.id, message_id=sent_message[-1].message_id, reply_markup=keyboard)
        
        # Send Videos
        for video_path in videos:
            if not video_path.exists(): continue

            if video_path.stat().st_size > config.MAX_FILE_SIZE:
                await send_large_file_pyro(video_path, caption, "Markdown", keyboard)
                await message.reply("✅ تم رفع الفيديو بنجاح للقناة.", reply_markup=keyboard)
            else:
                await message.reply_video(FSInputFile(video_path), caption=caption, parse_mode="Markdown", reply_markup=keyboard)

    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


async def process_chat_queue(chat_id: int, bot: Bot):
    """Worker لمعالجة طابور الرسائل لمحادثة معينة."""
    queue = chat_queues.get(chat_id)
    if not queue:
        active_workers.discard(chat_id)
        return

    while not queue.empty():
        message, tweet_ids, progress_msg = await queue.get()
        try:
            total = len(tweet_ids)
            for i, tweet_id in enumerate(tweet_ids, 1):
                try:
                    await progress_msg.edit_text(f"⏳ جاري معالجة الرابط **{i}** من **{total}**...")
                    await process_single_tweet(message, tweet_id)
                except Exception as e:
                    print(f"Unhandled error processing tweet {tweet_id} in chat {chat_id}: {e}")
                    await bot.send_message(chat_id, f"حدث خطأ فادح أثناء معالجة التغريدة: {tweet_id}")
            
            await progress_msg.edit_text(f"✅ اكتملت معالجة **{total}** روابط!")
            await asyncio.sleep(5)
            await progress_msg.delete()
        except Exception as e:
            print(f"Fatal error in queue worker for chat {chat_id}: {e}")
            await progress_msg.edit_text("❌ حدث خطأ غير متوقع أثناء المعالجة.")
        finally:
            queue.task_done()

    active_workers.discard(chat_id)


@router.message(F.text & (F.text.contains("twitter.com") | F.text.contains("x.com") | F.text.contains("t.co")))
async def handle_twitter_links(message: types.Message, bot: Bot):
    """معالج الرسائل التي تحتوي على روابط X."""
    tweet_ids = await extract_tweet_ids(message.text)
    if not tweet_ids:
        return

    progress_msg = await message.reply(f"تم استلام **{len(tweet_ids)}** روابط. سيتم البدء في المعالجة...")
    
    chat_id = message.chat.id
    if chat_id not in chat_queues:
        chat_queues[chat_id] = asyncio.Queue()
    await chat_queues[chat_id].put((message, tweet_ids, progress_msg))
    
    try:
        await bot.set_message_reaction(chat_id, message.message_id, reaction=[ReactionTypeEmoji(emoji='👨‍💻')])
    except Exception:
        pass

    if chat_id not in active_workers:
        active_workers.add(chat_id)
        asyncio.create_task(process_chat_queue(chat_id, bot))
