import os
import time
import threading
import logging
import asyncio
from typing import Optional, List, Tuple
from pathlib import Path
import aria2p
import nest_asyncio
import requests
from urllib.parse import urlparse
import re
import difflib
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Fix for nested asyncio loops
nest_asyncio.apply()

# --- CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "11111111"))
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"
WEBHOOK_DOMAIN = os.environ.get("WEBHOOK_DOMAIN") or os.environ.get("RENDER_EXTERNAL_URL")
ARIA2_PORT = 6800
ARIA2_RPC_URL = f"http://127.0.0.1:{ARIA2_PORT}/jsonrpc"
DOWNLOAD_DIR = Path("/app/downloads")
TELEGRAM_FILE_LIMIT = 2000 * 1024 * 1024  # 2GB
STATUS_UPDATE_INTERVAL = 4  # seconds
MAX_MESSAGE_LENGTH = 1800

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ARIA2 CONNECTION (Singleton) ---
_aria2_instance = None

def get_aria2():
    """Lazy initialization of aria2 client"""
    global _aria2_instance
    if _aria2_instance is None:
        _aria2_instance = aria2p.API(
            aria2p.Client(
                host="http://localhost",
                port=ARIA2_PORT,
                secret=""
            )
        )
    return _aria2_instance

# --- FLASK SERVER (KEEP-ALIVE) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running and Active!"

@app.route('/health')
def health():
    """Health check endpoint for monitoring"""
    return {"status": "healthy", "timestamp": time.time()}

def run_web_server():
    """Run Flask server for keep-alive"""
    port = int(os.environ.get('PORT', 8080))
    # Disable Flask's default logger to reduce noise
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)
    app.run(host='0.0.0.0', port=port, threaded=True)

# --- UTILITY FUNCTIONS ---

def is_owner(update: Update) -> bool:
    """Check if user is the owner"""
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        logger.warning(f"Unauthorized access attempt by: {user_id}")
        return False
    return True

def normalize_filename(filename: str) -> str:
    """Normalize filename for fuzzy matching"""
    return re.sub(r'[^0-9a-z]+', ' ', filename.lower()).strip()

def truncate_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> str:
    """Truncate message if it exceeds maximum length"""
    if len(text) > max_length:
        return text[:max_length] + "... (truncated)"
    return text

