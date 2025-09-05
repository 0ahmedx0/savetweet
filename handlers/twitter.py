# handlers/twitter.py
import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urlparse
import mimetypes
import logging

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

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Session Manager (reuse a single aiohttp session) ---
_session: Optional[aiohttp.ClientSession] = None

def _default_headers() -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }

def _get_session() -> aiohttp.ClientSession:
    """
    PATCH: إعادة استخدام جلسة aiohttp واحدة لتقليل الـ overhead وتحسين الأداء.
    """
    global _session
    if _session and not _session.closed:
        return _session
    timeout = aiohttp.ClientTimeout(total=120, connect=30, sock_read=120)
    _session = aiohttp.ClientSession(timeout=timeout, headers=_default_headers())
    return _session

async def _close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None

# --- Helper Functions (No changes here, مع تعديلات داخلية طفيفة) ---
async def extract_tweet_ids(text: str) -> Optional[List[str]]:
    # PATCH: دعم mobile.twitter.com + موازاة فك t.co
    url_pattern = r'https?://(?:(?:www\.)?(?:twitter|x|mobile\.twitter)\.com/\S+/status/\d+|t\.co/\S+)'
    urls = re.findall(url_pattern, text)
    if not urls: return None

    session = _get_session()

    async def resolve(url: str) -> Optional[str]:
        current_tweet_id = None
        if 't.co/' in url:
            try:
                # PATCH: استخدم GET بدل HEAD لأن بعض النواقل تمنع HEAD
                async with session.get(url, allow_redirects=True) as response:
                    match = re.search(r'/status/(\d+)', str(response.url))
                    if match: current_tweet_id = match.group(1)
            except Exception as e:
                logger.warning("Could not resolve %s: %s", url, e)
                return None
        else:
            match = re.search(r'/status/(\d+)', url)
            if match: current_tweet_id = match.group(1)
        return current_tweet_id

    ids = await asyncio.gather(*(resolve(u) for u in urls))
    ordered_unique_ids, seen_ids = [], set()
    for tid in ids:
        if tid and tid not in seen_ids:
            ordered_unique_ids.append(tid)
            seen_ids.add(tid)
    return ordered_unique_ids if ordered_unique_ids else None

def _unique_media_path(base_dir: Path, media_url: str) -> Path:
    """
    PATCH: توليد اسم ملف فريد باستخدام uuid مع محاولة الحفاظ على الامتداد إن وُجد.
    """
    parsed = urlparse(media_url)
    name = Path(parsed.path).name
    name = name.split('?')[0]
    # خذ الامتداد إن موجود، وإلا يحدد لاحقًا من Content-Type
    ext = ''.join(Path(name).suffixes) if '.' in name else ''
    if ext and not ext.startswith('.'):
        ext = f'.{ext}'
    return base_dir / f"{uuid.uuid4().hex}{ext}"

def _trim_caption(text: str, limit: int = 1024) -> str:
    """
    PATCH: قصّ الكابشن لا يتجاوز حدود تيليجرام.
    """
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"

def _escape_markdown_v2(text: str) -> str:
    """
    <<< الإصلاح الحاسم: توحيد MarkdownV2 >>>
    نهرب كل محارف MarkdownV2 حسب دليل تيليجرام.
    """
    if not text:
        return ""
    # القائمة الرسمية لمحارف MarkdownV2 الخاصة
    special = r'_\*\[\]\(\)~`>#+\-=|{}\.!'
    return re.sub(f'([{re.escape(special)}])', r'\\\1', text)

