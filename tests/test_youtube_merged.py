import asyncio
import time
import unittest
from unittest.mock import patch
from youtube_relay import RelayTrack, relay_source, resolve_request
from source_race import ParallelSources

class MergedYouTube(unittest.IsolatedAsyncioTestCase):
    async def test_slow_direct_does_not_serialize_fallback(self):
        import youtube_relay as relay
        relay._CACHE.clear(); relay._ALIASES.clear(); relay._DIRECT_BACKOFF=0
        track=RelayTrack(relay_source('nfs8NYg7yQM'),'Attention · YouTube',231)
        def slow(_):
            time.sleep(1.8)
            raise TimeoutError()
        with patch('youtube_relay._direct_search',side_effect=slow), patch('youtube_relay._flat_search',return_value=track):
            start=time.monotonic()
            self.assertEqual(await resolve_request('Attention'),track)
            self.assertLess(time.monotonic()-start,1.5)

    async def test_youtube_can_win_existing_race_and_discard_primary(self):
        class Primary:
            discarded=False
            async def consume(self):
                await asyncio.sleep(10)
            def discard(self): self.discarded=True
        prepared=Primary()
        async def relay(): return relay_source('nfs8NYg7yQM'),'Attention · YouTube',231
        result=await ParallelSources([],lambda _:None).race(prepared,'Attention',extra=relay)
        self.assertIn('YouTube',result[1]);self.assertTrue(prepared.discarded)

    async def test_search_preserves_youtube_id_without_audio_download(self):
        from music_search import collect_choices
        track=RelayTrack(relay_source('nfs8NYg7yQM'),'Attention · YouTube',231)
        with patch('music_search.soundcloud_choices',return_value=[]),patch('youtube_relay.resolve_request',return_value=track):
            rows=await collect_choices('Attention',youtube=True)
        self.assertEqual(rows[0].source,'https://www.youtube.com/watch?v=nfs8NYg7yQM')
