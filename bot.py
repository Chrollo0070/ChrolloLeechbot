import os
import time
import threading
import logging
import asyncio
import aria2p
import nest_asyncio
import requests
from urllib.parse import urlparse
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Fix for nested asyncio loops
nest_asyncio.apply()

# --- CONFIGURATION ---
# Get these from Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# Default to the ID you provided if not set in Env
OWNER_ID = int(os.environ.get("OWNER_ID", "11111111")) 

# Use webhook instead of polling when set to '1'
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"
# Domain used for webhook URL (Render sets RENDER_EXTERNAL_URL automatically)
WEBHOOK_DOMAIN = os.environ.get("WEBHOOK_DOMAIN") or os.environ.get("RENDER_EXTERNAL_URL")

ARIA2_PORT = 6800
DOWNLOAD_DIR = "/app/downloads"

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ARIA2 CONNECT ---
# Connects to the local aria2 instance running in background
aria2 = aria2p.API(
    aria2p.Client(
        host="http://localhost",
        port=ARIA2_PORT,
        secret=""
    )
)

# --- FLASK SERVER (KEEP-ALIVE) ---
# Render requires a web server listening on a port to keep the service "healthy"
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running and Active!"

def run_web_server():
    # Render assigns the PORT env variable automatically
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# --- BOT LOGIC ---

def is_owner(update: Update):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        logger.warning(f"Unauthorized access attempt by: {user_id}")
        return False
    return True

async def status_checker(update: Update, context: ContextTypes.DEFAULT_TYPE, download):
    """
    Monitors the download progress every few seconds.
    """
    gid = download.gid
    status_msg = await update.message.reply_text(f"⏳ Added: `{download.name}`\nInitializing...", parse_mode='Markdown')
    
    last_text = ""
    
    while True:
        try:
            # Refresh download status
            download = aria2.get_download(gid)
            
            if download.status == "active":
                # Construct status message
                progress = download.progress_string()
                speed = download.download_speed_string()
                eta = download.eta_string()
                
                new_text = (f"⬇️ **Downloading**\n"
                            f"Name: `{download.name}`\n"
                            f"Progress: {progress}\n"
                            f"Speed: {speed}\n"
                            f"ETA: {eta}")
                
                # Only edit message if text changed (prevents API flooding)
                if new_text != last_text:
                    # Check to update only every ~5 seconds ideally, 
                    # but here we rely on loop sleep
                    await status_msg.edit_text(new_text, parse_mode='Markdown')
                    last_text = new_text
            
            elif download.status == "complete":
                await status_msg.edit_text("✅ Download Complete. Preparing upload...")
                await upload_files(update, context, download)
                break
                
            elif download.status == "error":
                await status_msg.edit_text(f"❌ Error: {download.error_message}")
                break
            
            elif download.status == "removed":
                await status_msg.edit_text("🗑 Download was removed.")
                break
                
            await asyncio.sleep(4) # Check every 4 seconds
            
        except Exception as e:
            logger.error(f"Checker error: {e}")
            await asyncio.sleep(5)

