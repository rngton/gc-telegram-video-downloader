import os
import sys
import json
import uuid
import asyncio
import shutil
import random
import logging
import re
from contextlib import asynccontextmanager # New import for lifespan
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

# For Telegram Webhook handling
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ExtBot

# --- Setup Logging ---
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("spidybot_gc")

# --- Constants ---
TEMP_DIR = "/tmp/temp_processing"
os.makedirs(TEMP_DIR, exist_ok=True)

# --- Load Instagram Cookies from Environment Variables ---
def load_cookies():
    cookies = []
    i = 1
    while True:
        cookie = os.environ.get(f"INSTAGRAM_COOKIES{i}")
        if not cookie:
            break
        cookies.append(cookie)
        i += 1
    if not cookies:
        log.warning("⚠️ No Instagram cookies found. The bot may not work for private content.")
    return cookies

COOKIES = load_cookies()

# --- Telegram Bot Application Setup ---
# Initialize Application globally, but don't start it yet.
# We will initialize/start it within FastAPI's lifespan.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    log.critical("❌ TELEGRAM_BOT_TOKEN environment variable not set. Exiting.")
    sys.exit(1)

application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# Add handlers
application.add_handler(CommandHandler("start", start_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_download))


# --- FastAPI Application Setup with Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Handles startup and shutdown events for the FastAPI application.
    This is where python-telegram-bot's Application will be initialized.
    """
    log.info("FastAPI app starting up. Initializing Telegram Application...")
    try:
        await application.initialize()
        log.info("Telegram Application initialized successfully.")
    except Exception as e:
        log.critical(f"Failed to initialize Telegram Application: {e}", exc_info=True)
        sys.exit(1) # Exit if bot fails to initialize

    yield # This yields control to the FastAPI application to handle requests

    log.info("FastAPI app shutting down. Shutting down Telegram Application...")
    try:
        await application.shutdown()
        log.info("Telegram Application shut down gracefully.")
    except Exception as e:
        log.error(f"Error during Telegram Application shutdown: {e}", exc_info=True)


app = FastAPI(
    title="Telegram Bot Backend",
    description="Processes Telegram messages to download videos.",
    version="1.0.0",
    lifespan=lifespan # Link the lifespan manager to the FastAPI app
)


# --- Async Shell Executor ---
async def run_command(command: str) -> tuple[int, str, str]:
    """
    Executes a shell command asynchronously and returns its exit code, stdout, and stderr.
    Does NOT raise an error on non-zero exit code.
    """
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

# --- Telegram Bot Handlers (defined after application is built) ---
async def start_command(update: Update, context):
    await update.message.reply_text("👋 Hello! Send me any Instagram Reel, Post, or IGTV link, and I'll try to download it for you.")

async def handle_video_download(update: Update, context):
    message = update.message
    message_text = message.text
    chat_id = message.chat_id
    
    url_match = re.search(r'https?://[^\s]+', message_text)
    if not url_match:
        return await message.reply_text("⚠️ Please send a valid video URL.")

    input_url = url_match.group(0)
    
    session_id = uuid.uuid4().hex[:8]
    session_path = os.path.join(TEMP_DIR, session_id)
    os.makedirs(session_path, exist_ok=True)

    log.info(f"[{session_id}] Received URL: {input_url} from Chat ID: {chat_id}")
    status_message = await message.reply_text("🔄 Processing...")

    last_error = "Unknown error."
    is_instagram_url = "instagram.com" in input_url

    try:
        if is_instagram_url:
            url_match = re.search(r'https?://(?:www\.)?instagram\.com/(reel|p|tv)/([a-zA-Z0-9_-]+)/?', input_url)
            if not url_match:
                raise ValueError("Invalid Instagram URL format. Please provide a Reel, Post, or IGTV link.")
            if not COOKIES:
                 raise ValueError("Instagram URL detected but no cookies are configured. This is often required for Instagram downloads.")

        cookies_to_use = None
        if COOKIES and is_instagram_url:
            shuffled_cookies = random.sample(COOKIES, len(COOKIES))
            cookies_to_use = shuffled_cookies
        
        attempts_left = len(cookies_to_use) if cookies_to_use else 1
        current_cookie_index = 0

        while attempts_left > 0:
            cookie_str = cookies_to_use[current_cookie_index] if cookies_to_use else None
            attempt_num = (len(cookies_to_use) if cookies_to_use else 1) - attempts_left + 1
            log.info(f"[{session_id}] Attempt {attempt_num}/{len(cookies_to_use) if cookies_to_use else 1} {'with cookie' if cookie_str else 'without cookie'}.")
            
            cookie_path = None
            try:
                if cookie_str:
                    cookie_path = os.path.join(session_path, "cookie.txt")
                    with open(cookie_path, "w") as f:
                        f.write(cookie_str)

                await status_message.edit_text(f"📄 Fetching video metadata (Attempt {attempt_num})...")
                info_command = f'yt-dlp --cookies "{cookie_path}" --dump-json "{input_url}"' if cookie_path else f'yt-dlp --dump-json "{input_url}"'
                exit_code, stdout, stderr = await run_command(info_command)
                
                if exit_code != 0:
                    last_error = stderr
                    log.warning(f"[{session_id}] yt-dlp metadata failed (Attempt {attempt_num}): {last_error}")
                    if "No address associated with hostname" in last_error and is_instagram_url:
                        raise RuntimeError("Network block detected for Instagram. Instagram downloads might not work from this server.")
                    elif "login is required" in last_error.lower() and is_instagram_url:
                        attempts_left -= 1
                        current_cookie_index += 1
                        continue
                    else:
                        raise RuntimeError(f"Metadata extraction failed: {last_error}")
                
                metadata = json.loads(stdout)
                caption = metadata.get("description", "")
                if caption:
                    caption = caption[:1024] # Telegram caption limit

                await status_message.edit_text(f"⬇️ Downloading best quality video and audio (Attempt {attempt_num})...")
                base_output_name = os.path.join(session_path, "media")
                download_command = f'yt-dlp --cookies "{cookie_path}" -f "bv*+ba/b" -o "{base_output_name}.%(ext)s" "{input_url}"' if cookie_path else f'yt-dlp -f "bv*+ba/b" -o "{base_output_name}.%(ext)s" "{input_url}"'
                exit_code, stdout, stderr = await run_command(download_command)

                if exit_code != 0:
                    last_error = stderr
                    log.warning(f"[{session_id}] yt-dlp download failed (Attempt {attempt_num}): {last_error}")
                    if "No address associated with hostname" in last_error and is_instagram_url:
                         raise RuntimeError("Network block detected for Instagram. Instagram downloads might not work from this server.")
                    elif "login is required" in last_error.lower() and is_instagram_url:
                        attempts_left -= 1
                        current_cookie_index += 1
                        continue
                    else:
                        raise RuntimeError(f"Download failed: {last_error}")

                downloaded_file = None
                for f_name in os.listdir(session_path):
                    if f_name.startswith("media."):
                        downloaded_file = os.path.join(session_path, f_name)
                        break
                
                if not downloaded_file or not os.path.exists(downloaded_file):
                    raise FileNotFoundError("Downloaded media file not found after yt-dlp operation.")

                output_filename = f"vid_{session_id}.mp4"
                output_path = os.path.join(session_path, output_filename)

                await status_message.edit_text(f"🎞️ Re-encoding video with FFmpeg for optimal quality and size (Attempt {attempt_num})...")
                ffmpeg_command = (
                    f'ffmpeg -y -i "{downloaded_file}" '
                    f'-c:v libx264 -preset medium -crf 23 '
                    f'-c:a aac -b:a 192k -movflags +faststart "{output_path}"'
                )
                exit_code, stdout, stderr = await run_command(ffmpeg_command)
                
                if exit_code != 0:
                    raise RuntimeError(f"FFmpeg encoding failed: {stderr}")
                
                if not os.path.exists(output_path):
                    raise FileNotFoundError("FFmpeg output file not found.")

                log.info(f"[{session_id}] Video processed successfully: {output_path}")
                
                await status_message.edit_text("📤 Uploading to Telegram...")
                with open(output_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=os.path.basename(output_path),
                        caption=caption
                    )
                
                await status_message.delete()
                return

            except Exception as e:
                log.warning(f"[{session_id}] Attempt {attempt_num} failed with error: {e}")
                last_error = str(e)
                if cookie_path and os.path.exists(cookie_path):
                    os.remove(cookie_path)
                
                if "Network block detected for Instagram" in last_error:
                    raise
                
                attempts_left -= 1
                current_cookie_index += 1
                if attempts_left == 0:
                    raise


        raise RuntimeError("Unexpected state: No successful download or error was propagated.")


    except Exception as e:
        final_error_message = f"[{session_id}] Final error: {e}"
        log.error(final_error_message)
        
        user_msg = (
            "❌ Failed to process video.\n"
            "• It may be deleted/private.\n"
            "• Or our login session expired.\n"
        )
        if "Invalid Instagram URL" in str(e):
            user_msg = "⚠️ That doesn't look like a valid Instagram video URL."
        elif "Network block detected for Instagram" in str(e):
            user_msg = "⚠️ It seems there's a network block preventing access to Instagram from this server. Instagram downloads might not work."
        elif "login is required" in last_error.lower():
            user_msg = "⚠️ Login required to access this video. All login attempts failed. Please notify the bot administrator to update cookies."
        elif "Media file not found" in str(e):
            user_msg = "Could not find the media file after download. It might be unavailable or a temporary issue."
        elif "FFmpeg encoding failed" in str(e):
            user_msg = "Video encoding failed. The downloaded file might be corrupted or in an unsupported format."
        elif "Download failed" in str(e):
            user_msg = f"Video download failed: {str(e)}"
        
        try:
            await status_message.edit_text(user_msg)
        except Exception:
            await message.reply_text(user_msg)
    finally:
        if os.path.exists(session_path):
            log.info(f"[{session_id}] Cleaning up temporary directory: {session_path}")
            shutil.rmtree(session_path)


# --- FastAPI Endpoint to receive Telegram Webhooks ---
@app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    log.info("Received Telegram webhook.")
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        log.error(f"Error processing webhook update: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error processing update: {e}")

# --- Root endpoint for Cloud Run health check ---
@app.get("/")
async def root():
    return {"message": "Telegram Bot Backend is running. Please configure Telegram webhook to /telegram_webhook endpoint."}
