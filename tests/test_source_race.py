import asyncio
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock,Mock
from source_race import ParallelSources
from lybot_source import BotSourceUnavailable

class SourceRaceTest(unittest.IsolatedAsyncioTestCase):
    def source(self):
        source=Obj(cached=Mock(return_value=None))
        source.candidate=Obj(source=source,label='Purple – Wink & pGb',duration=163)
        source.find=AsyncMock(return_value=source.candidate)
        source.download=AsyncMock(return_value=('local.mp3','Purple · Bot',163))
        return source

    async def test_searches_all_sources_and_does_not_wait_for_slowest(self):
        started=[];cancelled=[]
        sources=[self.source() for _ in range(3)]
        async def slow(index):
            started.append(index)
            try:await asyncio.Event().wait()
            finally:cancelled.append(index)
        for i,source in enumerate(sources):
            async def lookup(q,i=i):return await slow(i)
            source.find=AsyncMock(side_effect=lookup)
        async def primary():
            while len(started)<3:await asyncio.sleep(0)
            return 'https://cdn.example/song','Purple · SoundCloud',163
        prepared=Obj(consume=primary,discard=Mock())
        race=ParallelSources(sources,Mock())
        result=await asyncio.wait_for(race.race(prepared,'Purple'),timeout=1)
        self.assertEqual(result[0],'https://cdn.example/song')
        self.assertEqual(len(cancelled),3)
        for source in sources:source.download.assert_not_called()

    async def test_downloads_only_one_telegram_file(self):
        sources=[self.source() for _ in range(3)]
        calls=0
        async def transfer(candidate):
            nonlocal calls
            calls+=1
            await asyncio.sleep(.01)
            return 'winner.mp3','Purple · Bot',163
        for source in sources:source.download=AsyncMock(side_effect=transfer)
        race=ParallelSources(sources,Mock())
        result=await race.race(None,'Purple')
        self.assertEqual(result[0],'winner.mp3')
        self.assertEqual(calls,1)

    async def test_failed_transfer_tries_another_ready_candidate(self):
        sources=[self.source(),self.source()]
        async def transfer(candidate):
            if not getattr(self,'failed',False):
                self.failed=True
                raise RuntimeError('temporary transfer error')
            return 'winner.mp3','Purple · Bot',163
        for source in sources:source.download=AsyncMock(side_effect=transfer)
        result=await ParallelSources(sources,Mock()).race(None,'Purple')
        self.assertEqual(result[0],'winner.mp3')

    async def test_primary_stream_cancels_slow_telegram_transfer(self):
        source=self.source();transfer_started=asyncio.Event();cancelled=asyncio.Event()
        async def transfer(candidate):
            transfer_started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        source.download=AsyncMock(side_effect=transfer)
        async def primary():
            await transfer_started.wait()
            return 'https://cdn.example/song','Purple · SoundCloud',163
        prepared=Obj(consume=primary,discard=Mock())
        result=await ParallelSources([source],Mock()).race(prepared,'Purple')
        self.assertTrue(cancelled.is_set())
        self.assertTrue(result[0].startswith('https://'))

    async def test_queue_description_never_downloads_audio(self):
        source=self.source()
        race=ParallelSources([source],Mock())
        result=await race.describe('Purple',AsyncMock(return_value=None))
        self.assertEqual(result[0],'Purple – Wink & pGb')
        source.download.assert_not_called()

    async def test_cached_audio_skips_searches_and_discards_unused_primary(self):
        source=self.source();cleanup=Mock()
        # Cached winner does not start background searches or consume a worker.
        source.cached.return_value=('cache.mp3','Purple',163)
        prepared=Obj(consume=AsyncMock(),discard=Mock())
        result=await ParallelSources([source],cleanup).race(prepared,'Purple')
        self.assertEqual(result[0],'cache.mp3')
        prepared.discard.assert_called_once()
        source.find.assert_not_called()
