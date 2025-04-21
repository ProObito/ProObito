import os
import re
import time
import shutil
import asyncio
import logging
from datetime import datetime
from PIL import Image
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaDocument, Message, InlineKeyboardMarkup, InlineKeyboardButton
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from plugins.antinsfw import check_anti_nsfw
from helper.utils import progress_for_pyrogram, humanbytes, convert
from helper.database import codeflixbots
from config import Config

# configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# global dictionary to track ongoing operations (user-specific)
renaming_operations = {}

# small caps mapping
SMALL_CAPS_MAP = {
    'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ', 'f': 'ꜰ', 'g': 'ɢ', 'h': 'ʜ', 'i': 'ɪ',
    'j': 'ᴊ', 'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ', 'o': 'ᴏ', 'p': 'ᴘ', 'q': 'Q', 'r': 'ʀ',
    's': 'ꜱ', 't': 'ᴛ', 'u': 'ᴜ', 'v': 'ᴠ', 'w': 'ᴡ', 'x': 'x', 'y': 'ʏ', 'z': 'ᴢ'
}

def to_small_caps(text):
    """convert text to small caps using unicode characters"""
    return ''.join(SMALL_CAPS_MAP.get(c.lower(), c) for c in text)

# enhanced regex patterns for season, episode, chapter, and volume extraction
SEASON_EPISODE_PATTERNS = [
    # standard patterns (S01E02, S01EP02)
    (re.compile(r'S(\d+)(?:E|EP)(\d+)'), ('season', 'episode')),
    # patterns with spaces/dashes (S01 E02, S01-EP02)
    (re.compile(r'S(\d+)[\s-]*(?:E|EP)(\d+)'), ('season', 'episode')),
    # full text patterns (Season 1 Episode 2)
    (re.compile(r'Season\s*(\d+)\s*Episode\s*(\d+)', re.IGNORECASE), ('season', 'episode')),
    # patterns with brackets/parentheses ([S01][E02])
    (re.compile(r'\[S(\d+)\]\[E(\d+)\]'), ('season', 'episode')),
    # fallback patterns (S01 13, Episode 13)
    (re.compile(r'S(\d+)[^\d]*(\d+)'), ('season', 'episode')),
    (re.compile(r'(?:E|EP|Episode)\s*(\d+)', re.IGNORECASE), (None, 'episode')),
    # chapter and volume patterns
    (re.compile(r'Chapter\s*(\d+)', re.IGNORECASE), (None, 'chapter')),
    (re.compile(r'Volume\s*(\d+)', re.IGNORECASE), (None, 'volume')),
    # final fallback (standalone number)
    (re.compile(r'\b(\d+)\b'), (None, 'episode'))
]

# quality detection patterns
QUALITY_PATTERNS = [
    (re.compile(r'\b(\d{3,4}[pi])\b', re.IGNORECASE), lambda m: m.group(1)),  # 1080p, 720p
    (re.compile(r'\b(4k|2160p)\b', re.IGNORECASE), lambda m: "4k"),
    (re.compile(r'\b(2k|1440p)\b', re.IGNORECASE), lambda m: "2k"),
    (re.compile(r'\b(HDRip|HDTV)\b', re.IGNORECASE), lambda m: m.group(1)),
    (re.compile(r'\b(4kX264|4kx265)\b', re.IGNORECASE), lambda m: m.group(1)),
    (re.compile(r'\[(\d{3,4}[pi])\]', re.IGNORECASE), lambda m: m.group(1))  # [1080p]
]

async def extract_season_episode(source_text, user_id):
    """extract season, episode, chapter, or volume numbers from source (filename or caption)"""
    extraction_mode = await codeflixbots.get_extraction_mode(user_id)
    logger.info(f"Extracting metadata from {extraction_mode} for user {user_id}: {source_text}")
    
    for pattern, (season_group, metadata_type) in SEASON_EPISODE_PATTERNS:
        match = pattern.search(source_text)
        if match:
            season = match.group(1) if season_group else None
            value = match.group(1) if metadata_type else match.group(2)
            logger.info(f"Extracted {metadata_type}: {value}, season: {season} from {source_text}")
            return season, value, metadata_type
    logger.warning(f"No metadata pattern matched for {source_text}")
    return None, None, None

