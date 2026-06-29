import os
import io
import logging
import asyncio
from aiohttp import web
from PIL import Image, ImageDraw
from rembg import remove
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

# State machine for holding bulk queue media assets
USER_DATA = {}

# --- IMAGE CORE OPERATIONS ---

def convert_image(img_bytes, target_format) -> io.BytesIO:
    img = Image.open(io.BytesIO(img_bytes))
    if img.mode in ('RGBA', 'LA') and target_format.upper() in ('JPEG', 'JPG'):
        img = img.convert('RGB')
    
    out_io = io.BytesIO()
    if target_format.upper() == 'PDF':
        img.save(out_io, format='PDF', save_all=True)
    else:
        img.save(out_io, format=target_format.upper())
    out_io.seek(0)
    return out_io

def compress_resize_image(img_bytes, resize_pct=50, quality=60) -> io.BytesIO:
    img = Image.open(io.BytesIO(img_bytes))
    if resize_pct != 100:
        new_size = (int(img.width * (resize_pct / 100)), int(img.height * (resize_pct / 100)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    
    out_io = io.BytesIO()
    if img.mode in ('RGBA', 'LA'):
        img.save(out_io, format='PNG', optimize=True)
    else:
        img.save(out_io, format='JPEG', quality=quality)
    out_io.seek(0)
    return out_io

def remove_bg(img_bytes) -> io.BytesIO:
    return io.BytesIO(remove(img_bytes))

def add_watermark_text(img_bytes, text="ImgCraftxBot") -> io.BytesIO:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    txt_layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
    
    d = ImageDraw.Draw(txt_layer)
    d.text((20, img.height - 40), text, fill=(255, 255, 255, 130)) 
    
    watermarked = Image.alpha_composite(img, txt_layer)
    out_io = io.BytesIO()
    watermarked.convert("RGB").save(out_io, format="JPEG")
    out_io.seek(0)
    return out_io

# --- TELEGRAM BOT LOGIC ROUTINES ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ **Welcome to @ImgCraftxBot!** 🛠️\n\n"
        "Drop one or multiple images here. I support bulk queue processing for format shifts, "
        "compressions, background dropouts, and text watermarking layouts!"
    )

async def handle_incoming_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("Please provide a valid image asset framework format.")
        return

    bot_file = await context.bot.get_file(file_id)
    img_buffer = io.BytesIO()
    await bot_file.download_to_memory(out=img_buffer)
    img_bytes = img_buffer.getvalue()

    if user_id not in USER_DATA:
        USER_DATA[user_id] = {"files": []}
    
    USER_DATA[user_id]["files"].append(img_bytes)
    count = len(USER_DATA[user_id]["files"])

    keyboard = [
        [InlineKeyboardButton("🔄 Convert Format", callback_data="menu_convert")],
        [InlineKeyboardButton("🗜️ Compress & Resize (50%)", callback_data="action_compress")],
        [InlineKeyboardButton("✂️ Remove Background", callback_data="action_rembg")],
        [InlineKeyboardButton("🏷️ Add Watermark", callback_data="action_watermark")],
        [InlineKeyboardButton("🧹 Clear Queue", callback_data="menu_clear")]
    ]

    await update.message.reply_text(
        f"📥 Image compiled into operational queue! Total current files: **{count}**\nSelect your pipeline action:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in USER_DATA or not USER_DATA[user_id]["files"]:
        await query.edit_message_text("Session context expired. Send new images.")
        return

    data = query.data
    files = USER_DATA[user_id]["files"]

    if data == "menu_convert":
        keyboard = [
            [InlineKeyboardButton("➡️ JPG", callback_data="to_jpg"), InlineKeyboardButton("➡️ PNG", callback_data="to_png")],
            [InlineKeyboardButton("➡️ WEBP", callback_data="to_webp"), InlineKeyboardButton("➡️ PDF", callback_data="to_pdf")]
        ]
        await query.edit_message_text("Choose target conversion container standard:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "menu_clear":
        USER_DATA[user_id]["files"] = []
        await query.edit_message_text("Queue scrubbed completely.")
        return

    await query.edit_message_text("⚡ Processing engine active... compiling pipeline execution vectors.")

    try:
        target_format = data.split("_")[1].upper() if data.startswith("to_") else None

        for idx, raw_bytes in enumerate(files):
            if target_format:
                processed = convert_image(raw_bytes, target_format)
                ext = target_format.lower()
            elif data == "action_compress":
                processed = compress_resize_image(raw_bytes)
                ext = "jpg"
            elif data == "action_rembg":
                processed = remove_bg(raw_bytes)
                ext = "png"
            elif data == "action_watermark":
                processed = add_watermark_text(raw_bytes)
                ext = "jpg"
            else:
                continue

            filename = f"craft_output_{idx+1}.{ext}"
            processed.name = filename
            await query.message.reply_document(document=processed, filename=filename)

        await query.message.reply_text("✅ All transformations finished successfully.")
    except Exception as err:
        logger.error(f"Processing route failed: {err}")
        await query.message.reply_text("❌ An error dropped inside your media rendering execution thread.")
    finally:
        USER_DATA[user_id]["files"] = []

# --- WEB SERVER INTERFACE TO PREVENT RENDER IDLE ---

async def health_endpoint(request):
    return web.Response(text="ImgCraftxBot matrix engine is live.")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Keepalive hook web server established on port {PORT}")

# --- MAIN RUNTIME HOOKS ---

def main():
    if not TOKEN:
        logger.error("System missing configuration environment assignment for TELEGRAM_BOT_TOKEN.")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_incoming_images))
    application.add_handler(CallbackQueryHandler(handle_callback))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_web_server())

    logger.info("Initializing polling routine loops for @ImgCraftxBot...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
