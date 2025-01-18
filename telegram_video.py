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

from db_utils import log_user_start, log_chat_usage, log_activity

# Load environment variables
load_dotenv()

# Constants
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")
INSTAGRAM_REELS_REGEX = r"https?://(?:www\.)?instagram\.com/(?:reel|share)/[\w-]+(\/?)"
YOUTUBE_SHORTS_REGEX = r"https?://(?:www\.)?youtube\.com/shorts/[\w-]+"
TWITTER_REGEX = r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[\w-]+/status/[\d]+"

START_MESSAGE_NON_ADMIN = (
    "🖖 Привіт, мене звати Кортес.\n\n"
    "Я допоможу тобі інтегрувати відео із Instagram Reels, YouTube Short та Twitter в Telegram. Просто присилай мені посилання на відео у форматі:\n"
    "https://www.instagram.com/reel/XXX\n"
    "https://www.youtube.com/shorts/XXX\n"
    "https://twitter.com/user/status/XXX\n\n"
    "Я його завантажу і пришлю тобі телеграм повідомленням. Завантаження займає певний час, тож будь терплячим.\n\n"
    "Якщо хочеш, щоб я працював у групі - додай мене туди і дай права адміна. На жаль, за правилами Telegram я не буду бачити повідомлення учасників без прав адміна.\n\n"
    "Якщо хочеш, щоб я видаляв повідомлення з посиланнями, які перетоврю на відео - то дай мені права на видалення повідолмень.\n\n"
    "Слідкуй за оновленнями та за іншими розробками на [каналі автора](https://t.me/knemchenko_dev). Ви також можете [підтримати проект фінансово](https://send.monobank.ua/jar/3ekUcZV1iR), але робіть це після того як задонатие на ЗСУ."
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

# Initialize Instaloader
instagram_loader = Instaloader()
instagram_loader.download_video_thumbnails = False
instagram_loader.download_comments = False
instagram_loader.download_geotags = False
instagram_loader.save_metadata = False
logging.getLogger("instaloader").setLevel(logging.ERROR)

def extract_shortcode(url: str) -> str:
    """Extract shortcode from Instagram Reel URL."""
    return url.split("/reel/")[1].split("/")[0]

def locate_video_file(directory: str) -> str:
    """Locate the downloaded video file in the specified directory."""
    return next((file for file in os.listdir(directory) if file.endswith(".mp4")), None)

async def notify_admin(url: str, error: Exception, sender: types.User):
    """Notify the admin about an error."""
    user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
    message = f"Failed to process video from {user_link} with URL: {url} \nError: {error}"
    logger.error(message)
    await bot.send_message(ADMIN_ID, message)

async def download_instagram_reel(url: str, chat_id: int, sender: types.User, message_id: int):
    """Download and send Instagram Reel to the chat."""
    try:
        logger.info(f"Starting download for URL: {url} sent by user: {sender.id}")
        shortcode = extract_shortcode(url)
        post = Post.from_shortcode(instagram_loader.context, shortcode)
        instagram_loader.download_post(post, target=shortcode)

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
        logger.error(f"Error during processing for URL in {chat_id=}: {url}.\nError: {e}")
        await notify_admin(url, e, sender)
        return False

async def download_youtube_shorts(url: str, chat_id: int, sender: types.User):
    """Download and send YouTube Shorts to the chat."""
    try:
        logger.info(f"Starting download for YouTube Shorts URL: {url} sent by user: {sender.id}")

        video_id = url.split("/shorts/")[1].split("?")[0]
        output_template = f"youtube_shorts_{video_id}.%(ext)s"
        ydl_opts = {
            'format': '(mp4)[filesize<20M]/(mp4)[height<=720]/mp4/best',  # Prefer MP4 and limit file size, fallback to best
            'outtmpl': output_template,
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
        logger.error(f"Error during processing for YouTube Shorts URL in {chat_id=}: {url}.\nError: {e}")
        await notify_admin(url, e, sender)
        return False

async def download_twitter_media(url: str, chat_id: int, sender: types.User):
    """Download and send Twitter media (video or images) to the chat."""
    try:
        logger.info(f"Processing Twitter URL: {url} sent by user: {sender.id}")

        # Спроба завантажити відео через yt_dlp
        video_success = await download_twitter_video(url, chat_id, sender)
        if video_success:
            return True

        # Якщо відео не знайдено, шукаємо зображення через FixTweet
        image_success = await download_twitter_images_via_fixtweet(url, chat_id, sender)
        if image_success:
            return True

        # Якщо немає ні відео, ні зображень
        logger.warning(f"No media found in tweet: {url}")
        return False

    except Exception as e:
        logger.error(f"Error processing Twitter media for URL: {url}. Error: {e}")
        await notify_admin(url, e, sender)
        return False

async def download_twitter_video(url: str, chat_id: int, sender: types.User) -> bool:
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
                await bot.send_video(chat_id, FSInputFile(video_file), caption=caption, parse_mode="Markdown")

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

async def download_twitter_images_via_fixtweet(url: str, chat_id: int, sender: types.User) -> bool:
    """Download and send Twitter images via FixTweet."""
    try:
        url_to_send = re.sub(r"(https?://)(?:www\.)?(twitter\.com|x\.com)", r"\1fxtwitter.com", url)
        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        message_text = f"{user_link} sent [Twitter post]({url_to_send})"
        await bot.send_message(chat_id, message_text, parse_mode="Markdown")
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
    else:
        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        await bot.send_message(ADMIN_ID, f"User {user_link} send /start to bot", parse_mode="Markdown")
        await message.reply(START_MESSAGE_NON_ADMIN, parse_mode="Markdown", disable_web_page_preview=True)

@router.message(lambda message: message.text and re.search(INSTAGRAM_REELS_REGEX, message.text))
async def handle_instagram_reels(message: types.Message):
    """Handle messages containing Instagram Reel links."""
    match = re.search(INSTAGRAM_REELS_REGEX, message.text)
    if match and len(message.text.split(' ')) == 1:
        url = match.group(0)
        url = url.replace('share', 'reel')if 'share' in url else url
        sender = message.from_user
        chat_id = message.chat.id

        log_activity(sender.id, chat_id, instagram=True)
        log_chat_usage(chat_id, message.chat.title)

        logger.info(f"Received Instagram Reels link: {url} from user: {sender.id}")
        success = await download_instagram_reel(url, chat_id, sender, message.message_id)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await message.delete()

@router.message(lambda message: message.text and re.search(YOUTUBE_SHORTS_REGEX, message.text))
async def handle_youtube_shorts(message: types.Message):
    """Handle messages containing YouTube Shorts links."""
    match = re.search(YOUTUBE_SHORTS_REGEX, message.text)
    if match and len(message.text.split(' ')) == 1:
        url = match.group(0)
        sender = message.from_user
        chat_id = message.chat.id

        log_activity(sender.id, chat_id, youtube=True)
        log_chat_usage(chat_id, message.chat.title)

        logger.info(f"Received YouTube Shorts link: {url} from user: {sender.id}")
        success = await download_youtube_shorts(url, chat_id, sender)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await message.delete()

@router.message(lambda message: message.text and re.search(TWITTER_REGEX, message.text))
async def handle_twitter_media(message: types.Message):
    """Handle messages containing Twitter links."""
    match = re.search(TWITTER_REGEX, message.text)
    if match and len(message.text.split(' ')) == 1:
        url = match.group(0)
        sender = message.from_user
        chat_id = message.chat.id

        log_activity(sender.id, chat_id, twitter=True)  # Логування використання функції Twitter
        log_chat_usage(chat_id, message.chat.title)

        logger.info(f"Received Twitter link: {url} from user: {sender.id}")
        success = await download_twitter_media(url, chat_id, sender)

        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await message.delete()

async def main():
    """Start the bot."""
    logger.info("Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
