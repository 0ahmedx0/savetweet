# handlers/twitter.py

# --- الإضافات والتحسينات ---
import logging
# --- نهاية الإضافات ---

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

# --- التحسين 1: استخدام نظام تسجيل احترافي بدلاً من print ---
logger = logging.getLogger(__name__)

router = Router()
chat_queues: Dict[int, asyncio.Queue] = {}
active_workers: set[int] = set()
download_semaphore = asyncio.Semaphore(4)


# --- Helper Functions (مع تحسينات) ---

def _get_session() -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(total=120, connect=30, sock_read=120)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    return aiohttp.ClientSession(timeout=timeout, headers=headers)

# --- التحسين 2: تسريع فك روابط t.co بمعالجتها بالتوازي ---
async def extract_tweet_ids(text: str) -> Optional[List[str]]:
    url_pattern = r'https?://(?:(?:www\.)?(?:twitter|x)\.com/\S+/status/\d+|t\.co/\S+)'
    urls = re.findall(url_pattern, text)
    if not urls:
        return None

    async def resolve_url(session: aiohttp.ClientSession, url: str) -> Optional[str]:
        if 't.co/' in url:
            try:
                # استخدم GET لأنه أكثر موثوقية من HEAD
                async with session.get(url, allow_redirects=True, timeout=15) as response:
                    match = re.search(r'/status/(\d+)', str(response.url))
                    return match.group(1) if match else None
            except Exception as e:
                logger.warning(f"Could not resolve t.co link {url}: {e}")
                return None
        else:
            match = re.search(r'/status/(\d+)', url)
            return match.group(1) if match else None

    async with _get_session() as session:
        tasks = [resolve_url(session, url) for url in urls]
        resolved_ids = await asyncio.gather(*tasks)

    ordered_unique_ids, seen_ids = [], set()
    for tweet_id in resolved_ids:
        if tweet_id and tweet_id not in seen_ids:
            ordered_unique_ids.append(tweet_id)
            seen_ids.add(tweet_id)

    return ordered_unique_ids if ordered_unique_ids else None

# --- التحسين 3: زيادة موثوقية yt-dlp ---
async def ytdlp_download_tweet_video(tweet_id: str, out_dir: Path) -> Optional[Path]:
    base_urls = [
        f"https://x.com/i/status/{tweet_id}",
        f"https://twitter.com/i/status/{tweet_id}",
    ]
    output_path = out_dir / f"{tweet_id}.mp4"
    common = [
        'yt-dlp', '--quiet',
        '-f', 'bv*+ba/best',
        '--merge-output-format', 'mp4',
        '--retries', '5', '--fragment-retries', '5',
        '--add-header', 'User-Agent: Mozilla/5.0',
        '--add-header', 'Accept-Language: en-US,en;q=0.9',
        '--no-warnings',
        '-o', str(output_path),
    ]

    # استخدام الكوكيز من الملف أو كخيار احتياطي من المتصفح
    if config.X_COOKIES and Path(config.X_COOKIES).exists():
        common.extend(['--cookies', str(config.X_COOKIES)])
    else:
        # يمكنك تغيير 'chrome' إلى 'firefox', 'edge', etc.
        common.extend(['--cookies-from-browser', 'chrome'])

    last_err = ""
    for url in base_urls:
        # حذف الملف الجزئي إذا كان موجوداً من محاولة سابقة فاشلة
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError as e:
                logger.error(f"Could not remove partial file {output_path}: {e}")
        
        cmd = [*common, url]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            # إضافة مهلة زمنية لمنع العملية من التعليق
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180.0)
            err = stderr.decode(errors="ignore")

            if process.returncode == 0 and output_path.exists():
                return output_path
            
            last_err = err
            if "JSONDecodeError" in err or "Failed to parse JSON" in err:
                continue
            if "No video could be found" in err:
                break
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.error(f"yt-dlp timed out for tweet {tweet_id}")
            last_err = "Timeout"
            break
        except Exception as e:
            logger.error(f"yt-dlp subprocess error for tweet {tweet_id}: {e}")
            last_err = str(e)

    if last_err and "No video could be found" not in last_err:
        logger.error(f"yt-dlp failed for tweet {tweet_id}: {last_err}")
    return None

