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
from pyrogram.types import InputMediaDocument, Message
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from plugins.antinsfw import check_anti_nsfw
from helper.utils import progress_for_pyrogram, humanbytes, convert
from helper.database import codeflixbots
from config import Config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global dictionary to track ongoing operations
renaming_operations = {}

# Patterns for extracting volume, chapter, season, episode
METADATA_PATTERNS = [
    (re.compile(r'(?:Vol|Volume|V)\s*(\d+)', re.IGNORECASE), ('volume', None)),
    (re.compile(r'(?:Ch|Chapter|C)\s*(\d+)', re.IGNORECASE), (None, 'chapter')),
    (re.compile(r'V(\d+)[^\d]*C(\d+)', re.IGNORECASE), ('volume', 'chapter')),
    (re.compile(r'Volume\s*(\d+)\s*Chapter\s*(\d+)', re.IGNORECASE), ('volume', 'chapter')),
    (re.compile(r'S(\d+)(?:E|EP)(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'S(\d+)[\s-]*(?:E|EP)(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'Season\s*(\d+)\s*Episode\s*(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'\[S(\d+)\]\[E(\d+)\]', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'S(\d+)[^\d]*(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'(?:E|EP|Episode)\s*(\d+)', re.IGNORECASE), (None, 'episode')),
    (re.compile(r'\b(\d+)\b', re.IGNORECASE), (None, 'episode'))
]

QUALITY_PATTERNS = [
    (re.compile(r'\b(\d{3,4}[pi])\b', re.IGNORECASE), lambda m: m.group(1).upper()),
    (re.compile(r'\b(4k|2160p)\b', re.IGNORECASE), lambda m: "4K"),
    (re.compile(r'\b(2k|1440p)\b', re.IGNORECASE), lambda m: "2K"),
    (re.compile(r'\b(HDRip|HDTV|WebRip|BluRay)\b', re.IGNORECASE), lambda m: m.group(1).upper()),
    (re.compile(r'\b(4kX264|4kX265|X264|X265|X26)\b', re.IGNORECASE), lambda m: "X264" if m.group(1).upper() == "X26" else m.group(1).upper()),
    (re.compile(r'\[(\d{3,4}[pi])\]', re.IGNORECASE), lambda m: m.group(1).upper())
]

def extract_season_episode(input_text, rename_mode):
    """Extract season and episode numbers from filename"""
    for pattern, (season_group, episode_group) in SEASON_EPISODE_PATTERNS:
        match = pattern.search(input_text, rename_mode)
        if match:
            season = match.group(1) if season_group else None
            episode = match.group(2) if episode_group else match.group(1)
            logger.info(f"Extracted season: {season}, episode: {episode} from {input_text, rename_mode}")
            return season, episode
    logger.warning(f"No season/episode pattern matched for {input_text, rename_mode}")
    return None, None

def extract_quality(input_text, rename_mode):
    """Extract quality information from filename"""
    for pattern, extractor in QUALITY_PATTERNS:
        match = pattern.search(input_text, rename_mode)
        if match:
            quality = extractor(match)
            logger.info(f"Extracted quality: {quality} from {input_text, rename_mode}")
            return quality
    logger.warning(f"No quality pattern matched for {input_text, rename_mode}")
    return "Unknown"

async def cleanup_files(*paths):
    """Safely remove files if they exist"""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.error(f"Error removing {path}: {e}")

async def process_thumbnail(thumb_path):
    """Process and resize thumbnail image"""
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
    """Add metadata to media file using ffmpeg"""
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

@Client.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def auto_rename_files(client, message):
    """Main handler for auto-renaming files"""
    user_id = message.from_user.id
    format_template = await codeflixbots.get_format_template(user_id)
    
    if not format_template:
        return await message.reply_text("Please set a rename format using /autorename")

    # Get file information
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
        return await message.reply_text("Unsupported file type")

    # NSFW check
    if await check_anti_nsfw(file_name, message):
        return await message.reply_text("NSFW content detected")

    # Prevent duplicate processing
    if file_id in renaming_operations:
        if (datetime.now() - renaming_operations[file_id]).seconds < 10:
            return
    renaming_operations[file_id] = datetime.now()

    try:
        # Extract metadata from filename
        season, episode = extract_season_episode(input_text, rename_mode)
        quality = extract_quality(input_text, rename_mode)
        
        # Replace placeholders in template
        replacements = {
            '{season}': season or 'SKILL_ISSUE',
            '{episode}': episode or 'SKILL_ISSUE',
            '{quality}': quality,
            'Season': season or 'SKILL_ISSUE',
            'Episode': episode or 'SKILL_ISSUE',
            'QUALITY': quality
        }
        
        for placeholder, value in replacements.items():
            format_template = format_template.replace(placeholder, value)

        # Prepare file paths
        ext = os.path.splitext(file_name)[1] or ('.mp4' if media_type == 'video' else '.mp3')
        new_filename = f"{format_template}{ext}"
        download_path = f"downloads/{new_filename}"
        metadata_path = f"metadata/{new_filename}"
        
        os.makedirs(os.path.dirname(download_path), exist_ok=True)
        os.makedirs(os.path.dirname(metadata_path), exist_ok=True)

        # Download file
        msg = await message.reply_text("**Downloading...**")
        try:
            file_path = await client.download_media(
                message,
                file_name=download_path,
                progress=progress_for_pyrogram,
                progress_args=("Downloading...", msg, time.time())
            )
        except Exception as e:
            await msg.edit(f"Download failed: {e}")
            raise

        # Process metadata
        await msg.edit("**Processing metadata...**")
        try:
            await add_metadata(file_path, metadata_path, user_id)
            file_path = metadata_path
        except Exception as e:
            await msg.edit(f"Metadata processing failed: {e}")
            raise

        # Prepare for upload
        await msg.edit("**Preparing upload...**")
        caption = await codeflixbots.get_caption(message.chat.id) or f"**{new_filename}**"
        thumb = await codeflixbots.get_thumbnail(message.chat.id)
        thumb_path = None

        # Handle thumbnail
        if thumb:
            thumb_path = await client.download_media(thumb)
        elif media_type == "video" and message.video.thumbs:
            thumb_path = await client.download_media(message.video.thumbs[0].file_id)
        
        thumb_path = await process_thumbnail(thumb_path)

        # Upload file
        await msg.edit("**Uploading...**")
        try:
            upload_params = {
                'chat_id': message.chat.id,
                'caption': caption,
                'thumb': thumb_path,
                'progress': progress_for_pyrogram,
                'progress_args': ("Uploading...", msg, time.time())
            }

            if media_type == "document":
                await client.send_document(document=file_path, **upload_params)
            elif media_type == "video":
                await client.send_video(video=file_path, **upload_params)
            elif media_type == "audio":
                await client.send_audio(audio=file_path, **upload_params)

            await msg.delete()
        except Exception as e:
            await msg.edit(f"Upload failed: {e}")
            raise

    except Exception as e:
        logger.error(f"Processing error: {e}")
        await message.reply_text(f"Error: {str(e)}")
    finally:
        # Clean up files
        await cleanup_files(download_path, metadata_path, thumb_path)
        renaming_operations.pop(file_id, None)
