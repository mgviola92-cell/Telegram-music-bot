import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from youtube_metadata import MetadataHTTP
import youtube_relay as relay


class MetadataTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http=MetadataHTTP()
        self.transports=[]
        async def handler(request):
            self.transports.append(request.transport)
            await request.json()
            return web.json_response({'ok':True})
        app=web.Application();app.router.add_post('/search',handler)
        app.router.add_post('/player',handler)
        self.runner=web.AppRunner(app);await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        self.root=patch('youtube_metadata.ROOT',f'http://127.0.0.1:{port}/');self.root.start()

    async def asyncTearDown(self):
        await self.http.close();self.root.stop();await self.runner.cleanup()

    async def test_real_requests_reuse_connection_and_close_session(self):
        self.assertEqual(await self.http.request('search',{'query':'a'}),{'ok':True})
        self.assertEqual(await self.http.request('player',{'videoId':'x'}),{'ok':True})
        self.assertIs(self.transports[0],self.transports[1])
        session=self.http.session
        await self.http.close()
        self.assertTrue(session.closed)

    async def test_request_cannot_select_arbitrary_host_or_path(self):
        with self.assertRaises(ValueError):await self.http.request('https://evil.test',{})


class MetadataSharingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        relay._CACHE.clear();relay._ALIASES.clear();relay._DIRECT_BACKOFF=0

    async def test_concurrent_normalized_names_share_one_request(self):
        track=relay.RelayTrack(relay.relay_source('nfs8NYg7yQM'),'Attention',231)
        async def search(_):
            await asyncio.sleep(.02)
            return track
        with patch('youtube_relay._direct_search_async',side_effect=search) as lookup:
            results=await asyncio.gather(relay.resolve_request('Attention'),relay.resolve_request('  ＡＴＴＥＮＴＩＯＮ '))
        self.assertEqual(results,[track,track]);lookup.assert_awaited_once()
        self.assertFalse(relay._INFLIGHT)

    async def test_cancel_one_waiter_keeps_other_alive(self):
        started=asyncio.Event();finish=asyncio.Event()
        track=relay.RelayTrack(relay.relay_source('nfs8NYg7yQM'),'Attention',231)
        async def search(_):
            started.set();await finish.wait();return track
        with patch('youtube_relay._direct_search_async',side_effect=search) as lookup:
            first=asyncio.create_task(relay.resolve_request('Attention'))
            await started.wait()
            second=asyncio.create_task(relay.resolve_request('Attention'))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):await first
            finish.set();self.assertEqual(await second,track)
        lookup.assert_awaited_once();self.assertFalse(relay._INFLIGHT)

    async def test_last_waiter_cancellation_cleans_request(self):
        started=asyncio.Event();cancelled=asyncio.Event()
        async def search(_):
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        with patch('youtube_relay._direct_search_async',side_effect=search):
            task=asyncio.create_task(relay.resolve_request('Attention'))
            await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            await asyncio.wait_for(cancelled.wait(),.2)
        self.assertFalse(relay._INFLIGHT)
