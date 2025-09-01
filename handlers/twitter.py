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
from aiogram.types import FSInputFile, InputMediaPhoto, Message, ReactionTypeEmoji
from pyrogram import Client as PyroClient
from pyrogram.errors import FloodWait

import config

# --- Globals & Setup ---
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
    """استخراج جميع معرفات التغريدات الفريدة من النص، مع فك روابط t.co."""
    url_pattern = r'https?://(?:www\.)?(?:twitter|x)\.com/\S+/status/(\d+)|https?://t\.co/\S+'
    matches = re.findall(url_pattern, text)
    if not matches:
        return None

    tweet_ids = set()
    unresolved_tco = []

    for match in matches:
        if match:
            tweet_ids.add(match)
        else:
            all_urls = re.findall(r'https?://\S+', text)
            for url in all_urls:
                if 't.co/' in url:
                    unresolved_tco.append(url)

    if unresolved_tco:
        async with _get_session() as session:
            tasks = [session.head(url, allow_redirects=True) for url in set(unresolved_tco)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, aiohttp.ClientResponse):
                    final_url = str(res.url)
                    tweet_id_match = re.search(r'/status/(\d+)', final_url)
                    if tweet_id_match:
                        tweet_ids.add(tweet_id_match.group(1))

    return list(tweet_ids) if tweet_ids else None


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
        print(f"yt-dlp failed for tweet {tweet_id}: {stderr.decode()}")
        return None

async def scrape_media(tweet_id: str) -> Optional[dict]:
    """جلب بيانات الوسائط من vxtwitter API."""
    api_url = f"https://api.vxtwitter.com/i/status/{tweet_id}"
    try:
        async with _get_session() as session, session.get(api_url) as response:
            if response.status == 200:
                try:
                    data = await response.json()
                    if not data.get("media_extended"):
                        print(f"No media found in vxtwitter API for {tweet_id}")
                        return None
                    return data
                except aiohttp.ContentTypeError:
                    error_html = await response.text()
                    error_match = re.search(r'<meta property="og:description" content="([^"]+)">', error_html)
                    if error_match:
                        print(f"vxtwitter API error for {tweet_id}: {error_match.group(1)}")
                    else:
                        print(f"vxtwitter API returned non-JSON for {tweet_id}")
                    return None
            else:
                print(f"vxtwitter API request failed for {tweet_id} with status: {response.status}")
                return None
    except Exception as e:
        print(f"Exception while scraping vxtwitter for {tweet_id}: {e}")
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
                    print(f"Successfully downloaded {file_path.name}")
                    return True
                else:
                    print(f"Failed to download {media_url}, status: {response.status}")
                    return False
        except Exception as e:
            print(f"Exception during download of {media_url}: {e}")
            return False


async def send_large_file_pyro(file_path: Path, caption: Optional[str] = None):
    """إرسال الملفات الكبيرة إلى القناة المحددة باستخدام Pyrogram."""
    print(f"File {file_path.name} is larger than 50MB. Uploading via Pyrogram...")
    app = PyroClient("user_bot", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.PYRO_SESSION_STRING, in_memory=True)
    try:
        await app.start()
        await app.send_video(chat_id=config.CHANNEL_ID, video=str(file_path), caption=caption)
        await app.stop()
        print("Successfully uploaded large file to channel.")
    except FloodWait as e:
        print(f"Pyrogram FloodWait: sleeping for {e.value} seconds.")
        await asyncio.sleep(e.value)
        await send_large_file_pyro(file_path, caption)
    except Exception as e:
        print(f"Pyrogram failed to send file: {e}")
        if await app.is_connected:
            await app.stop()