async def ytdlp_download_tweet_video(tweet_id: str, out_dir: Path) -> Optional[Path]:
    # PATCH: تقوية yt-dlp: رؤوس متصفح، محاولات أكثر، وتجربة x.com ثم twitter.com + مهلة + تحقّق وجود yt-dlp
    import shutil as _shutil
    if _shutil.which('yt-dlp') is None:
        logger.warning("yt-dlp not found in PATH")
        return None

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
    if config.X_COOKIES:
        common.extend(['--cookies', str(config.X_COOKIES)])
    else:
        # ملاحظة: cookies-from-browser قد لا تعمل في الخوادم بلا بروفايل متصفح
        common.extend(['--cookies-from-browser', 'chrome'])

    last_err = ""
    for url in base_urls:
        # لو في ملف جزئي سابق لنفس المسار، امسحه قبل المحاولة
        if output_path.exists():
            try: output_path.unlink()
            except Exception: pass
        cmd = [*common, url]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            except asyncio.TimeoutError:
                process.kill()
                logger.warning("yt-dlp timed out for %s", url)
                continue
        except Exception as e:
            logger.error("yt-dlp exec failed for %s: %s", url, e)
            continue

        err = stderr.decode(errors="ignore")
        if process.returncode == 0 and output_path.exists():
            return output_path
        last_err = err
        # PATCH: إذا كان JSONDecodeError — جرب الرابط التالي
        if "JSONDecodeError" in err or "Failed to parse JSON" in err:
            continue
        # إذا ما في فيديو، لا داعي لإعادة المحاولة
        if "No video could be found" in err:
            break

    if last_err and "No video could be found" not in last_err:
        logger.warning("yt-dlp failed for tweet %s: %s", tweet_id, last_err)
    return None

async def scrape_media(tweet_id: str) -> Optional[dict]:
    api_url = f"https://api.vxtwitter.com/i/status/{tweet_id}"
    try:
        session = _get_session()
        async with session.get(api_url) as response:
            if response.status == 200:
                data = await response.json()
                # PATCH: حواجز بنية + اختيار أفضل variant
                media_items = data.get("media_extended") or []
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
                data.setdefault("tweetURL", f"https://x.com/i/status/{tweet_id}")
                data.setdefault("id", tweet_id)
                return data
            return None
    except Exception as e:
        logger.warning("vxtwitter scrape failed for %s: %s", tweet_id, e)
        return None

async def download_media(session: aiohttp.ClientSession, media_url: str, file_path: Path) -> bool:
    # PATCH: retries + backoff + استنتاج الامتداد من Content-Type عند اللزوم
    async with download_semaphore:
        backoffs = [0, 1, 2, 4]
        last_exc = None
        for delay in backoffs:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with session.get(media_url, timeout=aiohttp.ClientTimeout(total=180)) as response:
                    if response.status == 200:
                        # استنتج الامتداد إذا كان المسار بلا امتداد
                        if not file_path.suffix:
                            ctype = response.headers.get("Content-Type", "")
                            guessed = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ""
                            if guessed:
                                try:
                                    file_path = file_path.with_suffix(guessed)
                                except Exception:
                                    pass
                        with open(file_path, "wb") as f:
                            async for chunk in response.content.iter_chunked(8192):
                                f.write(chunk)
                        return True
            except Exception as e:
                last_exc = e
                continue
        if last_exc:
            logger.warning("download_media failed for %s: %s", media_url, last_exc)
    return False

async def send_large_file_pyro(file_path: Path, caption: Optional[str] = None, parse_mode: str = "Markdown", markup: Optional[InlineKeyboardMarkup] = None):
    app = PyroClient("user_bot", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.PYRO_SESSION_STRING, in_memory=True)
    try:
        await app.start()
        from pyrogram.enums import ParseMode as PyroParseMode
        # PATCH: عند تمرير "MarkdownV2" نخليها بدون parse لتفادي تعارض V2
        if parse_mode and parse_mode.lower() in ("markdownv2", "markdown_v2"):
            pyro_parse_mode = None
        elif parse_mode and parse_mode.lower() == "markdown":
            pyro_parse_mode = PyroParseMode.MARKDOWN
        else:
            pyro_parse_mode = PyroParseMode.HTML
        await app.send_video(config.CHANNEL_ID, str(file_path), caption=caption, parse_mode=pyro_parse_mode, reply_markup=markup)
        await app.stop()
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            # إعادة الإرسال بعد الانتظار
            await app.send_video(config.CHANNEL_ID, str(file_path), caption=caption, parse_mode=pyro_parse_mode, reply_markup=markup)
        finally:
            try:
                await app.stop()
            except Exception:
                pass
    except Exception as e:
        logger.error("Pyrogram failed: %s", e)
        # PATCH: is_connected خاصية وليست awaitable
        try:
            if getattr(app, "is_connected", False):
                await app.stop()
        except Exception:
            pass

