"""Bounded source-race regression checks for caching, cancellation and fallback."""
import asyncio
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock
from source_race import ParallelSources

class SourceRaceEdges(unittest.IsolatedAsyncioTestCase):
    def source(self, label):
        source=Obj(cached=Mock(return_value=None))
        source.find=AsyncMock(return_value=Obj(source=source,label=label,duration=160))
        source.download=AsyncMock(return_value=(label+'.mp3',label,160))
        return source

    async def test_failed_first_transfer_uses_second_without_extra_download(self):
        first,second=self.source('first'),self.source('second')
        first.download.side_effect=RuntimeError('temporary transfer failed')
        async def wait_for_first(_):
            while first.download.await_count == 0:
                await asyncio.sleep(0)
            return second.candidate
        second.candidate=Obj(source=second,label='second',duration=160)
        second.find.side_effect=wait_for_first
        result=await asyncio.wait_for(ParallelSources([first,second],Mock()).race(None,'music'),2)
        self.assertEqual(result[0],'second.mp3')
        self.assertEqual(first.download.await_count,1)
        self.assertEqual(second.download.await_count,1)

    async def test_fast_primary_cancels_never_ending_searches(self):
        event=asyncio.Event()
        started=asyncio.Event()
        source=self.source('bot')
        async def slow(_):
            started.set()
            try: await event.wait()
            finally: event.set()
        source.find.side_effect=slow
        async def primary():
            await started.wait()
            return ('stream.mp3','Primary',160)
        prepared=Obj(consume=primary,discard=Mock())
        result=await asyncio.wait_for(ParallelSources([source],Mock()).race(prepared,'music'),2)
        self.assertEqual(result[0],'stream.mp3')
        self.assertTrue(event.is_set())
        source.download.assert_not_awaited()

    async def test_cache_does_not_start_primary_extra_or_metadata(self):
        source=self.source('bot')
        source.cached.return_value=('cached.mp3','Cached',160)
        prepared=Obj(consume=AsyncMock(),discard=Mock())
        extra=AsyncMock()
        result=await ParallelSources([source],Mock()).race(prepared,'music',extra=extra)
        self.assertEqual(result[0],'cached.mp3')
        prepared.discard.assert_called_once()
        source.find.assert_not_awaited()
        extra.assert_not_awaited()

    async def test_empty_primary_result_does_not_beat_ready_bot_audio(self):
        source=self.source('bot')
        prepared=Obj(consume=AsyncMock(return_value=None),discard=Mock())
        result=await asyncio.wait_for(ParallelSources([source],Mock()).race(prepared,'music'),2)
        self.assertEqual(result[0],'bot.mp3')

    async def test_empty_metadata_result_is_skipped(self):
        first,second=self.source('empty'),self.source('valid')
        first.find.return_value=None
        result=await asyncio.wait_for(ParallelSources([first,second],Mock()).race(None,'music'),2)
        self.assertEqual(result[0],'valid.mp3')

    async def test_provider_cooldown_skips_only_unhealthy_source(self):
        from provider_health import ProviderHealth
        health=ProviderHealth(threshold=1,cooldown_seconds=45)
        bad,good=self.source('bad'),self.source('good')
        health.failure(id(bad))
        result=await ParallelSources([bad,good],Mock(),health=health).race(None,'music')
        self.assertEqual(result[0],'good.mp3')
        bad.find.assert_not_awaited()

    async def test_provider_lookup_failure_records_health(self):
        from provider_health import ProviderHealth
        health=ProviderHealth(threshold=1,cooldown_seconds=45)
        bad,good=self.source('bad'),self.source('good')
        bad.find.side_effect=RuntimeError('temporarily unavailable')
        result=await ParallelSources([bad,good],Mock(),health=health).race(None,'music')
        self.assertEqual(result[0],'good.mp3')
        self.assertFalse(health.available(id(bad)))
        self.assertTrue(health.available(id(good)))
