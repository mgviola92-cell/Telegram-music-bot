"""Bounded reusable HTTP transport for public YouTube metadata only."""
import asyncio
import json
import logging
import time
from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, TCPConnector, TraceConfig

LOG = logging.getLogger('telegram_music')
ROOT = 'https://youtubei.googleapis.com/youtubei/v1/'


class MetadataHTTP:
    def __init__(self):
        self.session = None
        self.loop = None

    async def _session(self):
        loop = asyncio.get_running_loop()
        if self.session is not None and not self.session.closed:
            if self.loop is not loop:
                raise RuntimeError('Metadata transport belongs to another event loop')
            return self.session
        trace = TraceConfig()
        for name in ('dns_resolvehost_start', 'dns_resolvehost_end',
                     'connection_create_start', 'connection_create_end',
                     'connection_queued_start', 'connection_queued_end', 'connection_reuseconn'):
            async def record(session, context, params, event=name):
                context.trace_request_ctx[event] = time.monotonic()
            getattr(trace, 'on_' + name).append(record)
        self.loop = loop
        self.session = ClientSession(
            connector=TCPConnector(limit=2, limit_per_host=2, ttl_dns_cache=300),
            timeout=ClientTimeout(total=3, connect=2, sock_connect=1.5, sock_read=2),
            cookie_jar=DummyCookieJar(), trace_configs=[trace],
            headers={'Content-Type': 'application/json', 'X-Youtube-Client-Name': '3',
                     'User-Agent': 'com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip'})
        return self.session

    async def request(self, kind, body):
        if kind not in ('search', 'player'):
            raise ValueError('Unsupported metadata operation')
        session = await self._session()
        marks = {}
        started = time.monotonic()
        status = None
        outcome = 'cancelled'
        phase = 'connect_or_headers'
        try:
            endpoint = ROOT + kind + ('?prettyPrint=false&fields=contents' if kind == 'search' else '')
            async with session.post(endpoint, json=body, allow_redirects=False,
                                    trace_request_ctx=marks) as response:
                marks["headers"] = time.monotonic()
                status = response.status
                if status != 200:
                    raise LookupError('Metadata HTTP status ' + str(status))
                if 'json' not in response.headers.get('Content-Type', '').lower():
                    raise LookupError('Metadata response was not JSON')
                phase = 'response_body'
                payload = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    payload.extend(chunk)
                    if len(payload) > 1024 * 1024:
                        raise ValueError('Search metadata too large')
                marks['body'] = time.monotonic()
                phase = 'parse'
                result = json.loads(payload)
                outcome = 'ok'
                return result
        except Exception as exc:
            outcome = type(exc).__name__
            raise
        finally:
            def elapsed(begin, end):
                return round(marks[end] - marks[begin], 3) if begin in marks and end in marks else None
            LOG.info('YouTube metadata HTTP kind=%s outcome=%s status=%s phase=%s reused=%s dns=%s connect=%s queued=%s headers=%s body=%s total=%.3fs',
                     kind, outcome, status, phase, 'connection_reuseconn' in marks,
                     elapsed('dns_resolvehost_start', 'dns_resolvehost_end'),
                     elapsed('connection_create_start', 'connection_create_end'),
                     elapsed('connection_queued_start', 'connection_queued_end'),
                     round(marks['headers']-started, 3) if 'headers' in marks else None,
                     elapsed('headers', 'body'), time.monotonic()-started)

    async def close(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None
        self.loop = None


HTTP = MetadataHTTP()


async def close():
    await HTTP.close()