# --- دالة تضمن ظهور الكيبورد دائمًا (لا تطنيش) ---
async def ensure_reply_markup(bot: Bot, base_message: Message, reply_markup: InlineKeyboardMarkup):
    """
    يحاول تعديل المارك-أب؛ وإن قال تيليجرام 'message is not modified'،
    يرسل رسالة جديدة مع نفس الكيبورد ويُكمل بهدوء (بدون رمي استثناء).
    """
    try:
        await bot.edit_message_reply_markup(
            chat_id=base_message.chat.id,
            message_id=base_message.message_id,
            reply_markup=reply_markup
        )
    except TelegramBadRequest as e:
        msg = (e.message or "").lower()
        if "message is not modified" in msg:
            # أرسل رسالة جديدة بالكيبورد بدل التعديل
            await base_message.reply("🔗 الخيارات:", reply_markup=reply_markup, disable_web_page_preview=True)
            # تم التعامل بدون رمي استثناء
        else:
            # أخطاء أخرى (غير 'not modified') يجب أن تظهر
            raise

# --- Functions with CRUCIAL FIX ---
def escape_markdown(text: str) -> str:
    """
    <<< الإصلاح الحاسم هنا: إعادة دالة تهريب الرموز >>>
    تم توحيدها على MarkdownV2 لتتلاءم مع parse_mode الموحد.
    """
    return _escape_markdown_v2(text)

def format_caption(tweet_data: dict) -> str:
    # PATCH: استخدام MarkdownV2 مع تهريب النصوص
    user_name = escape_markdown(tweet_data.get("user_name", "Unknown"))
    user_screen_name = escape_markdown(tweet_data.get("user_screen_name", "unknown"))
    # غامق في V2: *...* و inline code في V2: `...` (نهربها مسبقًا)
    return _trim_caption(f"🐦 *بواسطة:* {user_name} \\(`@{user_screen_name}`\\)")

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
        # <<< تطبيق الإصلاح هنا قبل الإرسال (MarkdownV2) >>>
        safe_text = escape_markdown(tweet_text)
        await last_media_message.reply(f"📝 *نص التغريدة:*\n\n{safe_text}", parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)

