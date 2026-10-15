"""Opt-in, expiring Render network smoke test. Never joins or sends to a VC."""
import asyncio
import logging
import time
from music_search import collect_choices, youtube_title
from player_visuals import stream_headers_for
from stream_probe import probe_audio_url

LOG = logging.getLogger('telegram_music')


async def run_feature_probe(enabled_until, resolve_direct):
    now = time.time()
    if not now < enabled_until <= now + 900:
        return
    try:
        title = await youtube_title('https://youtu.be/60ItHLz5WEA')
        LOG.info('Music feature probe YouTube metadata: ok=%s', bool(title))
    except Exception as exc:
        LOG.info('Music feature probe YouTube metadata: unavailable (%s)', type(exc).__name__)
    choices = await collect_choices('Purple Wink pGb', timeout=12)
    LOG.info('Music feature probe metadata-only search: choices=%s', len(choices))
    if not choices:
        return
    try:
        url, _, _ = await asyncio.wait_for(asyncio.to_thread(resolve_direct, choices[0].source), timeout=25)
        result = await probe_audio_url(url, stream_headers_for(url), variant='hls_no_eof')
        LOG.info('Music feature probe direct HTTPS PCM: %s', result)
    except Exception as exc:
        LOG.info('Music feature probe direct HTTPS PCM: unavailable (%s)', type(exc).__name__)
