"""Bounded audio-sample recognition via the existing third-party source."""
import asyncio
import re
import tempfile
from pathlib import Path
from bot_source_probe import audio_media
from lybot_source import BotSourceUnavailable, song_button, subscription_gate

MAX_INPUT = 20 * 1024 * 1024


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

    async def recognize(self, message):
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
                    async with asyncio.timeout(25):
                        downloaded = await self.bot.download_media(media.file_id, file_name=str(input_path))
                    if not downloaded or not input_path.exists() or input_path.stat().st_size > MAX_INPUT:
                        raise BotSourceUnavailable('Media download failed')
                    await make_sample(input_path, sample)
                    return await self.identify(sample)
        finally:
            self.busy = False

    async def identify(self, sample):
        source = self.source
        # Share the text-search lock so responses cannot cross between requests.
        try:
            await asyncio.wait_for(source.lock.acquire(), 2)
        except TimeoutError:
            raise BotSourceUnavailable('Source busy') from None
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
                sent = await source.client.send_audio(source.peer.id, str(sample), disable_notification=True)
                while True:
                    messages = [m async for m in source.client.get_chat_history(source.peer.id, limit=15)
                                if m.id > sent.id and not getattr(m, 'outgoing', False)]
                    for message in reversed(messages):
                        reply_id = getattr(message, 'reply_to_message_id', None)
                        if reply_id and reply_id != sent.id:
                            continue
                        if subscription_gate(message):
                            raise BotSourceUnavailable('Assistant subscription required')
                        title = recognized_title(message)
                        if title:
                            return title
                        text = str(getattr(message, 'text', '') or '').casefold()
                        if any(s in text for s in ('not recognized', 'could not recognize', 'no matches', 'nothing found')):
                            raise BotSourceUnavailable('Song not recognized')
                    await asyncio.sleep(1)
        finally:
            source.lock.release()
