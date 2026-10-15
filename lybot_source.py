"""Bounded, serialized LyBot audio fallback. Normal SoundCloud stays primary."""
import asyncio
import logging
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path

from bot_source_probe import audio_media, select_song
from song_cache import get_cached_song, save_cached_song

LOG = logging.getLogger('telegram_music')
MAX_BYTES = 20 * 1024 * 1024


class BotSourceUnavailable(RuntimeError):
    pass


def song_button(message):
    # LyBot's observed search UI: song rows carry title – artist callbacks.
    # Never click subscription, navigation, URL or payment buttons.
    for row in getattr(getattr(message, 'reply_markup', None), 'inline_keyboard', []) or []:
        for button in row:
            label = str(button.text).strip()
            if (' – ' in label or ' - ' in label) and getattr(button, 'callback_data', None):
                if not any(word in label.casefold() for word in ('subscribe', 'premium', 'payment', 'buy ')):
                    return label
    return None


def matches_selection(media, label):
    def normalized(value):
        return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold()
                       if c.isalnum() or unicodedata.category(c).startswith("M"))
    selected_title = normalized(label.rsplit(" – ", 1)[0] if " – " in label
                                else label.rsplit(" - ", 1)[0])
    title = normalized(getattr(media, "title", "") or "")
    return bool(title and selected_title and title == selected_title)


class LyBotSource:
    def __init__(self, client):
        self.client = client
        self.lock = asyncio.Lock()
        self.peer = None
        self.cooldown_until = 0.

    def cached(self, query):
        return get_cached_song('lybot:' + query, False)

    async def resolve(self, query):
        query = query.strip()
        if not query or len(query) > 120 or query.startswith(('http://', 'https://', '/')):
            raise BotSourceUnavailable('LyBot fallback accepts song names only')
        cached = self.cached(query)
        if cached:
            LOG.info('Audio source: LyBot cached Telegram audio')
            return cached
        if time.monotonic() < self.cooldown_until:
            raise BotSourceUnavailable('LyBot temporarily unavailable')
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=2)
        except TimeoutError:
            raise BotSourceUnavailable('LyBot is busy; please try again shortly') from None
        directory = None
        callback = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(28):
                cached = self.cached(query)
                if cached:
                    return cached
                if self.peer is None:
                    peer = await self.client.get_users('LyBot')
                    if not peer.is_bot or str(peer.username).casefold() != 'lybot':
                        raise BotSourceUnavailable('LyBot identity check failed')
                    self.peer = peer
                sent = await self.client.send_message(self.peer.id, query, disable_web_page_preview=True)
                selected_id = None
                selected_label = ""
                while True:
                    messages = [m async for m in self.client.get_chat_history(self.peer.id, limit=15)
                                if m.id > sent.id and not getattr(m, 'outgoing', False)]
                    for message in reversed(messages):
                        media = audio_media(message)
                        if (media is not None and selected_id is not None and message.id > selected_id
                                and matches_selection(media, selected_label)):
                            size = int(getattr(media, 'file_size', 0) or 0)
                            duration = float(getattr(media, 'duration', 0) or 0)
                            if not 0 < size <= MAX_BYTES or not 0 < duration <= 600:
                                raise BotSourceUnavailable('LyBot audio exceeds the playback limits')
                            suffix = Path(str(getattr(media, 'file_name', '') or '')).suffix.lower()
                            if suffix not in {'.mp3', '.m4a', '.ogg', '.opus', '.aac', '.flac', '.wav'}:
                                suffix = '.audio'
                            directory = Path(tempfile.mkdtemp(prefix='mudyar_media_'))
                            path = directory / ('lybot' + suffix)
                            count = 0
                            with path.open('wb') as output:
                                async for chunk in self.client.stream_media(message, limit=21):
                                    count += len(chunk)
                                    if count > size or count > MAX_BYTES:
                                        raise BotSourceUnavailable('LyBot audio size changed')
                                    output.write(chunk)
                            if count != size:
                                raise BotSourceUnavailable('LyBot audio was incomplete')
                            title = str(getattr(media, 'title', '') or query)[:85]
                            artist = str(getattr(media, 'performer', '') or '')[:60]
                            label = f'{title}' + (f' – {artist}' if artist else '') + ' · LyBot'
                            save_cached_song('lybot:' + query, False, str(path), label, duration)
                            LOG.info('Audio source: LyBot Telegram audio ready in %.2fs (%d bytes)',
                                     time.monotonic()-started, count)
                            directory = None  # Playback now owns this disposable file.
                            return str(path), label, duration
                        if selected_id is None:
                            label = song_button(message)
                            if label:
                                selected_id = message.id
                                selected_label = label
                                callback = asyncio.create_task(select_song(self.client, message, label))
                    # Polling is bounded by the total deadline, not per-result retries.
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.cooldown_until = time.monotonic() + 30
            LOG.info('LyBot fallback unavailable (%s)', type(exc).__name__)
            if isinstance(exc, BotSourceUnavailable):
                raise
            raise BotSourceUnavailable('LyBot did not provide playable audio in time') from None
        finally:
            if callback is not None:
                if not callback.done():
                    callback.cancel()
                await asyncio.gather(callback, return_exceptions=True)
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
            self.lock.release()


async def resolve_with_fallback(prepared, query, source, *, primary_timeout=12):
    """Use a cached fallback immediately; otherwise bound primary lookup time."""
    cached = source.cached(query)
    if cached:
        prepared.discard()
        return cached
    try:
        return await asyncio.wait_for(prepared.consume(), timeout=primary_timeout)
    except asyncio.CancelledError:
        prepared.discard()
        raise
    except Exception as exc:
        # PreparedAudio owns late worker results and cleans discarded temp files.
        prepared.discard()
        LOG.info('Primary audio unavailable (%s); trying LyBot fallback', type(exc).__name__)
        return await source.resolve(query)


async def run_live_probe(source, enabled_until):
    """Opt-in startup test of the same provider used by /play; no VC joining."""
    if not 0 < enabled_until - time.time() <= 900:
        return
    path = None
    try:
        from bot_source_probe import decode_sample
        path, title, duration = await source.resolve('Chan Chan Buddha Puzaniya')
        result = await decode_sample(Path(path).read_bytes(), seconds=15)
        LOG.info('LyBot production source probe: title=%s duration=%s bytes=%s pcm_bytes=%s peak=%s exit=%s',
                 title, duration, Path(path).stat().st_size, result['pcm_bytes'], result['peak'], result['exit_code'])
    except Exception as exc:
        LOG.warning('LyBot production source probe failed (%s)', type(exc).__name__)
    finally:
        if path:
            shutil.rmtree(Path(path).parent, ignore_errors=True)