async def process_single_tweet(message: Message, tweet_id: str, settings: Dict):
    temp_dir = config.OUTPUT_DIR / str(uuid.uuid4())
    temp_dir.mkdir()
    bot: Bot = message.bot
    last_sent_message: Optional[Message] = None
    
    try:
        video_path = await ytdlp_download_tweet_video(tweet_id, temp_dir)
        if video_path:
            tweet_url = f"https://x.com/i/status/{tweet_id}"
            # PATCH: لتفادي اختلافات Markdown بين البوت و Pyrogram، نخلي الكابتشن بسيط بدون تنسيق
            caption_plain = _trim_caption(f"🐦 فيديو من تويتر: {tweet_url}")
            keyboard = create_inline_keyboard({"tweetURL": tweet_url, "id": tweet_id}, user_msg_id=message.message_id)
            if video_path.stat().st_size > config.MAX_FILE_SIZE:
                await send_large_file_pyro(video_path, caption_plain, "MarkdownV2", keyboard)
                last_sent_message = await message.reply("✅ تم رفع الفيديو بنجاح للقناة.", reply_markup=keyboard)
            else:
                last_sent_message = await message.reply_video(FSInputFile(video_path), caption=caption_plain, reply_markup=keyboard)
            
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

        media_items = tweet_data.get("media_extended", [])
        session = _get_session()

        # PATCH: جهّز مسارات فريدة لكل عنصر واربطها بالعنصر نفسه
        download_plan = []
        for item in media_items:
            url = item.get('url')
            if not url:
                continue
            target_path = _unique_media_path(temp_dir, url)
            item['_local_path'] = target_path  # اربط المسار بالعُنصر
            download_plan.append((url, target_path))

        # نزّل بالتوازي مع retries
        tasks = [download_media(session, url, path) for url, path in download_plan]
        results = await asyncio.gather(*tasks)

        photos, videos = [], []
        for item, ok in zip(media_items, results):
            if not ok:
                continue
            path = item['_local_path']
            mtype = item.get('type')
            if mtype == 'image':
                photos.append(path)
            elif mtype in ('video', 'gif'):
                videos.append(path)
        
        if photos:
            photo_groups = [photos[i:i + 5] for i in range(0, len(photos), 5)]
            for i, group in enumerate(photo_groups):
                media_group = [
                    InputMediaPhoto(
                        media=FSInputFile(p),
                        caption=caption if i == 0 else None,
                        parse_mode=ParseMode.MARKDOWN_V2
                    ) for p in group
                ]
                if not media_group: continue
                sent_messages = await message.reply_media_group(media_group)
                last_sent_message = sent_messages[-1]
                if keyboard:
                    # PATCH: ضمان ظهور الكيبورد حتى لو فشل التعديل
                    await ensure_reply_markup(bot, last_sent_message, keyboard)
        
        for video_path in videos:
            # سنستخدم نفس caption_plain لتفادي مشاكل V2 على الفيديوهات الفردية؟ هنا نحن عبر Bot API، فيعمل V2.
            # لكن caption من format_caption فيه تنسيق V2، استخدمه هنا.
            if video_path.stat().st_size > config.MAX_FILE_SIZE:
                caption_plain = _trim_caption(f"🐦 فيديو من تويتر: https://x.com/i/status/{tweet_id}")
                await send_large_file_pyro(video_path, caption_plain, "MarkdownV2", keyboard)
                last_sent_message = await message.reply("✅ تم رفع الفيديو بنجاح للقناة.", reply_markup=keyboard)
            else:
                last_sent_message = await message.reply_video(FSInputFile(video_path), caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)

        if last_sent_message and settings.get("send_text"):
            await send_tweet_text_reply(message, last_sent_message, tweet_data)

    finally:
        if temp_dir.exists(): shutil.rmtree(temp_dir, ignore_errors=True)

# --- Queue and Handler Logic with Fixes ---
async def process_chat_queue(chat_id: int, bot: Bot):
    queue = chat_queues.get(chat_id)
    if not queue: active_workers.discard(chat_id); return
    while not queue.empty():
        message, tweet_ids, progress_msg = await queue.get()
        settings = await get_user_settings(message.from_user.id)
        try:
            total = len(tweet_ids)
            # PATCH: منع تكرار نفس النص حرفيًا + استبدال الرسالة عند "not modified"
            last_progress_text = None
            for i, tweet_id in enumerate(tweet_ids, 1):
                try:
                    progress_text = f"⏳ جاري معالجة الرابط *{i}* من *{total}*"
                    if progress_text != last_progress_text:
                        try:
                            await progress_msg.edit_text(progress_text, parse_mode=ParseMode.MARKDOWN_V2)
                            last_progress_text = progress_text
                        except TelegramBadRequest as e:
                            if "message is not modified" in (e.message or "").lower():
                                # ننشئ رسالة جديدة ونحوّل المؤشّر لها
                                progress_msg = await message.reply(progress_text, parse_mode=ParseMode.MARKDOWN_V2)
                                last_progress_text = progress_text
                            else:
                                raise
                    await process_single_tweet(message, tweet_id, settings)
                except Exception as e: 
                    logger.error("Error processing tweet %s: %s", tweet_id, e)
            done_text = f"✅ اكتملت معالجة *{total}* روابط!"
            if done_text != last_progress_text:
                try:
                    await progress_msg.edit_text(done_text, parse_mode=ParseMode.MARKDOWN_V2)
                except TelegramBadRequest as e:
                    if "message is not modified" in (e.message or "").lower():
                        # ننشئ رسالة جديدة للإنهاء ونحوّل المؤشّر لها لمرحلة الحذف لاحقًا
                        progress_msg = await message.reply(done_text, parse_mode=ParseMode.MARKDOWN_V2)
                    else:
                        raise
            await asyncio.sleep(5); 
            try:
                await progress_msg.delete()
            except Exception:
                pass
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
    progress_msg = await message.reply(f"تم استلام *{len(tweet_ids)}* روابط", parse_mode=ParseMode.MARKDOWN_V2)
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
