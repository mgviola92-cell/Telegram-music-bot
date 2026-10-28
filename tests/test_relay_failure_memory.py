import asyncio
import os
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

import main
from relay_failure_memory import SilentRelayVideoCache
from youtube_relay import relay_source
from test_music_extensions import closure


class PerVideoRelayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.memory = SilentRelayVideoCache(clock=lambda: self.now, cooldown=180, maximum=3)
        self.bad = relay_source('jNQXAC9IVRw')
        self.good = relay_source('dQw4w9WgXcQ')

    def test_stall_blocks_only_this_video_id(self):
        self.assertTrue(self.memory.failure(self.bad, main.AudioFramesStalled('silent')))
        self.assertFalse(self.memory.available(self.bad))
        self.assertTrue(self.memory.available(self.good))
        self.now += 179
        self.assertFalse(self.memory.available(self.bad))
        self.now += 1
        self.assertTrue(self.memory.available(self.bad))

    def test_transport_or_cancel_error_does_not_block_track(self):
        for err in [main.AudioTransportUnavailable('muted'), RuntimeError('network'),
                    TimeoutError('generic')]:
            self.assertFalse(self.memory.failure(self.bad, err))
        self.assertTrue(self.memory.available(self.bad))

    def test_playing_success_clears_stall_and_bounds_cache(self):
        self.memory.failure(self.bad, main.AudioFramesStalled('silent'))
        self.memory.success(self.bad)
        self.assertTrue(self.memory.available(self.bad))
        for video in ('abcdefghijk','123456789ab','G9x0YsIs9Bk','xYz123456AB'):
            self.memory.failure(relay_source(video),main.AudioFramesStalled('stalled'))
        self.assertLessEqual(len(self.memory.entries),3)

    def test_invalid_sources_do_not_enter_cache(self):
        self.assertFalse(self.memory.failure('https://example.org/song',
                                             main.AudioFramesStalled('silent')))
        self.assertEqual(self.memory.entries,{})


class RoutingAfterSilentRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_query_skips_cached_failed_video(self):
        song = main.Track('Test artist song','Test artist song',youtube=True)
        relay = N(source=relay_source('jNQXAC9IVRw'),title='Test artist song · YouTube',duration=125)
        fallback_source = N(race=AsyncMock(return_value=('backup.mp3','Backup',125)))
        memo = N(available=Mock(return_value=False))
        ns = dict(vars(main))
        ns.update(lybot=fallback_source, silent_relay_videos=memo,
                  resolve_youtube_relay=AsyncMock(return_value=relay),
                  youtube_relay_enabled=lambda: True,
                  relay_cooldown=N(available=Mock(return_value=True)),
                  relay_playback_health=N(available=Mock(return_value=True)),
                  PreparedAudio=Mock())
        resolve = closure('resolve_for_queue',ns)
        with patch.dict(os.environ,{'NUBCODERS_API_TOKEN':'','NUBCODERS_ENABLED':'0'}):
            await resolve(song,True)
        extra=fallback_source.race.call_args.kwargs['extra']
        self.assertIsNotNone(extra)
        with self.assertRaises(LookupError):
            await extra()
        memo.available.assert_called_once_with(relay.source)

    async def test_direct_link_keeps_nubcoders_when_youtube_video_silent(self):
        song = main.Track('Test','https://youtu.be/jNQXAC9IVRw',youtube=True,relay_requested=True)
        relay=N(source=relay_source('jNQXAC9IVRw'),title='Test · YouTube',duration=90)
        signed='https://api.nubcoders.com/stream/resolver/fixture'
        ns=dict(vars(main))
        ns.update(silent_relay_videos=N(available=Mock(return_value=False)),
                  resolve_youtube_relay=AsyncMock(return_value=relay),
                  resolve_nubcoders_audio=AsyncMock(return_value=signed),
                  verify_nubcoders_audio=AsyncMock(return_value=True),
                  relay_cooldown=N(available=Mock(return_value=True)),
                  relay_playback_health=N(available=Mock(return_value=True)),
                  youtube_relay_enabled=lambda: True)
        async def verified_remote(prepared, query, **kwargs):
            return await kwargs['extra']()
        ns.update(lybot=N(race=AsyncMock(side_effect=verified_remote)),
                  youtube_title=AsyncMock(return_value='Test Song'),
                  PreparedAudio=Mock())
        with patch.dict(os.environ,{'NUBCODERS_API_TOKEN':'fixture','NUBCODERS_ENABLED':'1'}):
            result=await closure('resolve_for_queue',ns)(song,True)
        self.assertEqual(result[0],signed)
