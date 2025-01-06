import asyncio
import os
import re
import shutil
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types.input_file import FSInputFile
from instaloader import Instaloader, Post
import yt_dlp

# Load environment variables
load_dotenv()

# Constants
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")
INSTAGRAM_REELS_REGEX = r"https?://(?:www\.)?instagram\.com/reel/[\w-]+/"
YOUTUBE_SHORTS_REGEX = r"https?://(?:www\.)?youtube\.com/shorts/[\w-]+"

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

# Initialize Instaloader
loader = Instaloader()
logging.getLogger("instaloader").setLevel(logging.ERROR)

def extract_shortcode(url: str) -> str:
    """Extract shortcode from Instagram Reel URL."""
    return url.split("/reel/")[1].split("/")[0]

def locate_video_file(directory: str) -> str:
    """Locate the downloaded video file in the specified directory."""
    return next((file for file in os.listdir(directory) if file.endswith(".mp4")), None)

async def notify_admin(url: str, error: Exception):
    """Notify the admin about an error."""
    message = f"Failed to process video for URL: {url}. Error: {error}"
    logger.error(message)
    await bot.send_message(ADMIN_ID, message)

async def download_instagram_reel(url: str, chat_id: int, sender: types.User, message_id: int):
    """Download and send Instagram Reel to the chat."""
    try:
        logger.info(f"Starting download for URL: {url} sent by user: {sender.id}")
        shortcode = extract_shortcode(url)
        post = Post.from_shortcode(loader.context, shortcode)
        loader.download_post(post, target=shortcode)

        logger.info(f"Download completed for shortcode: {shortcode}")
        video_file = locate_video_file(shortcode)
        if not video_file:
            raise FileNotFoundError("Video file not found after download.")

        video_path = os.path.join(shortcode, video_file)
        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        caption = f"{user_link} sent [Reels]({url})"

        logger.info(f"Sending video {video_file} to chat: {chat_id}")
        await bot.send_video(chat_id, FSInputFile(video_path), caption=caption, parse_mode="Markdown")
        shutil.rmtree(shortcode)
        logger.info(f"Successfully sent video and cleaned up for shortcode: {shortcode}")
        return True
    except Exception as e:
        logger.error(f"Error during processing for URL: {url}. Error: {e}")
        await notify_admin(url, e)
        return False

async def download_youtube_shorts(url: str, chat_id: int, sender: types.User):
    """Download and send YouTube Shorts to the chat."""
    try:
        logger.info(f"Starting download for YouTube Shorts URL: {url} sent by user: {sender.id}")
        ydl_opts = {
            'format': '(mp4)[filesize<20M]/best',  # Prefer MP4 and limit file size, fallback to best
            'outtmpl': 'youtube_shorts.%(ext)s',
            'quiet': True,
            'no_warnings': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_file = ydl.prepare_filename(info)

        if not os.path.exists(video_file):
            raise FileNotFoundError("YouTube Shorts video file not found after download.")

        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        caption = f"{user_link} sent [YouTube Shorts]({url})"

        logger.info(f"Sending YouTube Shorts video to chat: {chat_id}")
        await bot.send_video(chat_id, FSInputFile(video_file), caption=caption, parse_mode="Markdown", width=480, height=720)
        os.remove(video_file)
        logger.info(f"Successfully sent YouTube Shorts video and cleaned up.")
        return True
    except Exception as e:
        logger.error(f"Error during processing for YouTube Shorts URL: {url}. Error: {e}")
        await notify_admin(url, e)
        return False

@router.message(F.text == "/start")
async def send_welcome(message: types.Message):
    """Send a welcome message to the admin."""
    sender = message.from_user
    if sender.id == int(ADMIN_ID):
        logger.info(f"Admin {ADMIN_ID} initiated the bot.")
        await message.reply("Hi Admin!\nI'm your bot, ready to assist you.")
    else:
        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        await bot.send_message(ADMIN_ID, f"User {user_link} send /start to bot", parse_mode="Markdown")

@router.message(lambda message: re.search(INSTAGRAM_REELS_REGEX, message.text))
async def handle_instagram_reels(message: types.Message):
    """Handle messages containing Instagram Reel links."""
    match = re.search(INSTAGRAM_REELS_REGEX, message.text)
    if match:
        url = match.group(0)
        sender = message.from_user
        chat_id = message.chat.id

        logger.info(f"Received Instagram Reels link: {url} from user: {sender.id}")
        success = await download_instagram_reel(url, chat_id, sender, message.message_id)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await message.delete()

@router.message(lambda message: re.search(YOUTUBE_SHORTS_REGEX, message.text))
async def handle_youtube_shorts(message: types.Message):
    """Handle messages containing YouTube Shorts links."""
    match = re.search(YOUTUBE_SHORTS_REGEX, message.text)
    if match:
        url = match.group(0)
        sender = message.from_user
        chat_id = message.chat.id

        logger.info(f"Received YouTube Shorts link: {url} from user: {sender.id}")
        success = await download_youtube_shorts(url, chat_id, sender)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await message.delete()

async def main():
    """Start the bot."""
    logger.info("Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
