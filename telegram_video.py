import asyncio
import os
import re
import shutil
import logging
import json
import tempfile
import hashlib
from pathlib import Path
import aiohttp
import requests
import subprocess
import tempfile

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.types import LinkPreviewOptions
from aiogram.types.input_file import FSInputFile
import yt_dlp

from db_utils import log_user_start, log_chat_usage, log_activity, get_cached_video, save_cached_video, init_db

# Load environment variables
load_dotenv()

# Constants
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
INSTAGRAM_REELS_REGEX = r"https?://(?:www\.)?instagram\.com/(?:reel|p|share|stories)/[\w-]+(/?)(?:\?.*)?$"
YOUTUBE_SHORTS_REGEX = r"https?://(?:www\.)?youtube\.com/shorts/[\w-]+"
TWITTER_REGEX = r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[\w-]+/status/[\d]+"
TIKTOK_REGEX = r"https?://(?:www\.)?(?:m\.)?(?:(?:vt|vm)\.)?tiktok\.com/(?:@[\w.-]+/video/\d+|t/[\w]+|[\w/?=&.-]+)(?:\?.*)?$"
THREADS_REGEX = r"https?://(?:www\.)?(?:threads\.net|threads\.com)/(?:@[\w.-]+/post/[\w-]+).*$"

# Instagram via yt-dlp + cookiefile (може бути JSON export -> конвертуємо)
IG_YTDLP_COOKIES = os.getenv("IG_YTDLP_COOKIES", "")
IG_RATE_SECONDS = float(os.getenv("IG_RATE_SECONDS", "15"))
COOKIES_CACHE_DIR = os.getenv("COOKIES_CACHE_DIR", os.path.join(tempfile.gettempdir(), "bot_cookies"))

# TikTok via Cobalt
COBALT_API_URL = os.getenv("COBALT_API_URL", "")  # напр. http://192.168.2.204:9000/
COBALT_TIMEOUT_SECONDS = float(os.getenv("COBALT_TIMEOUT_SECONDS", "120"))
COBALT_ALWAYS_PROXY = os.getenv("COBALT_ALWAYS_PROXY", "1") == "1"
COBALT_VIDEO_QUALITY = os.getenv("COBALT_VIDEO_QUALITY", "max")

# Anti-spam (throttling) settings
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "10"))
user_last_request_time = {}

IGNORED_CHATS_FOR_TIKTOK = (-1001152694757, -1001365731544)  # ДЯЧан, Пацанська будка
# IGNORED_CHATS_FOR_TIKTOK = (1,2) # ДЯЧан, Пацанська будка

START_MESSAGE_NON_ADMIN = (
    "🖖 Привіт, мене звати Кортес.\n\n"
    "Я допоможу тобі інтегрувати відео із Instagram Reels, YouTube Short, Twitter та Тікток в Telegram. Просто присилай мені посилання на відео у форматі:\n"
    "https://www.instagram.com/reel/XXX\n"
    "https://www.youtube.com/shorts/XXX\n"
    "https://twitter.com/user/status/XXX\n"
    "https://vm.tiktok.com/XXX\n"
    "https://www.threads.net/@user/post/XXX\n\n"
    "Я його завантажу і пришлю тобі телеграм повідомленням. Завантаження займає певний час, тож будь терплячим.\n\n"
    "Якщо хочеш, щоб я працював у групі - додай мене туди і дай права адміна. На жаль, за правилами Telegram я не буду бачити повідомлення учасників без прав адміна.\n\n"
    "Якщо хочеш, щоб я видаляв повідомлення з посиланнями, які перетоврю на відео - то дай мені права на видалення повідолмень.\n\n"
    "Слідкуй за оновленнями та за іншими розробками на [каналі автора](https://t.me/knemchenko_log). Ви також можете [підтримати проект фінансово](https://send.monobank.ua/jar/3ekUcZV1iR), але робіть це після того як задонатите на ЗСУ."
)

# Configure logging
try:
    import systemd.journal

    journal_handler = systemd.journal.JournalHandler()
    journal_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(journal_handler)
    logging.getLogger().setLevel(logging.INFO)