# --- التحسين 4: تحصين الكود ضد تغيرات API ---
async def scrape_media(tweet_id: str) -> Optional[dict]:
    api_url = f"https://api.vxtwitter.com/i/status/{tweet_id}"
    try:
        async with _get_session() as session, session.get(api_url) as response:
            if response.status == 200:
                data = await response.json()
                # التأكد من وجود الحقول الأساسية وتعيين قيم افتراضية
                data.setdefault("tweetURL", f"https://x.com/i/status/{tweet_id}")
                data.setdefault("id", tweet_id)

                media_items = data.get("media_extended") or []
                # التأكد من أن media_items هو قائمة دائماً
                if not isinstance(media_items, list):
                    media_items = []
                    
                for m in media_items:
                    if m.get("type") in ("video", "gif") and isinstance(m.get("variants"), list):
                        best = max(
                            (v for v in m["variants"] if v.get("url") and str(v.get("content_type","")).endswith("mp4")),
                            key=lambda v: v.get("bitrate", 0),
                            default=None
                        )
                        if best: m["url"] = best["url"]
                
                # إعادة تعيين القائمة المعالجة
                data["media_extended"] = media_items
                return data
            return None
    except Exception as e:
        logger.exception(f"vxtwitter scrape failed for {tweet_id}: {e}")
        return None

# --- التحسين 5: إضافة إعادة محاولات للتنزيل وتعقيم اسم الملف ---
def _safe_filename(url: str) -> str:
    """ينظف اسم الملف من الـ URL."""
    name = Path(url).name.split('?')[0]
    return re.sub(r'[^a-zA-Z0-9._-]', '_', name)