async def upload_files(update: Update, context: ContextTypes.DEFAULT_TYPE, download):
    """
    Iterates through downloaded files and uploads them to Telegram.
    """
    try:
        files = download.files
        if not files:
            await update.message.reply_text("❌ Error: No files found in download.")
            return

        for file_obj in files:
            path = file_obj.path
            if not os.path.exists(path):
                continue
                
            file_name = os.path.basename(path)
            file_size = os.path.getsize(path)
            
            # Telegram Limit check (2GB - overhead)
            LIMIT_BYTES = 2000 * 1024 * 1024 
            
            if file_size > LIMIT_BYTES:
                await update.message.reply_text(
                    f"⚠️ **File Too Large**\n"
                    f"Name: `{file_name}`\n"
                    f"Size: {file_obj.length_string()}\n"
                    f"Telegram limits bots to 2GB uploads.", 
                    parse_mode='Markdown'
                )
                continue
            
            # Uploading
            msg = await update.message.reply_text(f"⬆️ Uploading: `{file_name}`...", parse_mode='Markdown')
            
            try:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=open(path, 'rb'),
                    caption=f"📂 {file_name}",
                    read_timeout=300,  # 5 min timeout for large files
                    write_timeout=300,
                    connect_timeout=300
                )
                await msg.delete() # Delete the "Uploading" message on success
            except Exception as upload_error:
                await msg.edit_text(f"❌ Upload Failed: {upload_error}")

        # CLEANUP: Remove files from disk to free up Render space
        download.remove(force=True, files=True)
        await update.message.reply_text("✅ Task Finished & Cache Cleared.")

    except Exception as e:
        await update.message.reply_text(f"❌ Critical Upload Error: {e}")

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    await update.message.reply_text(
        "👋 **Welcome Owner!**\n\n"
        "Send me:\n"
        "1. A Magnet Link\n"
        "2. A Direct Download URL\n"
        "3. A `.torrent` file\n\n"
        "I will download it and upload it back to you.",
        parse_mode='Markdown'
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    
    link = update.message.text.strip()
    
    # Simple check to see if it looks like a link
    if not (link.startswith("http") or link.startswith("magnet:")):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

    try:
        download = aria2.add_uris([link])
        # Start the status checker in background
        asyncio.create_task(status_checker(update, context, download))
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to add link: {e}")

async def handle_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    
    try:
        # Get the file from Telegram
        doc = update.message.document
        file_id = doc.file_id
        file_name = doc.file_name
        
        if not file_name.endswith('.torrent'):
            await update.message.reply_text("❌ Please send a valid .torrent file.")
            return

        # Download .torrent file locally
        new_file = await context.bot.get_file(file_id)
        temp_path = f"temp_{int(time.time())}.torrent"
        await new_file.download_to_drive(temp_path)
        
        # Add to Aria2
        download = aria2.add_torrent(temp_path)
        
        # Remove the temp .torrent file
        if os.path.exists(temp_path):
            os.remove(temp_path)
            
        asyncio.create_task(status_checker(update, context, download))
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error handling .torrent: {e}")


async def debug_aria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only command to query aria2 JSON-RPC getGlobalStat and return output.
    """
    if not is_owner(update):
        return

    try:
        resp = requests.post(
            "http://127.0.0.1:6800/jsonrpc",
            json={"jsonrpc":"2.0","id":"test","method":"aria2.getGlobalStat"},
            timeout=5
        )

        if resp.ok:
            # Return a short message (truncate if very long)
            text = resp.text
            if len(text) > 1800:
                text = text[:1800] + "... (truncated)"
            await update.message.reply_text(f"✅ Aria2 Response:\n`{text}`", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ No response. Status: {resp.status_code} - {resp.text}")

    except Exception as e:
        await update.message.reply_text(f"❌ Execution Error: {e}")


async def lsdownloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: list files in the downloads directory with sizes."""
    if not is_owner(update):
        return

    try:
        files = []
        if os.path.exists(DOWNLOAD_DIR):
            for entry in os.scandir(DOWNLOAD_DIR):
                if entry.is_file():
                    size = entry.stat().st_size
                    files.append((entry.name, size))

        if not files:
            await update.message.reply_text("📭 No files in downloads directory.")
            return

        lines = []
        for name, size in sorted(files, key=lambda x: x[1], reverse=True):
            lines.append(f"{name} — {size//1024} KiB")

        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n... (truncated)"

        await update.message.reply_text(f"📁 Downloads:\n`{text}`", parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"❌ Error listing downloads: {e}")


async def aria_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: show aria2 status for a given GID or global stats.
    Usage: /aria <gid>  or just /aria to show global stats
    """
    if not is_owner(update):
        return

    try:
        args = context.args if hasattr(context, 'args') else []
        if args:
            gid = args[0]
            payload = {"jsonrpc":"2.0","id":"q","method":"aria2.tellStatus","params":[gid,["status","totalLength","completedLength","downloadSpeed","connections","numSeeders","numConnectedPeers"]]}
        else:
            payload = {"jsonrpc":"2.0","id":"q","method":"aria2.getGlobalStat","params":[]}

        resp = requests.post("http://127.0.0.1:6800/jsonrpc", json=payload, timeout=5)
        if resp.ok:
            text = resp.text
            if len(text) > 1800:
                text = text[:1800] + "... (truncated)"
            await update.message.reply_text(f"✅ Aria2:\n`{text}`", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Aria2 RPC error: {resp.status_code} {resp.text}")

    except Exception as e:
        await update.message.reply_text(f"❌ Execution Error: {e}")


async def forceupload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: force upload a file from the downloads folder by partial filename match.
    Usage: /forceupload <partial-or-full-filename>
    """
    if not is_owner(update):
        return

    args = context.args if hasattr(context, 'args') else []
    if not args:
        await update.message.reply_text("Usage: /forceupload <partial-or-full-filename>")
        return

    query = " ".join(args).lower()

    try:
        if not os.path.exists(DOWNLOAD_DIR):
            await update.message.reply_text("📭 Downloads directory does not exist.")
            return

        matches = []
        for entry in os.scandir(DOWNLOAD_DIR):
            if entry.is_file():
                if query in entry.name.lower():
                    matches.append(entry.path)

        if not matches:
            await update.message.reply_text("❌ No files matched that query in downloads.")
            return

        # If multiple matches, pick the largest (likely the target file)
        matches.sort(key=lambda p: os.path.getsize(p), reverse=True)
        file_path = matches[0]
        file_size = os.path.getsize(file_path)

        LIMIT_BYTES = 2000 * 1024 * 1024
        if file_size > LIMIT_BYTES:
            await update.message.reply_text(f"⚠️ File is larger than Telegram limit (2GB): {file_size} bytes")
            return

        file_name = os.path.basename(file_path)
        msg = await update.message.reply_text(f"⬆️ Forcing upload: `{file_name}` ({file_size//1024} KiB)...", parse_mode='Markdown')

        try:
            with open(file_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    caption=f"📂 {file_name}",
                    read_timeout=600,
                    write_timeout=600,
                    connect_timeout=300
                )
            await msg.delete()
            # Remove file after successful upload to free space
            try:
                os.remove(file_path)
            except Exception:
                logger.exception("Failed to remove uploaded file from disk")
            await update.message.reply_text("✅ Forced upload finished and file removed.")

        except Exception as upload_error:
            await msg.edit_text(f"❌ Upload Failed: {upload_error}")

    except Exception as e:
        await update.message.reply_text(f"❌ Execution Error: {e}")

# --- MAIN ENTRY POINT ---
if __name__ == '__main__':
    if not BOT_TOKEN:
        print("CRITICAL: BOT_TOKEN env variable is missing.")
        exit(1)
    # If using polling mode, ensure no webhook is set (Telegram will return 409 if webhook exists)
    if not USE_WEBHOOK:
        try:
            logger.info("Checking Telegram webhook status before starting polling...")
            resp = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=10)
            if resp.ok:
                info = resp.json().get('result', {})
                url = info.get('url')
                if url:
                    logger.warning(f"Detected active webhook: {url}. Deleting webhook to allow polling.")
                    delr = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
                    if delr.ok and delr.json().get('result'):
                        logger.info("Requested webhook deletion. Waiting for Telegram to clear webhook...")
                        # Wait until Telegram reports no webhook URL or until timeout
                        cleared = False
                        for i in range(10):
                            try:
                                time.sleep(1)
                                chk = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=10)
                                if chk.ok:
                                    result = chk.json().get('result', {})
                                    if not result.get('url'):
                                        cleared = True
                                        logger.info("Webhook cleared by Telegram.")
                                        break
                                    else:
                                        logger.info(f"Webhook still present, waiting... (attempt {i+1})")
                            except Exception as ee:
                                logger.debug(f"Error while checking webhook status: {ee}")
                        if not cleared:
                            logger.warning("Webhook was not cleared within timeout; polling may still receive 409 Conflict.")
                    else:
                        logger.error(f"Failed to delete webhook: {delr.text}")
        except Exception as e:
            logger.error(f"Failed to check/delete webhook: {e}")

    # Start whichever web server is needed
    if USE_WEBHOOK:
        # In webhook mode we will let the bot run the web server for incoming updates.
        print("--- Starting Telegram Bot in WEBHOOK mode ---")
        # Setup Bot
        app_bot = ApplicationBuilder().token(BOT_TOKEN).build()

        # Add Handlers
        app_bot.add_handler(CommandHandler("start", start))
        app_bot.add_handler(CommandHandler("debugaria", debug_aria))
        app_bot.add_handler(CommandHandler("lsdownloads", lsdownloads))
        app_bot.add_handler(CommandHandler("aria", aria_status_cmd))
        app_bot.add_handler(CommandHandler("forceupload", forceupload))
        app_bot.add_handler(MessageHandler(filters.Document.ALL, handle_torrent_file))
        app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

        if not WEBHOOK_DOMAIN:
            logger.error("WEBHOOK_DOMAIN or RENDER_EXTERNAL_URL is not set. Cannot register webhook.")
            exit(1)

        # Normalize WEBHOOK_DOMAIN: strip scheme and trailing slashes
        parsed = urlparse(WEBHOOK_DOMAIN)
        domain = parsed.netloc or parsed.path  # netloc when scheme present, else path
        domain = domain.rstrip('/')

        url_path = f"/webhook/{BOT_TOKEN}"
        port = int(os.environ.get('PORT', 8080))
        webhook_url = f"https://{domain}{url_path}"
        logger.info(f"Registering webhook URL: {webhook_url}")

        # Run webhook server that listens for Telegram updates
        app_bot.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=webhook_url
        )
    else:
        print("--- Starting Keep-Alive Web Server ---")
        # Run Flask in a separate thread (keeps the instance alive for Render)
        server_thread = threading.Thread(target=run_web_server)
        server_thread.daemon = True
        server_thread.start()

        print("--- Starting Telegram Bot (polling) ---")
        # Setup Bot
        app_bot = ApplicationBuilder().token(BOT_TOKEN).build()

        # Add Handlers
        app_bot.add_handler(CommandHandler("start", start))
        app_bot.add_handler(CommandHandler("debugaria", debug_aria))
        app_bot.add_handler(CommandHandler("lsdownloads", lsdownloads))
        app_bot.add_handler(CommandHandler("aria", aria_status_cmd))
        app_bot.add_handler(CommandHandler("forceupload", forceupload))
        app_bot.add_handler(MessageHandler(filters.Document.ALL, handle_torrent_file))
        app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

        # Run Bot (polling)
        app_bot.run_polling()