async def extract_quality(source_text, user_id):
    """extract quality information from source (filename or caption)"""
    extraction_mode = await codeflixbots.get_extraction_mode(user_id)
    logger.info(f"Extracting quality from {extraction_mode} for user {user_id}: {source_text}")
    
    for pattern, extractor in QUALITY_PATTERNS:
        match = pattern.search(source_text)
        if match:
            quality = extractor(match)
            logger.info(f"Extracted quality: {quality} from {source_text}")
            return quality
    logger.warning(f"No quality pattern matched for {source_text}")
    return "Unknown"


async def process_thumbnail(thumb_path):
    """process and resize thumbnail image"""
    if not thumb_path or not os.path.exists(thumb_path):
        return None
    
    try:
        with Image.open(thumb_path) as img:
            img = img.convert("RGB").resize((320, 320))
            img.save(thumb_path, "JPEG")
        return thumb_path
    except Exception as e:
        logger.error(f"Thumbnail processing failed: {e}")
        await cleanup_files(thumb_path)
        return None

async def add_metadata(input_path, output_path, user_id):
    """add metadata to media file using ffmpeg"""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found in PATH")
    
    metadata = {
        'title': await codeflixbots.get_title(user_id),
        'artist': await codeflixbots.get_artist(user_id),
        'author': await codeflixbots.get_author(user_id),
        'video_title': await codeflixbots.get_video(user_id),
        'audio_title': await codeflixbots.get_audio(user_id),
        'subtitle': await codeflixbots.get_subtitle(user_id)
    }
    
    cmd = [
        ffmpeg,
        '-i', input_path,
        '-metadata', f'title={metadata["title"]}',
        '-metadata', f'artist={metadata["artist"]}',
        '-metadata', f'author={metadata["author"]}',
        '-metadata:s:v', f'title={metadata["video_title"]}',
        '-metadata:s:a', f'title={metadata["audio_title"]}',
        '-metadata:s:s', f'title={metadata["subtitle"]}',
        '-map', '0',
        '-c', 'copy',
        '-loglevel', 'error',
        output_path
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {stderr.decode()}")

@Client.on_message(filters.private & filters.command("extraction"))
async def set_extract_command(client, message):
    """handle extraction command to choose extraction mode"""
    user_id = message.from_user.id
    current_mode = await codeflixbots.get_extraction_mode(user_id)

    # create inline keyboard with filename and caption options, showing ✅ for current mode
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"filename {'✅' if current_mode == 'filename' else ''}",
                callback_data=f"extract_filename_{user_id}"
            ),
            InlineKeyboardButton(
                f"caption {'✅' if current_mode == 'caption' else ''}",
                callback_data=f"extract_caption_{user_id}"
            )
        ]
    ])

    await message.reply_text(
        to_small_caps(
            f"ᴄᴜʀʀᴇɴᴛ ᴇxᴛʀᴀᴄᴛɪᴏɴ ᴍᴏᴅᴇ: **{current_mode.capitalize()}**\n"
            "ᴄʜᴏᴏꜱᴇ ᴛʜᴇ ꜱᴏᴜʀᴄᴇ ꜰᴏʀ ᴍᴇᴛᴀᴅᴀᴛᴀ ᴇxᴛʀᴀᴄᴛɪᴏɴ (ꜱᴇᴀꜱᴏɴ, ᴇᴘɪꜱᴏᴅᴇ, Qᴜᴀʟɪᴛʏ, ᴇᴛᴄ.):"
        ),
        reply_markup=keyboard
    )

