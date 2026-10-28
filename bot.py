import asyncio
import json
import logging
import os
import subprocess
import uuid
from typing import Dict, Any, List, Tuple

import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =====================================================================
# 1. SOZLAMALAR (CONFIGURATION)
# =====================================================================
from dotenv import load_dotenv
load_dotenv()  # .env faylidagi o'zgaruvchilarni yuklaydi

BOT_TOKEN = os.getenv("BOT_TOKEN")
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://localhost:8081")
DOWNLOAD_DIR = os.path.abspath("downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

URL_CACHE: Dict[str, str] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()


# =====================================================================
# 2. YORDAMCHI METADATA VA VIDEO ISHLOVCHI FUNKSIYALAR
# =====================================================================

def get_video_metadata(file_path: str) -> Tuple[int, int, int]:
    """
    FFprobe orqali videoning aniq davomiyligi (soniya), eni va bo'yini oladi.
    Bu Telegram'da 00:00 bo'lib qolishining oldini oladi.
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=width,height",
            "-of", "json",
            file_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(res.stdout)

        duration = int(float(data.get("format", {}).get("duration", 0)))
        width, height = 1280, 720
        for stream in data.get("streams", []):
            if "width" in stream and "height" in stream:
                width = int(stream["width"])
                height = int(stream["height"])
                break
        return duration, width, height
    except Exception as e:
        logger.error(f"Metadata olishda xatolik: {e}")
        return 0, 1280, 720


def generate_thumbnail(video_path: str, thumb_path: str) -> bool:
    """Video birinchi soniyasidan Telegram uchun muqova rasmi (thumbnail) yasaydi."""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", "00:00:01",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return os.path.exists(thumb_path)
    except Exception:
        return False


# =====================================================================
# 3. SINXRON YT-DLP FUNKSIYALARI
# =====================================================================

def extract_media_info_sync(url: str) -> Dict[str, Any]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "js_runtimes": {"node": {}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_media_sync(url: str, format_id: str, output_base: str) -> str:
    """
    Telegram har doim muammosiz o'ynata olishi uchun videoni
    H.264 (avc1) va AAC formatida, +faststart bayrog'i bilan yuklaydi.
    """
    if format_id == "mp3":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": f"{output_base}.%(ext)s",
            "noplaylist": True,
            "quiet": True,
            "js_runtimes": {"node": {}},
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    else:
        # Telegram taniy oladigan H.264 video va AAC audio tanlanadi
        ydl_opts = {
            "format": f"{format_id}+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": f"{output_base}.%(ext)s",
            "noplaylist": True,
            "quiet": True,
            # Telegram streaming qila olishi uchun moov atom fayl boshiga suriladi:
            "postprocessor_args": {
                "ffmpeg": [
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-c:a", "aac",
                    "-movflags", "+faststart"
                ]
            },
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

        if format_id == "mp3":
            return f"{output_base}.mp3"

        mp4_path = f"{output_base}.mp4"
        if os.path.exists(mp4_path):
            return mp4_path
        return f"{output_base}.mp4"


# =====================================================================
# 4. KLAVIATURA
# =====================================================================

def build_formats_keyboard(info: Dict[str, Any], req_id: str) -> InlineKeyboardMarkup:
    formats: List[Dict[str, Any]] = info.get("formats", [])
    target_heights = [1080, 720, 480, 360]
    selected_formats: Dict[int, str] = {}

    for f in formats:
        vcodec = f.get("vcodec")
        height = f.get("height")
        format_id = f.get("format_id")
        if vcodec and vcodec != "none" and height in target_heights and format_id:
            selected_formats[height] = str(format_id)

    buttons: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for h in target_heights:
        if h in selected_formats:
            fmt_id = selected_formats[h]
            cb_data = f"dl:{req_id}:{fmt_id}"
            if len(cb_data.encode("utf-8")) <= 64:
                row.append(InlineKeyboardButton(text=f"🎬 {h}p", callback_data=cb_data))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
    if row:
        buttons.append(row)

    if not buttons:
        buttons.append([InlineKeyboardButton(text="🎬 Eng yaxshi sifat", callback_data=f"dl:{req_id}:best")])

    buttons.append([InlineKeyboardButton(text="🎵 MP3 (Audio)", callback_data=f"dl:{req_id}:mp3")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# =====================================================================
# 5. HANDLERLAR
# =====================================================================

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>Assalomu alaykum!</b>\n\n"
        "Menga video havolasini yuboring, uni 2 GB gacha hajmda yuklab beraman."
    )


@router.message(F.text.regexp(r"https?://\S+"))
async def handle_video_url(message: Message) -> None:
    url = message.text.strip()
    status_msg = await message.reply("🔍 <i>Video ma'lumotlari olinmoqda...</i>")

    try:
        info = await asyncio.to_thread(extract_media_info_sync, url)
        title = info.get("title", "Video")
        duration = info.get("duration", 0)

        req_id = uuid.uuid4().hex[:8]
        URL_CACHE[req_id] = url
        keyboard = build_formats_keyboard(info, req_id)

        duration_str = f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else "Noma'lum"
        caption = f"📹 <b>{title}</b>\n⏱ Davomiyligi: {duration_str}\n\n👇 Formatni tanlang:"
        await status_msg.edit_text(caption, reply_markup=keyboard)
    except Exception as e:
        logger.exception("Xatolik:")
        await status_msg.edit_text(f"❌ Xatolik yuz berdi: {str(e)[:150]}")


@router.callback_query(F.data.startswith("dl:"))
async def handle_format_selection(callback: CallbackQuery) -> None:
    await callback.answer()
    _, req_id, format_id = callback.data.split(":", maxsplit=2)
    url = URL_CACHE.get(req_id)
    if not url:
        await callback.message.edit_text("⚠️ Havola eskirgan. Qaytadan yuboring.")
        return

    await callback.message.edit_text("⏳ <b>Yuklab olinmoqda va Telegram uchun tayyorlanmoqda...</b>")

    unique_id = f"{req_id}_{uuid.uuid4().hex[:6]}"
    output_base = os.path.join(DOWNLOAD_DIR, unique_id)
    thumb_path = f"{output_base}_thumb.jpg"
    downloaded_file: str | None = None

    try:
        downloaded_file = await asyncio.to_thread(download_media_sync, url, format_id, output_base)
        await callback.message.edit_text("📤 <b>Telegram'ga yuborilmoqda...</b>")

        media_file = FSInputFile(downloaded_file)

        if format_id == "mp3":
            await callback.message.answer_audio(
                audio=media_file,
                caption="🎵 <b>Audio tayyor!</b>"
            )
        else:
            # 1. Aniq davomiylik, en va bo'yni olamiz
            duration, width, height = await asyncio.to_thread(get_video_metadata, downloaded_file)

            # 2. Muqova rasmini (thumbnail) tayyorlaymiz
            has_thumb = await asyncio.to_thread(generate_thumbnail, downloaded_file, thumb_path)
            thumb_file = FSInputFile(thumb_path) if has_thumb else None

            # 3. Telegram'ga barcha parametrlar bilan yuboramiz (00:00 bo'lmaydi!)
            await callback.message.answer_video(
                video=media_file,
                caption="🎬 <b>Video tayyor!</b>",
                duration=duration,
                width=width,
                height=height,
                thumbnail=thumb_file,
                supports_streaming=True
            )

        await callback.message.delete()
    except Exception as e:
        logger.exception("Yuborishda xatolik:")
        await callback.message.edit_text(f"❌ Xatolik: {str(e)[:200]}")
    finally:
        # Fayllarni xotiradan tozalaymiz
        if downloaded_file and os.path.exists(downloaded_file):
            os.remove(downloaded_file)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)


# =====================================================================
# 6. ISHGA TUSHIRISH
# =====================================================================

async def main() -> None:
    local_server = TelegramAPIServer.from_base(LOCAL_API_URL, is_local=True)
    session = AiohttpSession(api=local_server, timeout=1200)

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    logger.info(f"Bot Local Telegram API serverga ulandi: {LOCAL_API_URL}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")