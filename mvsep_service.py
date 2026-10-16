"""Bounded, free-only MVSEP client. No local AI and no playback mutations."""
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import ClientSession, ClientTimeout, FormData

BASE = 'https://mvsep.com'
OUTPUT_HOSTS = {'mvsep.com', 'www.mvsep.com', 'de.mvsep.com',
                'de2.mvsep.com', 'sg.mvsep.com', 'hk.mvsep.com'}
MAX_INPUT = 20 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024


class KaraokeUnavailable(RuntimeError):
    pass


class KaraokeQuota(KaraokeUnavailable):
    pass


class KaraokeAuth(KaraokeUnavailable):
    pass


def output_url(value):
    try:
        p = urlsplit(value)
        valid = (p.scheme == 'https' and p.hostname in OUTPUT_HOSTS
                 and not p.username and not p.password and p.port in (None, 443)
                 and p.path.startswith('/storage/') and not p.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise KaraokeUnavailable('Untrusted output URL')
    return value


async def bounded_json(response):
    if response.status in (401, 403):
        raise KaraokeAuth('MVSEP authentication unavailable')
    if response.status in (402, 429):
        raise KaraokeQuota('MVSEP free quota unavailable')
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise KaraokeUnavailable('API response too large')
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise KaraokeUnavailable('Invalid API response') from None
    if response.status != 200 or not isinstance(data, dict) or data.get('success') is not True:
        # Classify, never print upstream text (may include account/token/URLs).
        message = str(data.get('message', '')) + ' ' + str(data.get('data', '')) if isinstance(data, dict) else ''
        text = message.casefold()
        if any(word in text for word in ('api token','api_token','api key','unauthorized','authentication')):
            raise KaraokeAuth('MVSEP token rejected')
        if any(word in text for word in ('quota','limit','premium','credit','captcha','verify','verification')):
            raise KaraokeQuota('MVSEP free API restricted')
        raise KaraokeUnavailable('MVSEP request rejected HTTP ' + str(response.status))
    return data


async def save_bounded(response, destination, limit=MAX_OUTPUT):
    size = 0
    try:
        with open(destination, 'wb') as out:
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > limit:
                    raise KaraokeUnavailable('Output too large')
                out.write(chunk)
        if not size:
            raise KaraokeUnavailable('Empty output')
    except BaseException:
        Path(destination).unlink(missing_ok=True)
        raise


def instrumental_file(files):
    if not isinstance(files, list):
        raise KaraokeUnavailable('No output files')
    for item in files:
        if not isinstance(item, dict):
            continue
        label = ' '.join(str(item.get(k, '')) for k in ('type', 'name', 'download', 'url')).casefold()
        if re.search(r'\b(?:instrumental|instrum|no[_ -]?vocals)\b', label.replace('_', ' ')):
            return output_url(item.get('url', ''))
    raise KaraokeUnavailable('No instrumental stem')


class MVSEPSeparator:
    name = 'mvsep'

    def __init__(self, token):
        if not token:
            raise ValueError('Missing MVSEP token')
        self.token = token

    async def check_free(self, session):
        async with session.get(BASE + '/api/app/user', params={'api_token': self.token},
                               allow_redirects=False) as response:
            row = await bounded_json(response)
        user = row.get('data', {})
        # Refuse paid mode rather than changing the owner's account preferences.
        if not isinstance(user, dict) or str(user.get('premium_enabled')) != '0':
            raise KaraokeQuota('MVSEP must have premium spending disabled')
        if user.get('current_queue'):
            raise KaraokeQuota('MVSEP already has an active separation')
        return True

    async def separate(self, path, destination, progress=None):
        path = Path(path)
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_INPUT:
            raise KaraokeUnavailable('Input too large')
        job = None
        completed = False
        timeout = ClientTimeout(total=40, connect=10, sock_read=30)
        async with ClientSession(timeout=timeout) as session:
            if progress:await progress('checking_free')
            await self.check_free(session)
            if progress:await progress('submitting')
            try:
                form = FormData()
                for key, value in {'api_token': self.token, 'sep_type': '23',
                                   'add_opt1': '8', 'output_format': '0', 'is_demo': '0'}.items():
                    form.add_field(key, value)
                with path.open('rb') as source:
                    form.add_field('audiofile', source, filename='mudyar-karaoke' + path.suffix,
                                   content_type='application/octet-stream')
                    async with session.post(BASE + '/api/separation/create', data=form,
                                            allow_redirects=False) as response:
                        row = await bounded_json(response)
                job = row.get('data', {}).get('hash')
                if not isinstance(job, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{8,240}', job):
                    job = None
                    raise KaraokeUnavailable('Invalid job reference')
                async with asyncio.timeout(240):
                    last_status = None
                    while True:
                        async with session.get(BASE + '/api/separation/get',
                                               params={'hash': job, 'mirror': '0'},
                                               allow_redirects=False) as response:
                            row = await bounded_json(response)
                        status = row.get('status')
                        if progress and status != last_status:
                            await progress(status)
                        last_status = status
                        if status == 'done':
                            url = instrumental_file(row.get('data', {}).get('files'))
                            completed = True
                            async with session.get(url, allow_redirects=False) as response:
                                if response.status != 200:
                                    raise KaraokeUnavailable('Output unavailable')
                                await save_bounded(response, destination)
                            return str(destination)
                        if status not in ('waiting', 'processing', 'distributing', 'merging'):
                            raise KaraokeUnavailable('Separation failed')
                        await asyncio.sleep(5)
            finally:
                if job:
                    # Only our own job; best effort cancellation/deletion, no retries.
                    try:
                        async with asyncio.timeout(8):
                            if not completed:
                                async with session.post(BASE + '/api/separation/cancel',
                                    data={'api_token': self.token, 'hash': job}, allow_redirects=False) as response:
                                    await response.read()
                            async with session.post(BASE + '/api/separation/delete',
                                data={'api_token': self.token, 'hash': job}, allow_redirects=False) as response:
                                await response.read()
                    except (Exception, asyncio.CancelledError):
                        pass


def configured_provider():
    if os.getenv('MUSIC_KARAOKE_ENABLED') != '1':
        return None
    token = os.getenv('MVSEP_API_TOKEN', '').strip()
    return MVSEPSeparator(token) if token else None