def format_size(size_bytes: int) -> str:
    """Format file size in human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"

async def safe_edit_message(message, new_text: str, **kwargs):
    """Safely edit message, avoiding rate limits and unchanged content"""
    try:
        if message.text != new_text:
            await message.edit_text(new_text, **kwargs)
    except Exception as e:
        logger.debug(f"Message edit failed (likely no change or rate limit): {e}")

# --- ARIA2 RPC HELPER ---

def aria2_rpc_call(method: str, params: Optional[List] = None, timeout: int = 5) -> Optional[dict]:
    """Make direct RPC call to aria2 with error handling"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": "rpc",
            "method": method,
            "params": params or []
        }
        resp = requests.post(ARIA2_RPC_URL, json=payload, timeout=timeout)
        if resp.ok:
            return resp.json()
        logger.error(f"Aria2 RPC error: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"Aria2 RPC call failed: {e}")
    return None

# --- DOWNLOAD MONITORING ---

async def status_checker(update: Update, context: ContextTypes.DEFAULT_TYPE, download):
    """Monitor download progress with optimized updates"""
    gid = download.gid
    aria2 = get_aria2()
    
    try:
        status_msg = await update.message.reply_text(
            f"⏳ Added: `{download.name}`\nInitializing...",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send status message: {e}")
        return
    
    last_progress = -1
    last_update_time = 0
    min_update_interval = 2  # Minimum seconds between updates
    
    while True:
        try:
            # Refresh download status
            download = aria2.get_download(gid)
            current_time = time.time()
            
            if download.status == "active":
                # Only update if significant change or enough time passed
                current_progress = download.progress
                time_since_update = current_time - last_update_time
                
                # Update if progress changed by 1% or 5+ seconds passed
                should_update = (
                    abs(current_progress - last_progress) >= 1.0 or
                    time_since_update >= 5
                )
                
                if should_update and time_since_update >= min_update_interval:
                    new_text = (
                        f"⬇️ **Downloading**\n"
                        f"Name: `{download.name}`\n"
                        f"Progress: {download.progress_string()}\n"
                        f"Speed: {download.download_speed_string()}\n"
                        f"ETA: {download.eta_string()}"
                    )
                    
                    await safe_edit_message(status_msg, new_text, parse_mode='Markdown')
                    last_progress = current_progress
                    last_update_time = current_time
            
            elif download.status == "complete":
                await safe_edit_message(status_msg, "✅ Download Complete. Preparing upload...")
                await upload_files(update, context, download)
                break
                
            elif download.status == "error":
                error_msg = download.error_message or "Unknown error"
                await safe_edit_message(status_msg, f"❌ Error: {error_msg}")
                break
            
            elif download.status == "removed":
                await safe_edit_message(status_msg, "🗑 Download was removed.")
                break
                
            await asyncio.sleep(STATUS_UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Status checker error: {e}")
            await asyncio.sleep(5)
            # Try to recover from errors a few times before giving up
            continue

# --- FILE UPLOAD ---

async def upload_files(update: Update, context: ContextTypes.DEFAULT_TYPE, download):
    """Upload downloaded files to Telegram with optimized error handling"""
    try:
        files = download.files
        if not files:
            await update.message.reply_text("❌ Error: No files found in download.")
            return

        uploaded_count = 0
        skipped_count = 0

        for file_obj in files:
            path = Path(file_obj.path)
            if not path.exists():
                logger.warning(f"File not found: {path}")
                continue
                
            file_size = path.stat().st_size
            
            # Skip files larger than Telegram limit
            if file_size > TELEGRAM_FILE_LIMIT:
                await update.message.reply_text(
                    f"⚠️ **File Too Large**\n"
                    f"Name: `{path.name}`\n"
                    f"Size: {format_size(file_size)}\n"
                    f"Telegram limit: 2GB", 
                    parse_mode='Markdown'
                )
                skipped_count += 1
                continue
            
            # Upload with progress indication
            msg = await update.message.reply_text(
                f"⬆️ Uploading: `{path.name}` ({format_size(file_size)})...",
                parse_mode='Markdown'
            )
            
            try:
                with open(path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        caption=f"📂 {path.name}",
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=60
                    )
                await msg.delete()
                uploaded_count += 1
            except Exception as upload_error:
                logger.error(f"Upload failed for {path.name}: {upload_error}")
                await safe_edit_message(msg, f"❌ Upload Failed: {upload_error}")
                skipped_count += 1

        # Cleanup: Remove files from disk
        try:
            download.remove(force=True, files=True)
            summary = f"✅ Task Finished. Uploaded: {uploaded_count}"
            if skipped_count > 0:
                summary += f", Skipped: {skipped_count}"
            await update.message.reply_text(summary)
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
            await update.message.reply_text(f"✅ Upload complete, but cleanup failed: {e}")

    except Exception as e:
        logger.error(f"Critical upload error: {e}")
        await update.message.reply_text(f"❌ Critical Upload Error: {e}")

# --- BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    if not is_owner(update):
        return
    
    await update.message.reply_text(
        "👋 **Welcome Owner!**\n\n"
        "Send me:\n"
        "• Magnet Link\n"
        "• Direct Download URL\n"
        "• .torrent file\n\n"
        "I will download and upload it back to you.\n\n"
        "**Commands:**\n"
        "/lsdownloads - List downloaded files\n"
        "/forceupload <filename> - Force upload a file\n"
        "/aria [gid] - Check aria2 status\n"
        "/debugaria - Debug aria2 connection",
        parse_mode='Markdown'
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle magnet links and direct download URLs"""
    if not is_owner(update):
        return
    
    link = update.message.text.strip()
    
    # Validate link format
    if not (link.startswith("http://") or link.startswith("https://") or link.startswith("magnet:")):
        await update.message.reply_text("❌ Invalid link format. Send a valid HTTP(S) or magnet link.")
        return

    try:
        aria2 = get_aria2()
        download = aria2.add_uris([link])
        asyncio.create_task(status_checker(update, context, download))
    except Exception as e:
        logger.error(f"Failed to add link: {e}")
        await update.message.reply_text(f"❌ Failed to add link: {e}")

async def handle_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle .torrent file uploads"""
    if not is_owner(update):
        return
    
    try:
        doc = update.message.document
        file_name = doc.file_name
        
        if not file_name.endswith('.torrent'):
            await update.message.reply_text("❌ Please send a valid .torrent file.")
            return

        # Download .torrent file
        new_file = await context.bot.get_file(doc.file_id)
        temp_path = f"/tmp/torrent_{int(time.time())}_{os.getpid()}.torrent"
        await new_file.download_to_drive(temp_path)
        
        # Add to Aria2
        aria2 = get_aria2()
        download = aria2.add_torrent(temp_path)
        
        # Cleanup temp file
        try:
            os.remove(temp_path)
        except Exception:
            pass
            
        asyncio.create_task(status_checker(update, context, download))
        
    except Exception as e:
        logger.error(f"Error handling .torrent: {e}")
        await update.message.reply_text(f"❌ Error handling .torrent: {e}")

async def debug_aria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug aria2 connection and status"""
    if not is_owner(update):
        return

    result = aria2_rpc_call("aria2.getGlobalStat")
    
    if result:
        text = truncate_message(str(result))
        await update.message.reply_text(f"✅ Aria2 Response:\n`{text}`", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Failed to connect to aria2")

async def lsdownloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List files in downloads directory"""
    if not is_owner(update):
        return

    try:
        if not DOWNLOAD_DIR.exists():
            await update.message.reply_text("📭 Downloads directory does not exist.")
            return

        files: List[Tuple[str, int]] = []
        for entry in DOWNLOAD_DIR.iterdir():
            if entry.is_file():
                files.append((entry.name, entry.stat().st_size))

        if not files:
            await update.message.reply_text("📭 No files in downloads directory.")
            return

        # Sort by size (largest first)
        files.sort(key=lambda x: x[1], reverse=True)
        
        lines = [f"{name} — {format_size(size)}" for name, size in files[:50]]  # Limit to 50 files
        text = "\n".join(lines)
        text = truncate_message(text)

        await update.message.reply_text(f"📁 Downloads ({len(files)} files):\n`{text}`", parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error listing downloads: {e}")
        await update.message.reply_text(f"❌ Error listing downloads: {e}")

async def aria_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show aria2 status for GID or global stats"""
    if not is_owner(update):
        return

    try:
        args = context.args if hasattr(context, 'args') else []
        
        if args:
            gid = args[0]
            result = aria2_rpc_call(
                "aria2.tellStatus",
                [gid, ["status", "totalLength", "completedLength", "downloadSpeed", 
                       "connections", "numSeeders", "numConnectedPeers"]]
            )
        else:
            result = aria2_rpc_call("aria2.getGlobalStat")

        if result:
            text = truncate_message(str(result))
            await update.message.reply_text(f"✅ Aria2:\n`{text}`", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Aria2 RPC error")

    except Exception as e:
        logger.error(f"Aria status error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def forceupload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force upload a file from downloads directory"""
    if not is_owner(update):
        return

    args = context.args if hasattr(context, 'args') else []
    if not args:
        await update.message.reply_text("Usage: /forceupload <filename or pattern>")
        return

    query_raw = " ".join(args)
    query_normalized = normalize_filename(query_raw)

    try:
        if not DOWNLOAD_DIR.exists():
            await update.message.reply_text("📭 Downloads directory does not exist.")
            return

        # Get all files
        all_files = [(f.name, f) for f in DOWNLOAD_DIR.iterdir() if f.is_file()]
        
        if not all_files:
            await update.message.reply_text("📭 No files in downloads directory.")
            return

        # Find matches using normalized substring search
        matches = []
        for name, path in all_files:
            if query_normalized in normalize_filename(name):
                matches.append(path)

        # Fallback to fuzzy matching
        if not matches:
            normalized_files = [(normalize_filename(n), p) for n, p in all_files]
            choices = [n for n, _ in normalized_files]
            close = difflib.get_close_matches(query_normalized, choices, n=5, cutoff=0.6)
            
            for match in close:
                for norm_name, path in normalized_files:
                    if norm_name == match:
                        matches.append(path)
                        break

        if not matches:
            suggestions = "\n".join([n for n, _ in all_files[:10]])
            await update.message.reply_text(
                f"❌ No files matched: `{query_raw}`\n\n"
                f"Available files:\n{truncate_message(suggestions)}",
                parse_mode='Markdown'
            )
            return

        # Use largest file if multiple matches
        file_path = max(matches, key=lambda p: p.stat().st_size)
        file_size = file_path.stat().st_size

        if file_size > TELEGRAM_FILE_LIMIT:
            await update.message.reply_text(
                f"⚠️ File exceeds Telegram limit (2GB): {format_size(file_size)}"
            )
            return

        msg = await update.message.reply_text(
            f"⬆️ Uploading: `{file_path.name}` ({format_size(file_size)})...",
            parse_mode='Markdown'
        )

        try:
            with open(file_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    caption=f"📂 {file_path.name}",
                    read_timeout=600,
                    write_timeout=600,
                    connect_timeout=60
                )
            
            await msg.delete()
            
            # Remove file after upload
            try:
                file_path.unlink()
            except Exception as e:
                logger.error(f"Failed to remove file: {e}")
            
            await update.message.reply_text("✅ Upload complete. File removed from disk.")

        except Exception as upload_error:
            logger.error(f"Force upload failed: {upload_error}")
            await safe_edit_message(msg, f"❌ Upload Failed: {upload_error}")

    except Exception as e:
        logger.error(f"Force upload error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

# --- WEBHOOK CLEANUP ---

async def cleanup_webhook(token: str) -> bool:
    """Delete existing webhook and wait for confirmation"""
    try:
        logger.info("Checking for existing webhook...")
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/getWebhookInfo",
            timeout=10
        )
        
        if not resp.ok:
            logger.error(f"Failed to get webhook info: {resp.text}")
            return False
        
        info = resp.json().get('result', {})
        url = info.get('url')
        
        if not url:
            logger.info("No webhook configured.")
            return True
        
        logger.warning(f"Existing webhook detected: {url}. Removing...")
        
        del_resp = requests.post(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            timeout=10
        )
        
        if not del_resp.ok or not del_resp.json().get('result'):
            logger.error(f"Failed to delete webhook: {del_resp.text}")
            return False
        
        # Wait for webhook to clear
        for attempt in range(10):
            await asyncio.sleep(1)
            check_resp = requests.post(
                f"https://api.telegram.org/bot{token}/getWebhookInfo",
                timeout=10
            )
            
            if check_resp.ok:
                result = check_resp.json().get('result', {})
                if not result.get('url'):
                    logger.info("Webhook successfully cleared.")
                    return True
        
        logger.warning("Webhook not cleared within timeout.")
        return False
        
    except Exception as e:
        logger.error(f"Webhook cleanup error: {e}")
        return False

# --- MAIN ENTRY POINT ---

def setup_handlers(app_bot):
    """Register all bot handlers"""
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("debugaria", debug_aria))
    app_bot.add_handler(CommandHandler("lsdownloads", lsdownloads))
    app_bot.add_handler(CommandHandler("aria", aria_status_cmd))
    app_bot.add_handler(CommandHandler("forceupload", forceupload))
    app_bot.add_handler(MessageHandler(filters.Document.ALL, handle_torrent_file))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

async def main():
    """Main entry point"""
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable is missing.")
        exit(1)
    
    # Build application
    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    setup_handlers(app_bot)
    
    if USE_WEBHOOK:
        logger.info("Starting in WEBHOOK mode")
        
        if not WEBHOOK_DOMAIN:
            logger.error("WEBHOOK_DOMAIN or RENDER_EXTERNAL_URL is not set.")
            exit(1)

        # Parse and normalize domain
        parsed = urlparse(WEBHOOK_DOMAIN)
        domain = (parsed.netloc or parsed.path).rstrip('/')
        
        url_path = f"/webhook/{BOT_TOKEN}"
        port = int(os.environ.get('PORT', 8080))
        webhook_url = f"https://{domain}{url_path}"
        
        logger.info(f"Webhook URL: {webhook_url}")
        
        # Run webhook
        await app_bot.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=webhook_url
        )
    else:
        logger.info("Starting in POLLING mode")
        
        # Cleanup webhook before polling
        await cleanup_webhook(BOT_TOKEN)
        
        # Start keep-alive server in background
        server_thread = threading.Thread(target=run_web_server, daemon=True)
        server_thread.start()
        logger.info("Keep-alive server started")
        
        # Run polling
        await app_bot.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

if __name__ == '__main__':
    asyncio.run(main())
