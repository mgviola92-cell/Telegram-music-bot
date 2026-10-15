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
APPROVED_BOTS = frozenset({"LyBot", "Song_Vkm_bot", "VoiceShazamBot", "VKMusicalRobot", "finder_muzbot", "MusicToolsRobot", "vkm_bot"})


class BotSourceUnavailable(RuntimeError):
    pass


def subscription_gate(message):
    text = str(getattr(message, 'text', '') or '').casefold()
    buttons = [str(b.text).casefold() for row in getattr(getattr(message, 'reply_markup', None), 'inline_keyboard', []) or [] for b in row]
    return 'need to subscribe' in text and any('check subscriptions' in label for label in buttons)


def song_button(message, query=None):
    # LyBot's observed search UI: song rows carry title – artist callbacks.
    # Never click subscription, navigation, URL or payment buttons.
    for row in getattr(getattr(message, 'reply_markup', None), 'inline_keyboard', []) or []:
        for button in row:
            label = str(button.text).strip()
            if (' – ' in label or ' — ' in label or ' - ' in label) and getattr(button, 'callback_data', None):
                if not any(word in label.casefold() for word in ('subscribe', 'premium', 'payment', 'buy ')):
                    if query is None or matches_query(type("Label", (), {"title": label, "performer": ""})(), query):
                        return label
    return None


def matches_selection(media, label):
    def normalized(value):
        return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold()
                       if c.isalnum() or unicodedata.category(c).startswith("M"))
    import re
    label = re.sub(r"^\s*[•·]?\s*\d+:\d+\s*[•·]\s*", "", label)
    parts = re.split(r"\s+[—–-]\s+", label)
    title = normalized(getattr(media, "title", "") or "")
    return bool(title and any(normalized(part) == title for part in parts))


class AudioCandidate:
    def __init__(self, source, query, message, label, duration):
        self.source, self.query, self.message = source, query, message
        self.label, self.duration = label, duration


def matches_query(media, query):
    import re
    def tokens(value):
        normalized = unicodedata.normalize("NFKC", str(value)).casefold()
        words = "".join(c if c.isalnum() or unicodedata.category(c).startswith("M") else " " for c in normalized)
        return set(words.split())
    wanted = tokens(query)
    actual = tokens(str(getattr(media, 'title', '') or '') + ' ' + str(getattr(media, 'performer', '') or ''))
    return bool(wanted and len(wanted & actual) / len(wanted) >= 0.6)