except ImportError:
    logging.basicConfig(
        filename='bot.log',
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

logger = logging.getLogger(__name__)

# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN)
router = Router()
dp = Dispatcher()
dp.include_router(router)

from urllib.parse import urlparse, urlunparse


def _clean_url(u: str) -> str:
    return (u or "").strip().rstrip(").,]}>\"'")


def _force_www_tiktok(u: str) -> str:
    try:
        p = urlparse(u)
        if p.netloc.lower() == "tiktok.com":
            return urlunparse(p._replace(netloc="www.tiktok.com"))
    except Exception:
        pass
    return u


async def _resolve_final_url(u: str, session: aiohttp.ClientSession, timeout_s: float = 12) -> str:
    # Резолвимо vt/vm short links до фінального URL
    try:
        async with session.get(
                u,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
                headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            return str(r.url)
    except Exception:
        return u


def extract_shortcode(url: str) -> str:
    """Extract shortcode from Instagram Reel URL."""
    return url.split("/reel/")[1].split("/")[0]


def locate_video_file(directory: str) -> str:
    """Locate the downloaded video file in the specified directory."""
    return next((file for file in os.listdir(directory) if file.endswith(".mp4")), None)


def _ensure_cookiefile_for_ytdlp(cookies_file: str, *, prefix: str = "ig") -> str:
    """
    yt-dlp очікує Netscape cookies.txt.
    Якщо передано JSON (EditThisCookie) — конвертуємо у кеш і повертаємо шлях до .txt.
    """
    try:
        p = Path(cookies_file)
        if not p.exists():
            return cookies_file

        head = p.read_text(encoding="utf-8", errors="ignore")[:4096].lstrip()

        # Вже cookies.txt
        if head.startswith("#") and ("Netscape" in head[:300] or "HTTP Cookie File" in head[:300]):
            return cookies_file

        # Якщо не JSON — повертаємо як є
        if not (head.startswith("[") or head.startswith("{")):
            return cookies_file

        raw = p.read_text(encoding="utf-8", errors="ignore")
        digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()

        out_dir = Path(COOKIES_CACHE_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{prefix}_cookies_{digest}.txt"
        if out_path.exists():
            return str(out_path)

        data = json.loads(raw)
        if isinstance(data, dict) and "cookies" in data:
            data = data["cookies"]
        if not isinstance(data, list):
            return cookies_file

        lines = [
            "# Netscape HTTP Cookie File",
            "# Generated from JSON cookies export for yt-dlp",
            "",
        ]

        for c in data:
            if not isinstance(c, dict):
                continue

            domain = c.get("domain") or c.get("host")
            if not domain:
                continue
            domain = str(domain)

            host_only = c.get("hostOnly")
            include_subdomains = "FALSE" if host_only is True and not domain.startswith(".") else "TRUE"

            path = str(c.get("path") or "/")
            secure = "TRUE" if bool(c.get("secure")) else "FALSE"

            if bool(c.get("session")) is True:
                exp_int = 0
            else:
                exp_val = c.get("expirationDate") or c.get("expires") or 0
                try:
                    exp_int = int(float(exp_val))
                except Exception:
                    exp_int = 0
                if exp_int > 10_000_000_000:  # ms -> sec
                    exp_int = int(exp_int / 1000)

            name = c.get("name")
            value = c.get("value")
            if name is None or value is None:
                continue

            lines.append("\t".join([domain, include_subdomains, path, secure, str(exp_int), str(name), str(value)]))

        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(out_path)

    except Exception as e:
        logger.warning("Cookie conversion failed for %s: %s", cookies_file, e)
        return cookies_file


async def notify_admin(url: str = None, error: Exception = None, sender: types.User = None,
                       context: str = None, message_type: str = "error"):
    """
    Enhanced function to notify the admin about bot events or errors.

    Parameters:
    - url: The URL being processed (if applicable)
    - error: The exception that was raised (if applicable)
    - sender: The user who triggered the notification
    - context: Additional context about the notification
    - message_type: Type of notification (error, warning, info)
    """
    from datetime import datetime
    import traceback

    # Create timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build the message
    parts = []

    # Add header with timestamp based on message type
    if message_type == "error":
        parts.append(f"🚨 *Error Notification* ({timestamp})")
    elif message_type == "warning":
        parts.append(f"⚠️ *Warning Notification* ({timestamp})")
    else:
        parts.append(f"ℹ️ *Bot Notification* ({timestamp})")

    # Add user information
    if sender:
        user_link = f"[{sender.full_name or sender.username or 'Unknown'}](tg://user?id={sender.id})"
        user_info = f"👤 *From:* {user_link} (ID: `{sender.id}`)"
        if sender.username:
            user_info += f"\n*Username:* @{sender.username}"
        if hasattr(sender, 'language_code') and sender.language_code:
            user_info += f"\n*Language:* {sender.language_code}"
        parts.append(user_info)

    # Add chat information if it was forwarded from a chat
    if sender and hasattr(sender, 'chat') and sender.chat and sender.chat.type != "private":
        chat_info = f"💬 *Chat:* {sender.chat.title} (ID: `{sender.chat.id}`)"
        chat_info += f"\n*Type:* {sender.chat.type}"
        parts.append(chat_info)

    # Add URL if provided
    if url and url != "N/A":
        parts.append(f"🔗 *URL:* `{url}`")

    # Add context if provided
    if context:
        parts.append(f"📝 *Context:* {context}")

    # Add error details with traceback for errors
    if error:
        error_type = type(error).__name__
        parts.append(f"❌ *Error:* `{error_type}: {str(error)}`")

        # Add traceback for detailed debugging
        tb = traceback.format_exc()
        if len(tb) > 3000:  # Limit length to avoid Telegram message size constraints
            tb = tb[:1000] + "\n...\n" + tb[-1000:]
        parts.append(f"*Traceback:*\n```\n{tb}\n```")

    # Combine all parts with blank lines for readability
    message = "\n\n".join(parts)

    # Log the notification
    if message_type == "error":
        logger.error(f"Admin notification: {error}" if error else "Admin notification sent")
    elif message_type == "warning":
        logger.warning(f"Admin warning: {error}" if error else "Admin warning sent")
    else:
        logger.info("Admin information message sent")

    # Send the message to admin
    try:
        await bot.send_message(ADMIN_ID, message, parse_mode="Markdown")
    except Exception as e:
        # If sending fails with Markdown, try without markup
        logger.error(f"Failed to send admin notification with Markdown: {e}")
        plain_message = message.replace('*', '').replace('`', '').replace('[', '').replace(']', '')
        try:
            await bot.send_message(ADMIN_ID, plain_message)
        except Exception as e2:
            logger.error(f"Failed to send plain text notification: {e2}")


async def send_cached_video_if_exists(url: str, chat_id: int, sender: types.User, platform_name: str,
                                      message_thread_id: int = None) -> bool:
    """Check cache for the URL and send the video via file_id if it exists."""
    file_id = get_cached_video(url)
    if not file_id:
        return False

    try:
        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        caption = f"{user_link} sent [{platform_name}]({url})"
        await bot.send_video(chat_id, file_id, caption=caption, parse_mode="Markdown",
                             message_thread_id=message_thread_id)
        logger.info(f"Successfully sent cached {platform_name} for {url}")
        return True
    except Exception as e:
        logger.warning(f"Failed to send cached video for {url} (possibly invalidated file_id): {e}")
        return False


async def compress_video(input_path: str, output_path: str) -> bool:
    """Compress video using ffmpeg."""
    try:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vcodec", "libx264", "-crf", "28", "-preset", "faster",
            "-acodec", "aac", "-b:a", "128k",
            output_path
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.communicate()
        return process.returncode == 0
    except Exception as e:
        logger.error(f"FFmpeg compression failed: {e}")
        return False


async def _ig_rate_limit():
    if IG_RATE_SECONDS > 0:
        await asyncio.sleep(IG_RATE_SECONDS)


async def download_instagram_via_ytdlp(url: str, chat_id: int, sender: types.User,
                                       message_thread_id: int = None) -> bool:
    """
    Instagram через yt-dlp + cookies.
    Якщо файл >50MB — пробуємо нижчу якість (540 -> 480 -> 360).
    Якщо все впало — (опційно) fallback на download_instagram_via_ddinstagram, якщо він є у твоєму коді.
    """
    await _ig_rate_limit()
    logger.info(f"Downloading IG via yt-dlp: {url}")

    shortcode = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    heights_to_try = [540, 480, 360]

    cookiefile = None
    if IG_YTDLP_COOKIES and os.path.exists(IG_YTDLP_COOKIES):
        cookiefile = _ensure_cookiefile_for_ytdlp(IG_YTDLP_COOKIES, prefix="ig")
        if not os.path.exists(cookiefile):
            cookiefile = None

    last_error = None

    for h in heights_to_try:
        try:
            output_template = os.path.join(
                tempfile.gettempdir(),
                f"instagram_{shortcode}_{h}.%(ext)s"
            )

            ydl_opts = {
                "outtmpl": output_template,
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "retries": 2,
                "http_headers": {"User-Agent": "Mozilla/5.0"},
                # обмеження якості + fps (часто саме 60fps робить файл жирним)
                "format": (
                    f"bv*[ext=mp4][height<={h}][fps<=30]+ba[ext=m4a]/"
                    f"b[ext=mp4][height<={h}][fps<=30]/"
                    f"best[ext=mp4][height<={h}]/best"
                ),
            }

            if cookiefile:
                ydl_opts["cookiefile"] = cookiefile

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_file = ydl.prepare_filename(info)

            if not os.path.exists(video_file):
                raise FileNotFoundError(f"IG file not found: {video_file}")

            file_size_mb = os.path.getsize(video_file) / (1024 * 1024)
            if file_size_mb > 50:
                logger.warning(f"IG too large at {h}p ({file_size_mb:.2f}MB), trying lower...")
                try:
                    os.remove(video_file)
                except Exception:
                    pass
                continue

            user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
            caption = f"{user_link} sent [Instagram Reel]({url})"

            sent_msg = await bot.send_video(chat_id, FSInputFile(video_file), caption=caption, parse_mode="Markdown",
                                            message_thread_id=message_thread_id)

            try:
                save_cached_video(url, sent_msg.video.file_id)
            except Exception as cache_err:
                logger.warning(f"Failed to cache IG video: {cache_err}")

            try:
                os.remove(video_file)
            except Exception:
                pass

            return True

        except Exception as e:
            last_error = e
            # Якщо це login/rate-limit — пониження якості не допоможе, можна одразу виходити
            msg = str(e)
            if ("login required" in msg.lower()) or ("rate-limit" in msg.lower()) or (
                    "requested content is not available" in msg.lower()):
                break
            continue

    # Один раз нотифікуємо адміна і виходимо
    if last_error:
        logger.error(f"IG yt-dlp failed: {last_error}")
        await notify_admin(url, last_error, sender, context="IG yt-dlp download failed", message_type="warning")

    # fallback на твій старий метод, якщо він у файлі існує
    fallback = globals().get("download_instagram_via_ddinstagram")
    if callable(fallback):
        import inspect
        if "message_thread_id" in inspect.signature(fallback).parameters:
            return await fallback(url, chat_id, sender, message_thread_id=message_thread_id)
        return await fallback(url, chat_id, sender)

    return False


async def download_instagram_via_playwright(url: str, chat_id: int, sender: types.User,
                                            message_thread_id: int = None) -> bool:
    """Завантажує відео з Instagram через власний Playwright мікросервіс. Повертає True, якщо успішно."""
    logger.info(f"Attempting Playwright fallback for Instagram: {url}")
    try:
        payload = {"url": url}
        async with aiohttp.ClientSession() as session:
            async with session.post("http://127.0.0.1:8001/api/extract_instagram", json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    media_url = data.get("video_url")
                    if media_url:
                        logger.info(f"Successfully extracted Instagram via Playwright: {media_url[:50]}...")
                        # Відправляємо відео
                        await bot.send_chat_action(chat_id, 'upload_video', message_thread_id=message_thread_id)

                        shortcode = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
                        raw_file = os.path.join(tempfile.gettempdir(), f"instagram_{shortcode}_playwright.mp4")

                        async with session.get(media_url, timeout=60) as v_resp:
                            v_resp.raise_for_status()
                            with open(raw_file, 'wb') as f:
                                async for chunk in v_resp.content.iter_chunked(8192):
                                    f.write(chunk)

                        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
                        caption = f"{user_link} sent [Instagram Reel]({url})"

                        try:
                            sent_msg = await bot.send_video(chat_id, FSInputFile(raw_file), caption=caption,
                                                            parse_mode="Markdown", message_thread_id=message_thread_id)
                            save_cached_video(url, sent_msg.video.file_id)
                        finally:
                            if os.path.exists(raw_file):
                                os.remove(raw_file)

                        return True
                else:
                    logger.error(f"Playwright extract failed with status: {response.status}")
    except Exception as e:
        logger.error(f"Playwright fallback error for Instagram {url}: {e}")
    return False


def _cobalt_base() -> str:
    if not COBALT_API_URL:
        return ""
    return COBALT_API_URL if COBALT_API_URL.endswith("/") else (COBALT_API_URL + "/")


def _guess_ext(filename: str | None, url: str | None, default_ext: str = ".mp4") -> str:
    for s in (filename, url):
        if not s:
            continue
        s = str(s).split("?", 1)[0]
        _, ext = os.path.splitext(s)
        if ext and len(ext) <= 6:
            return ext
    return default_ext


async def _http_get_to_file(session: aiohttp.ClientSession, url: str, dest: str, timeout_s: float):
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in resp.content.iter_chunked(1024 * 128):
                f.write(chunk)


async def download_tiktok_via_cobalt(url: str, chat_id: int, sender: types.User, message_thread_id: int = None) -> bool:
    base = _cobalt_base()
    if not base:
        logger.warning("COBALT_API_URL is empty; cannot download TikTok via Cobalt")
        return False

    try:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "telegram-video-bot/1.0",
        }

        async with aiohttp.ClientSession() as session:
            # 1) нормалізуємо + резолвимо short link (vt/vm)
            u0 = _clean_url(url)
            u1 = await _resolve_final_url(u0, session)
            u1 = _force_www_tiktok(_clean_url(u1))

            payload = {
                "url": u1,
                "alwaysProxy": bool(COBALT_ALWAYS_PROXY),
                "allowH265": True,
                "videoQuality": COBALT_VIDEO_QUALITY,
            }

            timeout = aiohttp.ClientTimeout(total=COBALT_TIMEOUT_SECONDS)
            async with session.post(base, json=payload, headers=headers, timeout=timeout) as resp:
                body = await resp.text()

            if resp.status != 200:
                # важливо: покажемо тіло відповіді (воно пояснює причину)
                raise RuntimeError(f"Cobalt HTTP {resp.status}: {body[:500]}")

            data = json.loads(body)

            if not isinstance(data, dict):
                return False
            if data.get("status") == "error":
                logger.warning(f"Cobalt error: {data.get('error')}")
                return False

            status = data.get("status")
            dl_url = None
            filename = None

            if status in ("tunnel", "redirect"):
                dl_url = data.get("url")
                filename = data.get("filename")
            elif status == "picker":
                items = data.get("picker") or []
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and it.get("type") == "video" and it.get("url"):
                            dl_url = str(it.get("url"))
                            break

            if not dl_url:
                return False

            ext = _guess_ext(filename, dl_url, ".mp4")
            out_path = os.path.join(tempfile.gettempdir(), f"tiktok_{hashlib.sha1(u1.encode()).hexdigest()}{ext}")

            await _http_get_to_file(session, dl_url, out_path, timeout_s=COBALT_TIMEOUT_SECONDS)

        if not os.path.exists(out_path):
            return False

        file_size_mb = os.path.getsize(out_path) / (1024 * 1024)
        if file_size_mb > 50:
            os.remove(out_path)
            logger.warning(f"TikTok file too large ({file_size_mb:.2f}MB)")
            return False

        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        caption = f"{user_link} sent [TikTok Video]({url})"

        sent_msg = await bot.send_video(chat_id, FSInputFile(out_path), caption=caption, parse_mode="Markdown",
                                        message_thread_id=message_thread_id)

        try:
            save_cached_video(url, sent_msg.video.file_id)
        except Exception as cache_err:
            logger.warning(f"Failed to cache Cobalt TikTok video: {cache_err}")

        os.remove(out_path)
        return True

    except Exception as e:
        logger.error(f"Cobalt TikTok failed: {e}")
        await notify_admin(url, e, sender, context="TikTok Cobalt download failed", message_type="warning")
        return False


async def download_tiktok_via_ytdlp(url: str, chat_id: int, sender: types.User, message_thread_id: int = None) -> bool:
    """Завантажує відео з TikTok через yt-dlp. Повертає True, якщо успішно."""
    logger.info(f"Attempting yt-dlp for TikTok: {url}")
    try:
        shortcode = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        output_template = os.path.join(tempfile.gettempdir(), f"tiktok_{shortcode}.%(ext)s")

        ydl_opts = {
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "http_headers": {"User-Agent": "Mozilla/5.0"},
            "retries": 2,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_file = ydl.prepare_filename(info)

        if not os.path.exists(video_file):
            return False

        file_size_mb = os.path.getsize(video_file) / (1024 * 1024)
        if file_size_mb > 50:
            os.remove(video_file)
            logger.warning(f"TikTok yt-dlp file too large ({file_size_mb:.2f}MB)")
            return False

        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        caption = f"{user_link} sent [TikTok Video]({url})"

        sent_msg = await bot.send_video(chat_id, FSInputFile(video_file), caption=caption, parse_mode="Markdown",
                                        message_thread_id=message_thread_id)

        try:
            save_cached_video(url, sent_msg.video.file_id)
        except Exception as cache_err:
            logger.warning(f"Failed to cache yt-dlp TikTok video: {cache_err}")

        os.remove(video_file)
        return True
    except Exception as e:
        logger.error(f"yt-dlp error for TikTok {url}: {e}")
        return False


async def download_tiktok_via_playwright(url: str, chat_id: int, sender: types.User,
                                         message_thread_id: int = None) -> bool:
    """Завантажує відео з TikTok через власний Playwright мікросервіс. Повертає True, якщо успішно."""
    logger.info(f"Attempting Playwright fallback for TikTok: {url}")
    try:
        payload = {"url": url}
        async with aiohttp.ClientSession() as session:
            async with session.post("http://127.0.0.1:8001/api/extract_tiktok", json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    media_url = data.get("video_url")
                    if media_url:
                        logger.info(f"Successfully extracted TikTok via Playwright: {media_url[:50]}...")
                        # Відправляємо відео
                        await bot.send_chat_action(chat_id, 'upload_video', message_thread_id=message_thread_id)

                        shortcode = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
                        raw_file = os.path.join(tempfile.gettempdir(), f"tiktok_{shortcode}_playwright.mp4")

                        async with session.get(media_url, timeout=60) as v_resp:
                            v_resp.raise_for_status()
                            with open(raw_file, 'wb') as f:
                                async for chunk in v_resp.content.iter_chunked(8192):
                                    f.write(chunk)

                        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
                        caption = f"{user_link} sent [TikTok Video]({url})"

                        try:
                            sent_msg = await bot.send_video(chat_id, FSInputFile(raw_file), caption=caption,
                                                            parse_mode="Markdown", message_thread_id=message_thread_id)
                            save_cached_video(url, sent_msg.video.file_id)
                        finally:
                            if os.path.exists(raw_file):
                                os.remove(raw_file)

                        return True
                else:
                    logger.error(f"Playwright extract failed with status: {response.status}")
    except Exception as e:
        logger.error(f"Playwright fallback error for TikTok {url}: {e}")
    return False


async def download_threads_media(url: str, chat_id: int, sender: types.User, message_thread_id: int = None) -> bool:
    """Download and send Threads media to the chat using native extraction."""
    try:
        logger.info(f"Downloading Threads media from microservice: {url}")

        async with aiohttp.ClientSession() as session:
            # 1. Ask microservice for the raw mp4 link
            microservice_url = "http://127.0.0.1:8001/api/extract_threads"
            payload = {"url": url}

            async with session.post(microservice_url, json=payload, timeout=45) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Microservice failed with status {resp.status}: {error_text}")
                data = await resp.json()
                media_list = data.get("media", [])
                post_caption = data.get("caption", "")

            if not media_list:
                raise Exception("Microservice returned success but no media found")

            shortcode = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
            temp_dir = Path(tempfile.gettempdir())
            downloaded_files = []

            user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"

            # Use retrieved caption or fallback to generic text
            if post_caption:
                # Escape for Telegram markdown, or just use HTML/Plain. We use Markdown.
                # Safe truncating and minimal escaping for Markdown if needed:
                c_text = post_caption[:800].replace("[", "\\[").replace("]", "\\]")
                caption = f"{user_link} sent [Threads Post]({url})\n\n{c_text}"
            else:
                caption = f"{user_link} sent [Threads Post]({url})"

            for idx, item in enumerate(media_list):
                ext = ".mp4" if item["type"] == "video" else ".jpg"
                raw_file = temp_dir / f"threads_{shortcode}_raw_{idx}{ext}"

                async with session.get(item["url"], timeout=60) as v_resp:
                    v_resp.raise_for_status()
                    with open(raw_file, 'wb') as f:
                        async for chunk in v_resp.content.iter_chunked(8192):
                            f.write(chunk)

                final_file = raw_file
                if item["type"] == "video":
                    # Compress video
                    compressed_file = temp_dir / f"threads_{shortcode}_cmp_{idx}.mp4"
                    logger.info(f"Compressing video {raw_file}...")
                    if await compress_video(str(raw_file), str(compressed_file)):
                        # If compression OK, use it
                        final_file = compressed_file
                        try:
                            raw_file.unlink()
                        except:
                            pass

                # Check size
                if final_file.exists():
                    file_size_mb = final_file.stat().st_size / (1024 * 1024)
                    if file_size_mb > 50:
                        logger.warning(f"File too large {file_size_mb:.2f}MB, skipping {final_file}")
                        final_file.unlink()
                        continue
                    downloaded_files.append({"type": item["type"], "path": final_file})

        if not downloaded_files:
            return False

        send_success = False
        try:
            if len(downloaded_files) == 1:
                # Send single file
                single = downloaded_files[0]
                if single["type"] == "video":
                    sent_msg = await bot.send_video(chat_id, FSInputFile(single["path"]), caption=caption,
                                                    parse_mode="Markdown", message_thread_id=message_thread_id)
                    save_cached_video(url, sent_msg.video.file_id)
                else:
                    await bot.send_photo(chat_id, FSInputFile(single["path"]), caption=caption, parse_mode="Markdown",
                                         message_thread_id=message_thread_id)
                send_success = True
            else:
                # Send as media group
                media_group = MediaGroupBuilder(caption=caption)
                for df in downloaded_files:
                    if df["type"] == "video":
                        media_group.add_video(media=FSInputFile(df["path"]), parse_mode="Markdown")
                    else:
                        media_group.add_photo(media=FSInputFile(df["path"]), parse_mode="Markdown")
                await bot.send_media_group(chat_id=chat_id, media=media_group.build(),
                                           message_thread_id=message_thread_id)
                send_success = True
        except Exception as send_err:
            logger.error(f"Failed to send Threads media: {send_err}")
            raise send_err
        finally:
            # Cleanup all
            for df in downloaded_files:
                try:
                    df["path"].unlink(missing_ok=True)
                except:
                    pass

        return send_success

    except Exception as e:
        logger.error(f"Threads download failed: {e}")
        await notify_admin(url, e, sender, context="Threads microservice/download failed", message_type="error")
        return False


async def download_youtube_shorts(url: str, chat_id: int, sender: types.User, message_thread_id: int = None):
    """Download and send YouTube Shorts to the chat with audio."""
    try:
        logger.info(f"Starting download for YouTube Shorts URL: {url} sent by user: {sender.id}")

        video_id = url.split("/shorts/")[1].split("?")[0]
        output_template = f"youtube_shorts_{video_id}.%(ext)s"
        ydl_opts = {
            'format': '231+234/bestvideo[height<=480][ext=mp4]+bestaudio/best',  # Explicitly prioritize 231+234
            'outtmpl': output_template,
            'merge_output_format': 'mp4',  # Merge into MP4
            'postprocessors': [{  # Ensure FFmpeg merges video and audio
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'ffmpeg_location': '/usr/bin/ffmpeg',  # Confirmed path for your system
            'quiet': False,  # Enable verbose output for debugging
            'no_warnings': False,
        }

        # Debug available formats
        with yt_dlp.YoutubeDL({'quiet': False, 'no_warnings': False}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            # logger.info(f"Available formats for {url}: {[f'{f.get('format_id')}: {f.get('ext')} {f.get('resolution', 'unknown')} acodec={f.get('acodec', 'none')} vcodec={f.get('vcodec', 'none')} filesize={f.get('filesize_approx', 'unknown')}' for f in formats]}")

        # Download the video
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_file = ydl.prepare_filename(info)

            # Log selected format
            selected_format_id = info.get('format_id', 'unknown')
            logger.info(f"Selected format for {url}: {selected_format_id}")

            # Check if the file exists
            if not os.path.exists(video_file):
                raise FileNotFoundError("YouTube Shorts video file not found after download.")

        # Check file size (Telegram limit: 50 MB for regular bots)
        file_size_mb = os.path.getsize(video_file) / (1024 * 1024)
        logger.info(f"Downloaded file size for {url}: {file_size_mb:.2f} MB")
        if file_size_mb > 50:
            raise ValueError(f"Video file size ({file_size_mb:.2f} MB) exceeds Telegram's 50 MB limit.")

        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        caption = f"{user_link} sent [YouTube Shorts]({url})"

        logger.info(f"Sending YouTube Shorts video to chat: {chat_id}")
        sent_msg = await bot.send_video(chat_id, FSInputFile(video_file), caption=caption, parse_mode="Markdown",
                                        width=480, height=854, message_thread_id=message_thread_id)

        try:
            save_cached_video(url, sent_msg.video.file_id)
        except Exception as cache_err:
            logger.warning(f"Failed to cache YouTube Shorts video: {cache_err}")

        os.remove(video_file)
        logger.info(f"Successfully sent YouTube Shorts video and cleaned up.")
        return True
    except Exception as e:
        logger.error(f"Error during processing for YouTube Shorts URL in {chat_id=}: {url}.\nError: {e}")
        await notify_admin(url, e, sender, context="Failed to download YouTube Shorts")
        return False


async def download_twitter_media(url: str, chat_id: int, sender: types.User, message_thread_id: int = None):
    """Download and send Twitter media (video or images) to the chat."""
    try:
        logger.info(f"Processing Twitter URL: {url} sent by user: {sender.id}")

        # Спроба завантажити відео через yt_dlp
        video_success = await download_twitter_video(url, chat_id, sender, message_thread_id)
        if video_success:
            return True

        # Якщо відео не знайдено, шукаємо зображення через FixTweet
        image_success = await download_twitter_images_via_fixtweet(url, chat_id, sender, message_thread_id)
        if image_success:
            return True

        # Якщо немає ні відео, ні зображень
        logger.warning(f"No media found in tweet: {url}")
        return False

    except Exception as e:
        logger.error(f"Error processing Twitter media for URL: {url}. Error: {e}")
        await notify_admin(url, e, sender)
        return False


async def download_twitter_video(url: str, chat_id: int, sender: types.User, message_thread_id: int = None) -> bool:
    """Download and send Twitter video."""
    try:
        tweet_id = url.split("/status/")[1].split("?")[0]
        output_template = f"twitter_video_{tweet_id}.%(ext)s"
        ydl_opts = {
            'format': '(mp4)[filesize<20M]/(mp4)[height<=720]/mp4',
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'formats' in info:
                ydl.download([url])
                video_file = ydl.prepare_filename(info)

                if not os.path.exists(video_file):
                    raise FileNotFoundError(f"Twitter video file not found: {video_file}")

                user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
                caption = f"{user_link} sent [Twitter Video]({url})"
                sent_msg = await bot.send_video(chat_id, FSInputFile(video_file), caption=caption,
                                                parse_mode="Markdown", message_thread_id=message_thread_id)

                try:
                    save_cached_video(url, sent_msg.video.file_id)
                except Exception as cache_err:
                    logger.warning(f"Failed to cache Twitter video: {cache_err}")

                os.remove(video_file)
                logger.info(f"Successfully sent Twitter video for tweet: {url}")
                return True
            else:
                logger.info(f"No video found in tweet: {url}")
                return False

    except yt_dlp.utils.DownloadError:
        logger.info(f"No video found in tweet: {url}")
        return False
    except Exception as e:
        logger.error(f"Error downloading Twitter video for URL: {url}. Error: {e}")
        return False


async def download_twitter_images_via_fixtweet(url: str, chat_id: int, sender: types.User,
                                               message_thread_id: int = None) -> bool:
    """Download and send Twitter images via FixTweet."""
    try:
        url_to_send = re.sub(r"(https?://)(?:www\.)?(twitter\.com|x\.com)", r"\1fxtwitter.com", url)
        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        message_text = f"{user_link} sent [Twitter post]({url_to_send})"
        await bot.send_message(chat_id, message_text, parse_mode="Markdown", message_thread_id=message_thread_id)
        return True
    except Exception as e:
        logger.error(f"Error downloading Twitter video for URL: {url}. Error: {e}")
        return False


@router.message(F.text == "/start")
async def send_welcome(message: types.Message):
    """Send a welcome message to the admin."""
    sender = message.from_user
    log_user_start(sender.id, sender.username, sender.full_name)
    if sender.id == int(ADMIN_ID):
        logger.info(f"Admin {ADMIN_ID} initiated the bot.")
        await message.reply("Hi Admin!\nI'm your bot, ready to assist you.")
    user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
    await bot.send_message(ADMIN_ID, f"User {user_link} send /start to bot", parse_mode="Markdown")
    await message.reply(START_MESSAGE_NON_ADMIN, parse_mode="Markdown", disable_web_page_preview=True)


@router.message(lambda message: message.text and re.search(INSTAGRAM_REELS_REGEX, message.text))
async def handle_instagram_reels(message: types.Message):
    """Handle messages containing Instagram Reel links."""
    match = re.search(INSTAGRAM_REELS_REGEX, message.text)
    if match and len(message.text.split()) == 1:
        sender = message.from_user
        if not check_rate_limit(sender.id):
            asyncio.create_task(
                send_temp_message(message.chat.id, "⏳ Зачекайте кілька секунд перед наступним завантаженням.", delay=5,
                                  message_thread_id=message.message_thread_id, reply_to_message_id=message.message_id))
            return

        url = match.group(0)
        chat_id = message.chat.id
        thread_id = message.message_thread_id
        log_activity(sender.id, chat_id, instagram=True)
        log_chat_usage(chat_id, message.chat.title)

        if await send_cached_video_if_exists(url, chat_id, sender, "Instagram Reel", thread_id):
            await safe_delete_message(message)
            return

        logger.info(f"Received Instagram Reels link: {url} from user: {sender.id}")
        loading_msg = await message.reply("⏳ Опрацьовую посилання...")
        success = await download_instagram_via_ytdlp(url, chat_id, sender, thread_id)

        if not success:
            logger.info("yt-dlp download failed, trying Playwright fallback for Instagram...")
            success = await download_instagram_via_playwright(url, chat_id, sender, thread_id)

        await safe_delete_message(loading_msg)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await safe_delete_message(message)
        else:
            asyncio.create_task(
                send_temp_message(chat_id, "❌ На жаль, не вдалося завантажити медіа за цим посиланням.", delay=30,
                                  message_thread_id=thread_id))


@router.message(lambda message: message.text and re.search(YOUTUBE_SHORTS_REGEX, message.text))
async def handle_youtube_shorts(message: types.Message):
    """Handle messages containing YouTube Shorts links."""
    match = re.search(YOUTUBE_SHORTS_REGEX, message.text)
    if match and len(message.text.split(' ')) == 1:
        sender = message.from_user
        if not check_rate_limit(sender.id):
            asyncio.create_task(
                send_temp_message(message.chat.id, "⏳ Зачекайте кілька секунд перед наступним завантаженням.", delay=5,
                                  message_thread_id=message.message_thread_id, reply_to_message_id=message.message_id))
            return

        url = match.group(0)
        chat_id = message.chat.id
        thread_id = message.message_thread_id

        log_activity(sender.id, chat_id, youtube=True)
        log_chat_usage(chat_id, message.chat.title)

        if await send_cached_video_if_exists(url, chat_id, sender, "YouTube Shorts", thread_id):
            await safe_delete_message(message)
            return

        logger.info(f"Received YouTube Shorts link: {url} from user: {sender.id}")
        loading_msg = await message.reply("⏳ Опрацьовую посилання...")
        success = await download_youtube_shorts(url, chat_id, sender, thread_id)

        await safe_delete_message(loading_msg)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await safe_delete_message(message)
        else:
            asyncio.create_task(
                send_temp_message(chat_id, "❌ На жаль, не вдалося завантажити медіа за цим посиланням.", delay=30,
                                  message_thread_id=thread_id))


@router.message(lambda message: message.text and re.search(TWITTER_REGEX, message.text))
async def handle_twitter_media(message: types.Message):
    """Handle messages containing Twitter links."""
    match = re.search(TWITTER_REGEX, message.text)
    if match and len(message.text.split(' ')) == 1:
        sender = message.from_user
        if not check_rate_limit(sender.id):
            asyncio.create_task(
                send_temp_message(message.chat.id, "⏳ Зачекайте кілька секунд перед наступним завантаженням.", delay=5,
                                  message_thread_id=message.message_thread_id, reply_to_message_id=message.message_id))
            return

        url = match.group(0)
        chat_id = message.chat.id
        thread_id = message.message_thread_id
        log_activity(sender.id, chat_id, twitter=True)  # Логування використання функції Twitter
        log_chat_usage(chat_id, message.chat.title)

        if await send_cached_video_if_exists(url, chat_id, sender, "Twitter Video", thread_id):
            await safe_delete_message(message)
            return

        logger.info(f"Received Twitter link: {url} from user: {sender.id}")
        loading_msg = await message.reply("⏳ Опрацьовую посилання...")
        success = await download_twitter_media(url, chat_id, sender, thread_id)

        await safe_delete_message(loading_msg)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await safe_delete_message(message)
        else:
            asyncio.create_task(
                send_temp_message(chat_id, "❌ На жаль, не вдалося завантажити медіа за цим посиланням.", delay=30,
                                  message_thread_id=thread_id))


@router.message(lambda message: message.text and re.search(TIKTOK_REGEX, message.text))
async def handle_tiktok(message: types.Message):
    """Обробляє повідомлення з TikTok посиланнями."""
    match = re.search(TIKTOK_REGEX, message.text)
    if match and len(message.text.split(' ')) == 1 and message.chat.id not in IGNORED_CHATS_FOR_TIKTOK:
        sender = message.from_user
        if not check_rate_limit(sender.id):
            asyncio.create_task(
                send_temp_message(message.chat.id, "⏳ Зачекайте кілька секунд перед наступним завантаженням.", delay=5,
                                  message_thread_id=message.message_thread_id, reply_to_message_id=message.message_id))
            return

        url = match.group(0)
        chat_id = message.chat.id
        thread_id = message.message_thread_id
        log_activity(sender.id, chat_id, tiktok=True)  # Потрібно оновити функцію log_activity
        log_chat_usage(chat_id, message.chat.title)

        if await send_cached_video_if_exists(url, chat_id, sender, "TikTok Video", thread_id):
            await safe_delete_message(message)
            return

        logger.info(f"Received TikTok link: {url} from user: {sender.id}")
        loading_msg = await message.reply("⏳ Опрацьовую посилання...")

        success = await download_tiktok_via_cobalt(url, chat_id, sender, thread_id)

        if not success:
            logger.info("Cobalt download failed, trying yt-dlp fallback...")
            success = await download_tiktok_via_ytdlp(url, chat_id, sender, thread_id)

        if not success:
            logger.info("yt-dlp download failed, trying Playwright fallback...")
            success = await download_tiktok_via_playwright(url, chat_id, sender, thread_id)

        await safe_delete_message(loading_msg)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await safe_delete_message(message)
        else:
            asyncio.create_task(
                send_temp_message(chat_id, "❌ На жаль, не вдалося завантажити медіа за цим посиланням.", delay=30,
                                  message_thread_id=thread_id))


@router.message(lambda message: message.text and re.search(THREADS_REGEX, message.text))
async def handle_threads(message: types.Message):
    """Обробляє повідомлення з Threads посиланнями."""
    match = re.search(THREADS_REGEX, message.text)
    if match and len(message.text.split(' ')) == 1:
        sender = message.from_user
        if not check_rate_limit(sender.id):
            asyncio.create_task(
                send_temp_message(message.chat.id, "⏳ Зачекайте кілька секунд перед наступним завантаженням.", delay=5,
                                  message_thread_id=message.message_thread_id, reply_to_message_id=message.message_id))
            return

        url = match.group(0).split('?')[0]  # Remove query parameters for better caching and processing
        url = url.replace("threads.com", "threads.net")  # Standardize to threads.net
        chat_id = message.chat.id
        thread_id = message.message_thread_id
        log_activity(sender.id, chat_id, threads=True)
        log_chat_usage(chat_id, message.chat.title)

        if await send_cached_video_if_exists(url, chat_id, sender, "Threads Post", thread_id):
            await safe_delete_message(message)
            return

        logger.info(f"Received Threads link: {url} from user: {sender.id}")
        loading_msg = await message.reply("⏳ Опрацьовую посилання...")

        success = await download_threads_media(url, chat_id, sender, thread_id)

        await safe_delete_message(loading_msg)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await safe_delete_message(message)
        else:
            asyncio.create_task(
                send_temp_message(chat_id, "❌ На жаль, не вдалося завантажити медіа за цим посиланням.", delay=30,
                                  message_thread_id=thread_id))


@router.message()  # Catch-all handler for any unhandled messages
async def forward_to_admin(message: types.Message):
    """Forward any unhandled direct message to the admin."""
    # Only forward messages from private chats (direct messages to bot)
    if message.chat.type != "private":
        return

    # Prevent forwarding admin's own messages
    if message.from_user.id == int(ADMIN_ID):
        return

    sender = message.from_user
    logger.info(f"Forwarding private message from user {sender.id} to admin")

    try:
        # Forward the original message
        await bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )

        # Send context information about the sender
        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"

        # Build user profile information
        user_info = f"👤 **User Profile:**\n"
        user_info += f"- Name: {sender.full_name or 'Not provided'}\n"
        user_info += f"- Username: @{sender.username or 'None'}\n"
        user_info += f"- User ID: `{sender.id}`\n"

        if hasattr(sender, 'language_code') and sender.language_code:
            user_info += f"- Language: {sender.language_code}\n"

        context = f"👆 Message above forwarded from private chat with {user_link}\n\n{user_info}"

        await bot.send_message(ADMIN_ID, context, parse_mode="Markdown")

        # Log forwarding activity
        log_activity(sender.id, message.chat.id, forwarded=True)

    except Exception as e:
        logger.error(f"Error forwarding message to admin: {e}")
        await notify_admin("N/A", e, sender)


async def safe_delete_message(message: types.Message):
    """Attempt to delete a message safely. Fails gracefully if bot lacks permissions."""
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Failed to delete message in chat {message.chat.id}: {e}")


async def send_temp_message(chat_id: int, text: str, delay: int = 30, message_thread_id: int = None,
                            reply_to_message_id: int = None):
    """Send a temporary message and delete it after a delay."""
    try:
        msg = await bot.send_message(chat_id, text, message_thread_id=message_thread_id,
                                     reply_to_message_id=reply_to_message_id)
        await asyncio.sleep(delay)
        await safe_delete_message(msg)
    except Exception as e:
        logger.error(f"Failed to send or delete temp message: {e}")


def check_rate_limit(user_id: int) -> bool:
    """Check if the user is downloading too fast. Returns True if allowed, False if throttled."""
    import time
    now = time.time()
    last_req = user_last_request_time.get(user_id, 0)
    if now - last_req < RATE_LIMIT_SECONDS:
        return False
    user_last_request_time[user_id] = now
    return True


async def main():
    """Start the bot."""
    init_db()
    logger.info("Bot is starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