async def process_single_tweet(message: Message, tweet_id: str):
    """المعالجة الكاملة لتغريدة واحدة."""
    temp_dir = config.OUTPUT_DIR / str(uuid.uuid4())
    temp_dir.mkdir()
    bot: Bot = message.bot
    
    try:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        
        video_path = await ytdlp_download_tweet_video(tweet_id, temp_dir)
        if video_path:
            file_size = video_path.stat().st_size
            caption = f"https://x.com/i/status/{tweet_id}"
            if file_size > config.MAX_FILE_SIZE:
                await send_large_file_pyro(video_path, caption)
                await message.reply("✅ تم رفع الفيديو بنجاح إلى القناة لأنه أكبر من 50 ميغابايت.")
            else:
                await message.reply_video(FSInputFile(video_path), caption=caption)
            return

        tweet_data = await scrape_media(tweet_id)
        if not tweet_data or not tweet_data.get("media_extended"):
            await message.reply(f"لم أتمكن من العثور على وسائط للتغريدة:\nhttps://x.com/i/status/{tweet_id}")
            return
        
        media_items = tweet_data["media_extended"]
        photos, videos = [], []
        
        async with _get_session() as session:
            tasks = []
            for item in media_items:
                url = item.get("url")
                if not url: continue
                
                file_name = Path(url).name.split('?')[0]
                file_path = temp_dir / file_name
                
                if item["type"] == "image":
                    photos.append({"path": file_path, "caption": tweet_data.get("text", "")})
                elif item["type"] in ["video", "gif"]:
                    videos.append({"path": file_path, "caption": f"https://x.com/i/status/{tweet_id}"})
                tasks.append(download_media(session, url, file_path))

            await asyncio.gather(*tasks)

        photo_groups = [photos[i:i + 5] for i in range(0, len(photos), 5)]
        for i, group in enumerate(photo_groups):
            media_group = []
            for j, photo in enumerate(group):
                if not photo['path'].exists():
                    continue
                caption_to_set = photo["caption"] if i == 0 and j == 0 else None
                media_group.append(
                    InputMediaPhoto(media=FSInputFile(photo['path']), caption=caption_to_set, parse_mode=ParseMode.HTML)
                )
            if media_group:
                await message.reply_media_group(media_group)

        for video in videos:
            path = video["path"]
            if not path.exists(): continue
            
            file_size = path.stat().st_size
            if file_size > config.MAX_FILE_SIZE:
                await send_large_file_pyro(path, video["caption"])
                await message.reply("✅ تم رفع الفيديو بنجاح إلى القناة لأنه أكبر من 50 ميغابايت.")
            else:
                await message.reply_video(FSInputFile(path), caption=video["caption"])

    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            print(f"Cleaned up temporary directory: {temp_dir}")


async def process_chat_queue(chat_id: int, bot: Bot):
    """Worker لمعالجة طابور الرسائل لمحادثة معينة."""
    queue = chat_queues.get(chat_id)
    if not queue:
        active_workers.discard(chat_id)
        return

    while not queue.empty():
        message, tweet_ids = await queue.get()
        try:
            for tweet_id in tweet_ids:
                try:
                    await process_single_tweet(message, tweet_id)
                except Exception as e:
                    print(f"Unhandled error processing tweet {tweet_id} in chat {chat_id}: {e}")
                    try:
                        await bot.send_message(chat_id, f"حدث خطأ أثناء معالجة التغريدة: {tweet_id}")
                        await bot.set_message_reaction(chat_id, message.message_id, reaction=[ReactionTypeEmoji(emoji='👎')])
                    except Exception as reaction_err:
                         print(f"Could not send error message or reaction: {reaction_err}")
        finally:
            # --- التعديل الوحيد هنا ---
            # يتم استدعاء task_done() مرة واحدة فقط بعد الانتهاء من كل الروابط في الرسالة
            queue.task_done()

    active_workers.discard(chat_id)
    print(f"Worker for chat {chat_id} finished.")


@router.message(F.text & (F.text.contains("twitter.com") | F.text.contains("x.com") | F.text.contains("t.co")))
async def handle_twitter_links(message: types.Message, bot: Bot):
    """معالج الرسائل التي تحتوي على روابط X."""
    chat_id = message.chat.id
    tweet_ids = await extract_tweet_ids(message.text)
    if not tweet_ids:
        print("Handler triggered but no tweet IDs were extracted.")
        return

    if chat_id not in chat_queues:
        chat_queues[chat_id] = asyncio.Queue()
    await chat_queues[chat_id].put((message, tweet_ids))
    try:
        await bot.set_message_reaction(chat_id, message.message_id, reaction=[ReactionTypeEmoji(emoji='👨‍💻')])
    except Exception as e:
        print(f"Couldn't set reaction: {e}")

    if chat_id not in active_workers:
        active_workers.add(chat_id)
        asyncio.create_task(process_chat_queue(chat_id, bot))