async def download_media(session: aiohttp.ClientSession, media_url: str, file_path: Path, retries: int = 3) -> bool:
    async with download_semaphore:
        for attempt in range(retries):
            try:
                async with session.get(media_url) as response:
                    if response.status == 200:
                        with open(file_path, "wb") as f:
                            async for chunk in response.content.iter_chunked(8192):
                                f.write(chunk)
                        return True
                    else:
                        logger.warning(f"Failed to download {media_url}, status: {response.status}")
            except Exception as e:
                logger.warning(f"Download attempt {attempt+1}/{retries} for {media_url} failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(1.5)  # انتظر قليلاً قبل إعادة المحاولة
    return False

# --- التحسين 6: تحسين معالجة Pyrogram ---
async def send_large_file_pyro(file_path: Path, caption: Optional[str] = None, markup: Optional[InlineKeyboardMarkup] = None):
    # ملاحظة: إنشاء العميل لكل مرة ليس الأمثل أداءً، لكنه آمن.
    # الحل المتقدم هو استخدام Singleton client.
    app = PyroClient("user_bot", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.PYRO_SESSION_STRING, in_memory=True)
    try:
        await app.start()
        from pyrogram.enums import ParseMode as PyroParseMode
        # استخدام MarkdownV2 بشكل متسق
        pyro_parse_mode = PyroParseMode.MARKDOWN
        
        # تحسين معالجة FloodWait لتجنب الاستدعاء الذاتي الغير محدود
        while True:
            try:
                await app.send_video(
                    config.CHANNEL_ID,
                    str(file_path),
                    caption=caption,
                    parse_mode=pyro_parse_mode,
                    reply_markup=markup
                )
                break # الخروج من الحلقة عند النجاح
            except FloodWait as e:
                logger.warning(f"Pyrogram FloodWait: sleeping for {e.value} seconds.")
                await asyncio.sleep(e.value + 2) # الانتظار للمدة المطلوبة + ثانيتين
            
    except Exception as e:
        logger.exception(f"Pyrogram failed to send large file: {e}")
    finally:
        try:
            if getattr(app, "is_connected", False):
                await app.stop()
        except Exception as e:
            logger.error(f"Failed to stop pyrogram client: {e}")


async def ensure_reply_markup(bot: Bot, base_message: Message, reply_markup: InlineKeyboardMarkup):
    try:
        await bot.edit_message_reply_markup(
            chat_id=base_message.chat.id,
            message_id=base_message.message_id,
            reply_markup=reply_markup
        )
    except TelegramBadRequest as e:
        msg = (e.message or "").lower()
        if "message is not modified" in msg:
            await base_message.reply("🔗 *الخيارات:*", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            raise

# --- Functions with CRUCIAL FIX ---
# --- التحسين 7: الانتقال الكامل إلى MarkdownV2 ---
def escape_markdown_v2(text: str) -> str:
    """دالة تهريب الرموز المتوافقة مع MarkdownV2."""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def format_caption(tweet_data: dict) -> str:
    user_name = escape_markdown_v2(tweet_data.get("user_name", "Unknown"))
    user_screen_name = escape_markdown_v2(tweet_data.get("user_screen_name", "unknown"))
    return f"🐦 *بواسطة:* {user_name} (`@{user_screen_name}`)"

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
        safe_text = escape_markdown_v2(tweet_text)
        await last_media_message.reply(f"📝 *نص التغريدة:*\n\n{safe_text}", parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)

async def process_single_tweet(message: Message, tweet_id: str, settings: Dict):
    temp_dir = config.OUTPUT_DIR / str(uuid.uuid4())
    temp_dir.mkdir()
    bot: Bot = message.bot
    last_sent_message: Optional[Message] = None
    
    try:
        video_path = await ytdlp_download_tweet_video(tweet_id, temp_dir)
        if video_path:
            # لا حاجة لتهريب الرابط في صيغة [text](URL)
            tweet_url = f"https://x.com/i/status/{tweet_id}"
            caption = f"🐦 [فيديو من تويتر]({tweet_url})"
            keyboard = create_inline_keyboard({"tweetURL": tweet_url, "id": tweet_id}, user_msg_id=message.message_id)
            
            if video_path.stat().st_size > config.MAX_FILE_SIZE:
                # Pyrogram uses its own Markdown parser, which is similar to V2
                await send_large_file_pyro(video_path, caption, keyboard)
                last_sent_message = await message.reply("✅ تم رفع الفيديو بنجاح للقناة\\.", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                last_sent_message = await message.reply_video(FSInputFile(video_path), caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
            
            if settings.get("send_text"):
                tweet_data = await scrape_media(tweet_id)
                if tweet_data: await send_tweet_text_reply(message, last_sent_message, tweet_data)
            return

        tweet_data = await scrape_media(tweet_id)
        if not tweet_data or not tweet_data.get("media_extended"):
            safe_url = escape_markdown_v2(f"https://x.com/i/status/{tweet_id}")
            await message.reply(f"لم أتمكن من العثور على وسائط للتغريدة:\n{safe_url}", parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        caption = format_caption(tweet_data)
        keyboard = create_inline_keyboard(tweet_data, user_msg_id=message.message_id)

        media_items = tweet_data.get("media_extended", [])
        photos, videos = [], []
        async with _get_session() as session:
            tasks = [download_media(session, item['url'], temp_dir / _safe_filename(item['url'])) for item in media_items]
            await asyncio.gather(*tasks)

        for item in media_items:
            path = temp_dir / _safe_filename(item['url'])
            if path.exists():
                if item['type'] == 'image': photos.append(path)
                elif item['type'] in ['video', 'gif']: videos.append(path)
        
        if photos:
            photo_groups = [photos[i:i + 5] for i in range(0, len(photos), 5)]
            for i, group in enumerate(photo_groups):
                media_group = [InputMediaPhoto(media=FSInputFile(p), caption=caption if i == 0 and j == 0 else None, parse_mode=ParseMode.MARKDOWN_V2) for j, p in enumerate(group)]
                if not media_group: continue
                sent_messages = await message.reply_media_group(media_group)
                last_sent_message = sent_messages[-1]
                if keyboard:
                    await ensure_reply_markup(bot, last_sent_message, keyboard)
        
        for video_path in videos:
            if video_path.stat().st_size > config.MAX_FILE_SIZE:
                await send_large_file_pyro(video_path, caption, keyboard)
                last_sent_message = await message.reply("✅ تم رفع الفيديو بنجاح للقناة\\.", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                last_sent_message = await message.reply_video(FSInputFile(video_path), caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)

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
            last_progress_text = None
            for i, tweet_id in enumerate(tweet_ids, 1):
                # تحديث رسالة التقدم في كتلة منفصلة
                progress_text = f"⏳ جاري معالجة الرابط *{i}* من *{total}*\\.\\.\\."
                if progress_text != last_progress_text:
                    try:
                        await progress_msg.edit_text(progress_text, parse_mode=ParseMode.MARKDOWN_V2)
                        last_progress_text = progress_text
                    except TelegramBadRequest as e:
                        if "message is not modified" not in (e.message or "").lower():
                            logger.error(f"Error updating progress message: {e}")
                
                # معالجة التغريدة في كتلة منفصلة للإبلاغ عن الأخطاء الحقيقية
                try:
                    await process_single_tweet(message, tweet_id, settings)
                except Exception as e:
                    logger.exception(f"Error genuinely processing tweet {tweet_id}: {e}")

            done_text = f"✅ اكتملت معالجة *{total}* روابط\\!"
            if done_text != last_progress_text:
                try:
                    await progress_msg.edit_text(done_text, parse_mode=ParseMode.MARKDOWN_V2)
                except TelegramBadRequest: pass
            
            await asyncio.sleep(5)
            await progress_msg.delete()
            
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
    progress_msg = await message.reply(f"تم استلام *{len(tweet_ids)}* روابط\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
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