@Client.on_callback_query(filters.regex(r"extract_(filename|caption)_(\d+)"))
async def handle_extract_callback(client, callback_query):
    """handle callback queries for extraction mode selection"""
    mode = callback_query.matches[0].group(1)  # filename or caption
    user_id = int(callback_query.matches[0].group(2))

    if callback_query.from_user.id != user_id:
        await callback_query.answer(to_small_caps("ᴛʜɪꜱ ʙᴜᴛᴛᴏɴ ɪꜱ ɴᴏᴛ ꜰᴏʀ ʏᴏᴜ!"), show_alert=True)
        return

    # update extraction mode in the database
    await codeflixbots.set_extraction_mode(user_id, mode)

    # update inline keyboard to reflect new mode with ✅
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"filename {'✅' if mode == 'filename' else ''}",
                callback_data=f"extract_filename_{user_id}"
            ),
            InlineKeyboardButton(
                f"caption {'✅' if mode == 'caption' else ''}",
                callback_data=f"extract_caption_{user_id}"
            )
        ]
    ])

    await callback_query.message.edit_text(
        to_small_caps(
            f"ᴇxᴛʀᴀᴄᴛɪᴏɴ ᴍᴏᴅᴇ ꜱᴇᴛ ᴛᴏ: **{mode.capitalize()}**\n"
            f"ᴍᴇᴛᴀᴅᴀᴛᴀ (ꜱᴇᴀꜱᴏɴ, ᴇᴘɪꜱᴏᴅᴇ, Qᴜᴀʟɪᴛʏ, ᴇᴛᴄ.) ᴡɪʟʟ ɴᴏᴡ � Wtedy be extracted from the {mode}."
        ),
        reply_markup=keyboard
    )
    await callback_query.answer(to_small_caps(f"ꜱᴇᴛ ᴛᴏ {mode} ᴍᴏᴅᴇ"))

async def cleanup_files(*file_paths):
    """Clean up temporary files"""
    for file_path in file_paths:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to cleanup file {file_path}: {e}")

