"""Bounded audio-sample recognition via the existing third-party source."""
from source_replies import ReplyInbox
import asyncio
import re
import tempfile
import time
import logging
from collections import OrderedDict
from pathlib import Path
from bot_source_probe import audio_media
from lybot_source import AudioCandidate, BotSourceUnavailable, song_button, subscription_gate

MAX_INPUT = 20 * 1024 * 1024
LOG = logging.getLogger("telegram_music")


class DeliveredAudioCache:
    """Only this bot's Telegram file IDs; bounded, expiring, no audio bytes."""
    def __init__(self):
        self.items = OrderedDict()

    def get(self, key):
        if not key:
            return None
        row = self.items.get(key)
        if row and row[0] > time.monotonic():
            self.items.move_to_end(key)
            return row[1]
        self.items.pop(key, None)
        return None

    def discard(self, key):
        self.items.pop(key, None)

    def put(self, key, title, audio):
        if not key or not audio or not getattr(audio, "file_id", None):
            return
        self.items[key] = (time.monotonic() + 7*86400,
                           (title, audio.file_id, getattr(audio, "duration", 0) or 0))
        self.items.move_to_end(key)
        while len(self.items) > 64:
            self.items.popitem(last=False)


def recognition_media(message):
    if message is None:
        return None
    for name in ('video', 'video_note', 'audio', 'voice'):
        media = getattr(message, name, None)
        if media:
            return media
    doc = getattr(message, 'document', None)
    if doc and str(getattr(doc, 'mime_type', '')).startswith(('audio/', 'video/')):
        return doc
    return None


def recognized_title(message):
    media = audio_media(message)
    if media and getattr(media, 'title', None):
        title = str(media.title).strip()
        artist = str(getattr(media, 'performer', '') or '').strip()
        return (title + (f' – {artist}' if artist else ''))[:120]
    label = song_button(message)
    if label:
        return re.sub(r'^\s*[•·]?\s*\d+:\d+\s*[•·]\s*', '', label)[:120]
    return None


async def make_sample(input_path, output_path):
    process = await asyncio.create_subprocess_exec(
        'ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(input_path),
        '-t', '20', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame',
        '-b:a', '64k', str(output_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(12):
            code = await process.wait()
        if code or not output_path.exists() or output_path.stat().st_size < 1000:
            raise BotSourceUnavailable('No usable audio sample')
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class SongRecognizer:
    def __init__(self, bot, source):
        self.bot, self.source = bot, source
        self.busy = False

    async def recognize(self, message, *, with_audio=False):
        media = recognition_media(message)
        if not media or not 0 < int(getattr(media, 'file_size', 0) or 0) <= MAX_INPUT:
            raise ValueError('Reply to audio/video under 20 MB')
        if self.busy:
            raise BotSourceUnavailable('Recognition busy')
        self.busy = True
        try:
            async with asyncio.timeout(65):
                with tempfile.TemporaryDirectory(prefix='mudyar_recognize_') as folder:
                    input_path, sample = Path(folder)/'input', Path(folder)/'sample.mp3'
                    stage = time.monotonic()
                    async with asyncio.timeout(25):
                        downloaded = await self.bot.download_media(media.file_id, file_name=str(input_path))
                    if not downloaded or not input_path.exists() or input_path.stat().st_size > MAX_INPUT:
                        raise BotSourceUnavailable('Media download failed')
                    LOG.info("Recognition input ready after %.2fs", time.monotonic() - stage)
                    stage = time.monotonic()
                    await make_sample(input_path, sample)
                    LOG.info("Recognition sample ready after %.2fs", time.monotonic() - stage)
                    stage = time.monotonic()
                    result = await self.identify(sample, with_audio=with_audio)
                    LOG.info("Recognition identified after %.2fs", time.monotonic() - stage)
                    return result
        finally:
            self.busy = False

    async def identify(self, sample, *, with_audio=False):
        source = self.source
        # Share the text-search lock so responses cannot cross between requests.
        try:
            await asyncio.wait_for(source.lock.acquire(), 2)
        except TimeoutError:
            raise BotSourceUnavailable('Source busy') from None
        inbox = None
        try:
            async with asyncio.timeout(25):
                if source.peer is None:
                    peer = await source.client.get_users(source.username)
                    if not peer.is_bot or str(peer.username).casefold() != 'voiceshazambot':
                        raise BotSourceUnavailable('Recognition bot identity mismatch')
                    source.peer = peer
                if not source.started:
                    await source.client.send_message(source.peer.id, '/start')
                    await asyncio.sleep(.5)
                    source.started = True
                inbox = ReplyInbox(source.client, source.peer.id)
                sent = await source.client.send_audio(source.peer.id, str(sample), disable_notification=True)
                while True:
                    messages = await inbox.batch(sent.id)
                    for message in messages:
                        reply_id = getattr(message, 'reply_to_message_id', None)
                        if reply_id and reply_id != sent.id:
                            continue
                        if subscription_gate(message):
                            raise BotSourceUnavailable('Assistant subscription required')
                        title = recognized_title(message)
                        if title:
                            if not with_audio:
                                return title
                            media = audio_media(message)
                            candidate = None
                            if media:
                                size = int(getattr(media, 'file_size', 0) or 0)
                                duration = float(getattr(media, 'duration', 0) or 0)
                                if not 0 < size <= MAX_INPUT or duration > 600:
                                    raise BotSourceUnavailable('Recognized audio exceeds limits')
                                candidate = AudioCandidate(source, title, message,
                                                           title + ' · VoiceShazamBot', duration or None)
                            return title, candidate
                        text = str(getattr(message, 'text', '') or '').casefold()
                        if any(s in text for s in ('not recognized', 'could not recognize', 'no matches', 'nothing found')):
                            raise BotSourceUnavailable('Song not recognized')
        finally:
            if inbox is not None:
                inbox.close()
            source.lock.release()


async def send_recognized_audio(bot, message, path, title, duration, markup, *, file_id=None):
    """Upload our own MP3 with our caption and inline controls."""
    with tempfile.TemporaryDirectory(prefix='mudyar_recognition_upload_') as folder:
        upload = file_id or str(path)
        if not file_id and Path(path).suffix.lower() != '.mp3':
            upload = str(Path(folder) / 'song.mp3')
            process = await asyncio.create_subprocess_exec(
                'ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(path),
                '-vn', '-c:a', 'libmp3lame', '-b:a', '128k', upload,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                async with asyncio.timeout(35):
                    code = await process.wait()
                if code or not Path(upload).exists() or not 0 < Path(upload).stat().st_size <= MAX_INPUT:
                    raise BotSourceUnavailable('MP3 conversion failed')
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        async with asyncio.timeout(45):
            stage = time.monotonic()
            identity = getattr(bot, "me", None) or await bot.get_me()
            username = str(identity.username or '').lstrip('@')
            from html import escape
            from pyrogram.enums import ParseMode
            caption = escape('@' + username if username else str(identity.first_name or 'မုဒြာ Music'))
            result = await bot.send_audio(message.chat.id, upload, title=title[:120],
                                        duration=int(duration or 0), caption=caption,parse_mode=ParseMode.HTML,
                                        reply_to_message_id=message.id, reply_markup=markup)
            LOG.info("Recognition audio sent after %.2fs (Telegram reuse=%s)", time.monotonic() - stage, bool(file_id))
            return result