class LyBotSource:
    def __init__(self, client, username='LyBot', *, trace=False):
        if username not in APPROVED_BOTS:
            raise ValueError('Unapproved music bot')
        self.client, self.username, self.trace = client, username, trace
        self.lock = asyncio.Lock()
        self.peer = None
        self.cooldown_until = 0.
        self.candidates = {}
        self.started = username == 'LyBot'

    def cached(self, query):
        return get_cached_song(self.username.casefold() + ':' + query, False)

    async def find(self, query):
        query = query.strip()
        if not query or len(query) > 120 or query.startswith(('http://', 'https://', '/')):
            raise BotSourceUnavailable('Music bot search accepts song names only')
        key = query.casefold()
        existing = self.candidates.get(key)
        if existing and existing[1] > time.monotonic():
            return existing[0]
        if time.monotonic() < self.cooldown_until:
            raise BotSourceUnavailable('Source temporarily unavailable')
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=2)
        except TimeoutError:
            raise BotSourceUnavailable('Source is busy') from None
        callback = None
        seen = set()
        try:
            async with asyncio.timeout(18):
                existing = self.candidates.get(key)
                if existing and existing[1] > time.monotonic():
                    return existing[0]
                if self.peer is None:
                    peer = await self.client.get_users(self.username)
                    if not peer.is_bot or str(peer.username).casefold() != self.username.casefold():
                        raise BotSourceUnavailable('Music bot identity check failed')
                    self.peer = peer
                if not self.started:
                    await self.client.send_message(self.peer.id, '/start')
                    await asyncio.sleep(0.5)
                    self.started = True
                sent = await self.client.send_message(self.peer.id, query, disable_web_page_preview=True)
                selected_id, selected_label = None, ''
                while True:
                    messages = [m async for m in self.client.get_chat_history(self.peer.id, limit=15)
                                if m.id > sent.id and not getattr(m, 'outgoing', False)]
                    for message in reversed(messages):
                        media = audio_media(message)
                        if subscription_gate(message):
                            LOG.info("Music source @%s requires assistant subscriptions; skipping this source", self.username)
                            raise BotSourceUnavailable("Assistant subscription required")
                        if self.trace and message.id not in seen:
                            from stream_probe import safe_error
                            seen.add(message.id)
                            buttons = [[str(b.text)[:80] for b in row] for row in getattr(getattr(message, 'reply_markup', None), 'inline_keyboard', []) or []]
                            LOG.info('Music bot probe @%s response: audio=%s text=%s buttons=%s', self.username, bool(media), safe_error(str(getattr(message, 'text', '') or getattr(message, 'caption', '') or ''))[:400], buttons)
                        matched = (selected_id is not None and message.id > selected_id and matches_selection(media, selected_label)) if media else False
                        if media and (matched or (selected_id is None and matches_query(media, query))):
                            size = int(getattr(media, 'file_size', 0) or 0)
                            duration = float(getattr(media, 'duration', 0) or 0)
                            if not 0 < size <= MAX_BYTES or duration > 600:
                                raise BotSourceUnavailable('Audio exceeds playback limits')
                            title = str(getattr(media, 'title', '') or selected_label or Path(str(getattr(media, 'file_name', '') or '')).stem or 'Telegram audio')[:100]
                            artist = str(getattr(media, 'performer', '') or '')[:70]
                            label = title + (f' – {artist}' if artist and artist.casefold() not in title.casefold() else '') + ' · ' + self.username
                            candidate = AudioCandidate(self, query, message, label, duration or None)
                            self.candidates.pop(key, None)
                            if len(self.candidates) >= 24:
                                self.candidates.pop(next(iter(self.candidates)))
                            self.candidates[key] = (candidate, time.monotonic()+600)
                            return candidate
                        if selected_id is None:
                            label = song_button(message, query=query)
                            if label:
                                selected_id, selected_label = message.id, label
                                callback = asyncio.create_task(select_song(self.client, message, label))
                    await asyncio.sleep(.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.cooldown_until = time.monotonic()+30
            LOG.info('Music source @%s unavailable (%s)', self.username, type(exc).__name__)
            raise BotSourceUnavailable('Music bot did not return a matching track in time') from None
        finally:
            if callback is not None:
                if not callback.done():
                    callback.cancel()
                await asyncio.gather(callback, return_exceptions=True)
            self.lock.release()

    async def download(self, candidate):
        cached = self.cached(candidate.query)
        if cached:
            return cached
        directory = None
        try:
            async with asyncio.timeout(18):
                media = audio_media(candidate.message)
                size = int(media.file_size)
                suffix = Path(str(getattr(media, 'file_name', '') or '')).suffix.lower()
                if suffix not in {'.mp3', '.m4a', '.ogg', '.opus', '.aac', '.flac', '.wav'}:
                    suffix = '.audio'
                directory = Path(tempfile.mkdtemp(prefix='mudyar_media_'))
                path = directory / ('telegram' + suffix)
                count = 0
                with path.open('wb') as output:
                    async for chunk in self.client.stream_media(candidate.message, limit=21):
                        count += len(chunk)
                        if count > size or count > MAX_BYTES:
                            raise BotSourceUnavailable('Audio size changed')
                        output.write(chunk)
                if count != size:
                    raise BotSourceUnavailable('Audio was incomplete')
                duration = candidate.duration
                if duration is None:
                    process = await asyncio.create_subprocess_exec('ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(path),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
                    try:
                        out, _ = await asyncio.wait_for(process.communicate(), timeout=4)
                        duration = float(out.strip())
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    if not 0 < duration <= 600:
                        raise BotSourceUnavailable('Audio exceeds the duration limit')
                save_cached_song(self.username.casefold()+':'+candidate.query,False,str(path),candidate.label,duration)
                LOG.info('Audio source @%s ready (%d bytes)', self.username, count)
                directory = None
                return str(path), candidate.label, duration
        finally:
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)

    async def resolve(self, query):
        cached = self.cached(query)
        if cached:
            return cached
        return await self.download(await self.find(query))


async def resolve_with_fallback(prepared, query, source, *, primary_timeout=12):
    """Use a cached fallback immediately; otherwise bound primary lookup time."""
    if hasattr(source, "race") and type(source).__name__ == "ParallelSources":
        return await source.race(prepared, query)
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
    """Opt-in live checks of all configured bots; never joins a group VC."""
    if not 0 < enabled_until - time.time() <= 900:
        return
    async def probe(provider):
        path = None
        started = time.monotonic()
        provider.trace = True
        try:
            from bot_source_probe import decode_sample
            import hashlib
            path, title, duration = await provider.resolve('Purple Wink pGb')
            data = Path(path).read_bytes()
            result = await decode_sample(data, seconds=15)
            LOG.info('Music source live probe @%s: seconds=%.2f title=%s duration=%s bytes=%s pcm_bytes=%s peak=%s exit=%s hash=%s',
                     provider.username,time.monotonic()-started,title,duration,len(data),result['pcm_bytes'],result['peak'],result['exit_code'],hashlib.sha256(data).hexdigest()[:12])
        except Exception as exc:
            LOG.warning('Music source live probe @%s failed (%s)',provider.username,type(exc).__name__)
        finally:
            provider.trace = False
            if path:
                shutil.rmtree(Path(path).parent,ignore_errors=True)
    await asyncio.gather(*(probe(provider) for provider in getattr(source,'sources',[source])))


async def run_candidate_probes(client, active, enabled_until):
    """Compare adjacent queries sequentially; candidates stay out of normal races."""
    if not 0 < enabled_until - time.time() <= 900:
        return
    baseline = next((s for s in getattr(active, 'sources', []) if s.username == 'VoiceShazamBot'), None)
    if baseline is not None:
        await run_live_probe(baseline, enabled_until)
    for name in ('VKMusicalRobot', 'finder_muzbot', 'MusicToolsRobot', 'vkm_bot'):
        await run_live_probe(LyBotSource(client, name), enabled_until)