@Client.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def auto_rename_files(client, message):
    """main handler for auto-renaming files"""
    user_id = message.from_user.id
    format_template = await codeflixbots.get_format_template(user_id)
    
    if not format_template:
        return await message.reply_text(to_small_caps("ᴘʟᴇᴀꜱᴇ ꜱᴇᴛ ᴀ ʀᴇɴᴀᴍᴇ ꜰᴏʀᴍᴀᴛ ᴜꜱɪɴɢ /autorename"))

    try:
        # get file information
        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name
            file_size = message.document.file_size
            media_type = "document"
        elif message.video:
            file_id = message.video.file_id
            file_name = message.video.file_name or "video"
            file_size = message.video.file_size
            media_type = "video"
        elif message.audio:
            file_id = message.audio.file_id
            file_name = message.audio.file_name or "audio"
            file_size = message.audio.file_size
            media_type = "audio"
        else:
            return await message.reply_text(to_small_caps("ᴜɴꜱᴜᴘᴘᴏʀᴛᴇᴅ ꜰɪʟᴇ ᴛʏᴘᴇ"))

        # nsfw check
        if await check_anti_nsfw(file_name, message):
            return await message.reply_text(to_small_caps("ɴꜱꜰᴡ ᴄᴏɴᴛᴇɴᴛ ᴅᴇᴛᴇᴄᴛᴇᴅ"))

        # determine extraction source (filename or caption)
        extraction_mode = await codeflixbots.get_extraction_mode(user_id)
        source_text = message.caption or file_name if extraction_mode == "caption" else file_name

        # extract metadata from source
        season, value, metadata_type = await extract_season_episode(source_text, user_id)
        quality = await extract_quality(source_text, user_id)
        
        # replace placeholders in template
        replacements = {
            '{season}': season or 'XX',
            '{episode}': value or 'XX' if metadata_type == 'episode' else 'XX',
            '{chapter}': value or 'XX' if metadata_type == 'chapter' else 'XX',
            '{volume}': value or 'XX' if metadata_type == 'volume' else 'XX',
            '{quality}': quality,
            'Season': season or 'XX',
            'Episode': value or 'XX' if metadata_type == 'episode' else 'XX',
            'Chapter': value or 'XX' if metadata_type == 'chapter' else 'XX',
            'Volume': value or 'XX' if metadata_type == 'volume' else 'XX',
            'QUALITY': quality
        }
        
        for placeholder, value in replacements.items():
            format_template = format_template.replace(placeholder, value)

        # prepare file paths
        ext = os.path.splitext(file_name)[1] or ('.mp4' if media_type == 'video' else '.mp3')
        new_filename = f"{format_template}{ext}"
        download_path = f"downloads/{user_id}/{new_filename}"
        metadata_path = f"metadata/{user_id}/{new_filename}"
        
        os.makedirs(os.path.dirname(download_path), exist_ok=True)
        os.makedirs(os.path.dirname(metadata_path), exist_ok=True)

        # download file
        msg = await message.reply_text(to_small_caps("**ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ...**"))
        try:
            file_path = await client.download_media(
                message,
                file_name=download_path,
                progress=progress_for_pyrogram,
                progress_args=(to_small_caps("ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ..."), msg, time.time())
            )
        except Exception as e:
            await msg.edit(to_small_caps(f"ᴅᴏᴡɴʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ: {e}"))
            await cleanup_files(download_path)
            return

        # process metadata
        await msg.edit(to_small_caps("**ᴘʀᴏᴄᴇꜱꜱɪɴɢ ᴍᴇᴛᴀᴅᴀᴛᴀ...**"))
        try:
            await add_metadata(file_path, metadata_path, user_id)
            file_path = metadata_path
        except Exception as e:
            await msg.edit(to_small_caps(f"ᴍᴇᴛᴀᴅᴀᴛᴀ ᴘʀᴏᴄᴇꜱꜱɪɴɢ ꜰᴀɪʟᴇᴅ: {e}"))
            await cleanup_files(download_path, metadata_path)
            return

        # prepare for upload
        await msg.edit(to_small_caps("**ᴘʀᴇᴘᴀʀɪɴɢ ᴜᴘʟᴏᴀᴅ...**"))
        caption = await codeflixbots.get_caption(message.chat.id) or f"**{new_filename}**"
        thumb = await codeflixbots.get_thumbnail(message.chat.id)
        thumb_path = None

        # handle thumbnail
        if thumb:
            thumb_path = await client.download_media(thumb)
        elif media_type == "video" and message.video.thumbs:
            thumb_path = await client.download_media(message.video.thumbs[0].file_id)
        
        thumb_path = await process_thumbnail(thumb_path)

        # upload file
        await msg.edit(to_small_caps("**ᴜᴘʟᴏᴀᴅɪɴɢ...**"))
        try:
            upload_params = {
                'chat_id': message.chat.id,
                'caption': caption,
                'thumb': thumb_path,
                'progress': progress_for_pyrogram,
                'progress_args': (to_small_caps("ᴜᴘʟᴏᴀᴅɪɴɢ..."), msg, time.time())
            }

            if media_type == "document":
                await client.send_document(document=file_path, **upload_params)
            elif media_type == "video":
                await client.send_video(video=file_path, **upload_params)
            elif media_type == "audio":
                await client.send_audio(audio=file_path, **upload_params)

            await msg.delete()
        except Exception as e:
            await msg.edit(to_small_caps(f"ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ: {e}"))
            raise
        finally:
            # Cleanup files after upload
            await cleanup_files(download_path, metadata_path, thumb_path)

    except Exception as e:
        logger.error(f"Processing error for user {user_id}: {e}")
        await message.reply_text(to_small_caps(f"ᴇʀʀᴏʀ: {str(e)}"))
        await cleanup_files(download_path, metadata_path, thumb_path)
