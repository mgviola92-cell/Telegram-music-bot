"""Explicitly enabled, bounded Telegram source diagnostic; never joins a VC."""
import asyncio
import json
import logging
import time
from array import array
from stream_probe import safe_error

log = logging.getLogger('telegram_music')
MAX_BYTES = 8 * 1024 * 1024


def audio_media(message):
    audio = getattr(message, 'audio', None)
    if audio:
        return audio
    doc = getattr(message, 'document', None)
    if doc and str(getattr(doc, 'mime_type', '')).startswith('audio/'):
        return doc
    return None


async def decode_sample(data):
    """Check a half second of PCM from bounded Telegram bytes, only in memory."""
    process = await asyncio.create_subprocess_exec(
        'ffmpeg', '-v', 'error', '-i', 'pipe:0', '-t', '0.5',
        '-vn', '-f', 's16le', '-ar', '48000', '-ac', '2', 'pipe:1',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(data), timeout=15)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    samples = array('h')
    samples.frombytes(out[:len(out) // 2 * 2])
    return {'pcm_bytes': len(out), 'peak': max((abs(x) for x in samples), default=0),
            'exit_code': process.returncode, 'stderr': safe_error(err.decode(errors='replace'))}


async def run_bot_source_probe(client, *, enabled_until, query='Chan Chan Buddha Puzaniya', timeout=65):
    # Expiry also bounds accidental re-runs following a service restart.
    if not 0 < enabled_until - time.time() <= 900:
        return
    started = time.monotonic()
    try:
        peer = await asyncio.wait_for(client.get_users('LyBot'), timeout=15)
        if not peer.is_bot or str(peer.username).lower() != 'lybot':
            raise ValueError('Exact LyBot bot identity not verified')
        log.info('Bot source probe identity verified @LyBot bot=True')
        await asyncio.wait_for(client.send_message(peer.id, '/start'), timeout=15)
        await asyncio.sleep(2)
        sent = await asyncio.wait_for(client.send_message(peer.id, query, disable_web_page_preview=True), timeout=15)
        log.info('Bot source probe query sent @LyBot')
        seen = set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async def read():
                return [m async for m in client.get_chat_history(peer.id, limit=12) if m.id > sent.id]
            messages = await asyncio.wait_for(read(), timeout=12)
            for message in reversed(messages):
                if getattr(message, 'outgoing', False):
                    continue
                media = audio_media(message)
                text = str(getattr(message, 'text', None) or getattr(message, 'caption', None) or '')
                buttons = [[str(b.text)[:90] for b in row] for row in getattr(getattr(message, 'reply_markup', None), 'inline_keyboard', []) or []]
                state = (message.id, text, bool(media), str(buttons))
                if state not in seen:
                    seen.add(state)
                    log.info('Bot source probe response %s', json.dumps({'audio': bool(media), 'text': safe_error(text)[:700], 'buttons': buttons}, ensure_ascii=False))
                if not media:
                    continue
                size = int(getattr(media, 'file_size', 0) or 0)
                log.info('Bot source probe audio received %s', json.dumps({'title': str(getattr(media, 'title', '') or getattr(media, 'file_name', ''))[:120], 'size': size, 'duration': getattr(media, 'duration', None), 'seconds': round(time.monotonic()-started, 2)}, ensure_ascii=False))
                data = bytearray()
                async def collect():
                    async for chunk in client.stream_media(message, limit=8):
                        data.extend(chunk[:MAX_BYTES-len(data)])
                        if len(data) >= MAX_BYTES:
                            break
                await asyncio.wait_for(collect(), timeout=40)
                result = await decode_sample(bytes(data))
                result.update({'telegram_bytes': len(data), 'complete_file': bool(size and len(data) == size), 'seconds': round(time.monotonic()-started, 2)})
                log.info('Bot source probe decode result %s', json.dumps(result))
                return
            await asyncio.sleep(3)
        log.info('Bot source probe finished: no audio received within %ss', timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning('Bot source probe failed %s: %s', type(exc).__name__, safe_error(str(exc)))
