import asyncio
import os
import re
import shutil
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import LinkPreviewOptions
from aiogram.types.input_file import FSInputFile
import yt_dlp

from db_utils import log_user_start, log_chat_usage, log_activity

# Load environment variables
load_dotenv()

# Constants
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
INSTAGRAM_REELS_REGEX = r"https?://(?:www\.)?instagram\.com/(?:reel|p|share|stories)/[\w-]+(/?)(?:\?.*)?$"
YOUTUBE_SHORTS_REGEX = r"https?://(?:www\.)?youtube\.com/shorts/[\w-]+"
TWITTER_REGEX = r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[\w-]+/status/[\d]+"
TIKTOK_REGEX = r"https?://(?:www\.|vm\.)?tiktok\.com/(?:@[\w.-]+/video/\d+|[\w]+/?)"

IGNORED_CHATS_FOR_TIKTOK = (-1, -2)

START_MESSAGE_NON_ADMIN = (
    "🖖 Привіт, мене звати Кортес.\n\n"
    "Я допоможу тобі інтегрувати відео із Instagram Reels, YouTube Short, Twitter та Тікток в Telegram. Просто присилай мені посилання на відео у форматі:\n"
    "https://www.instagram.com/reel/XXX\n"
    "https://www.youtube.com/shorts/XXX\n"
    "https://twitter.com/user/status/XXX\n"
    "https://vm.tiktok.com/XXX\n\n"
    "Я його завантажу і пришлю тобі телеграм повідомленням. Завантаження займає певний час, тож будь терплячим.\n\n"
    "Якщо хочеш, щоб я працював у групі - додай мене туди і дай права адміна. На жаль, за правилами Telegram я не буду бачити повідомлення учасників без прав адміна.\n\n"
    "Якщо хочеш, щоб я видаляв повідомлення з посиланнями, які перетоврю на відео - то дай мені права на видалення повідолмень.\n\n"
    "Слідкуй за оновленнями та за іншими розробками на [каналі автора](https://t.me/knemchenko_log). Ви також можете [підтримати проект фінансово](https://send.monobank.ua/jar/3ekUcZV1iR), але робіть це після того як задонатие на ЗСУ."
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


def extract_shortcode(url: str) -> str:
    """Extract shortcode from Instagram Reel URL."""
    return url.split("/reel/")[1].split("/")[0]

def locate_video_file(directory: str) -> str:
    """Locate the downloaded video file in the specified directory."""
    return next((file for file in os.listdir(directory) if file.endswith(".mp4")), None)


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
        parts.append(f"*Traceback:*\n``````")

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


async def download_instagram_via_ddinstagram(url: str, chat_id: int, sender: types.User) -> bool:
    try:
        url_to_send = re.sub(r"(https?://)(?:www\.)?(instagram\.com)", r"\1ddinstagram.com", url)
        # url_to_send = re.sub(r"(https?://)(?:www\.)?(instagram\.com)", r"\1knemchenko.chickenkiller.com", url)

        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        message_text = f"{user_link} sent [Instagram Reel]({url_to_send})"

        # Використовуємо розширені налаштування превʼю
        link_preview_options = LinkPreviewOptions(
            is_disabled=False,
            url=url_to_send,  # Явно вказуємо URL для превʼю
            prefer_large_media=True,
            show_above_text=False
        )

        await bot.send_message(
            chat_id,
            message_text,
            parse_mode="Markdown",
            link_preview_options=link_preview_options
        )
        return True
    except Exception as e:
        logger.error(f"Error: {e}")
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


async def download_tiktok_via_vxtiktok(url: str, chat_id: int, sender: types.User) -> bool:
    """Трансформує та надсилає TikTok відео через vxtiktok."""
    try:
        logger.info(f"Processing TikTok URL: {url} sent by user: {sender.id}")

        url_to_send = re.sub(r"(https?://(?:www\.|vm\.)?)tiktok\.com", r"\1vxtiktok.com", url)

        user_link = f"[{sender.full_name or sender.username}](tg://user?id={sender.id})"
        message_text = f"{user_link} sent [TikTok Video]({url_to_send})"

        link_preview_options = LinkPreviewOptions(
            is_disabled=False,
            url=url_to_send,
            prefer_large_media=True,
            show_above_text=False
        )

        await bot.send_message(
            chat_id,
            message_text,
            parse_mode="Markdown",
            link_preview_options=link_preview_options
        )

        return True
    except Exception as e:
        logger.error(f"Error downloading TikTok video for URL: {url}. Error: {e}")
        await notify_admin(url, e, sender)
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
    if match and len(message.text.split(' ')) == 1:
        url = match.group(0)
        sender = message.from_user
        chat_id = message.chat.id
        log_activity(sender.id, chat_id, instagram=True)
        log_chat_usage(chat_id, message.chat.title)
        logger.info(f"Received Instagram Reels link: {url} from user: {sender.id}")
        success = await download_instagram_via_ddinstagram(url, chat_id, sender)

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

@router.message(lambda message: message.text and re.search(TIKTOK_REGEX, message.text))
async def handle_tiktok(message: types.Message):
    """Обробляє повідомлення з TikTok посиланнями."""
    match = re.search(TIKTOK_REGEX, message.text)
    if match and len(message.text.split(' ')) == 1 and message.chat.id not in IGNORED_CHATS_FOR_TIKTOK:
        url = match.group(0)
        sender = message.from_user
        chat_id = message.chat.id
        log_activity(sender.id, chat_id, tiktok=True)  # Потрібно оновити функцію log_activity
        log_chat_usage(chat_id, message.chat.title)
        logger.info(f"Received TikTok link: {url} from user: {sender.id}")
        success = await download_tiktok_via_vxtiktok(url, chat_id, sender)
        if success:
            logger.info(f"Deleting original message with URL: {url}")
            await message.delete()


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


async def main():
    """Start the bot."""
    logger.info("Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
