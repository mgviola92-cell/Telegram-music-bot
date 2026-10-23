import asyncio
import os
from collections import deque
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, Mock, patch

import main
from relay_cooldown import RelayCooldown, RelayCoolingDown, RelayPlaybackHealth, upstream_failure
from shared_audio import SharedAudio
from youtube_relay import RelayAudioStream, relay_source


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.guard = RelayCooldown(clock=lambda: self.now)

    def fail(self):
        return self.guard.failure(RuntimeError('ffmpeg: Server returned 503 Service Unavailable'))

    def test_one_failure_keeps_youtube_available(self):
        self.assertTrue(self.fail())
        self.assertTrue(self.guard.available())

    def test_three_recent_failures_pause_and_automatically_restore(self):
        for _ in range(3): self.fail()
        self.assertFalse(self.guard.available())
        with self.assertRaises(RelayCoolingDown): self.guard.require_available()
        self.now += 30
        self.assertTrue(self.guard.available())
        self.assertFalse(self.guard.failures)

    def test_old_failures_do_not_accumulate_forever(self):
        self.fail(); self.fail(); self.now += 61; self.fail()
        self.assertTrue(self.guard.available())

    def test_success_resets_streak(self):
        self.fail(); self.fail(); self.guard.success(); self.fail()
        self.assertTrue(self.guard.available())
        self.assertEqual(len(self.guard.failures), 1)

    def test_ignored_failures_do_not_disable_youtube(self):
        for exc in [TimeoutError(), RuntimeError('No matching track'),
                    RuntimeError('Server returned 404 Not Found'),
                    RuntimeError('HTTP Error 403 Forbidden'),
                    main.AudioFramesStalled('ffmpeg server returned 503'),
                    main.AudioTransportUnavailable('connection timed out'),
                    RelayCoolingDown('cooldown')]:
            for _ in range(4): self.assertFalse(self.guard.failure(exc))
        self.assertTrue(self.guard.available())
        self.assertFalse(self.guard.failures)

    def test_network_failure_requires_input_context(self):
        self.assertFalse(upstream_failure(RuntimeError('Telegram connection timed out')))
        self.assertTrue(upstream_failure(RuntimeError('ffmpeg: connection timed out')))
        self.assertFalse(upstream_failure(RuntimeError('HTTP error 502')))
        self.assertTrue(upstream_failure(RuntimeError('ffmpeg HTTP error 502')))

    def test_late_results_cannot_extend_or_clear_open_window(self):
        for _ in range(3): self.fail()
        until = self.guard.until
        self.now += 5
        self.fail(); self.guard.success()
        self.assertEqual(self.guard.until, until)

    def test_failure_memory_is_bounded(self):
        for _ in range(100): self.fail()
        self.assertEqual(len(self.guard.failures), 3)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_relay_cannot_share_normal_inflight_resolution(self):
        reached = asyncio.Event()
        release = asyncio.Event()
        seen = []
        async def resolver(track, prefer):
            seen.append(track.skip_relay)
            if not track.skip_relay:
                reached.set()
                await release.wait()
            return ('https://example.com/fallback' if track.skip_relay else
                    relay_source('nfs8NYg7yQM'), 'Title', 30)
        shared = SharedAudio(resolver, Mock())
        normal = N(source='Attention', video=False, skip_relay=False)
        fallback = N(source='Attention', video=False, skip_relay=True)
        with patch('shared_audio.get_cached_song', return_value=None):
            task = asyncio.create_task(shared.resolve(normal, True))
            await reached.wait()
            result = await shared.resolve(fallback, True)
            release.set()
            await task
        self.assertEqual(result[0], 'https://example.com/fallback')
        self.assertEqual(seen, [False, True])

    async def test_relay_http_failure_falls_through_without_vc_rejoin(self):
        calls = N(play=AsyncMock(side_effect=RuntimeError('ffmpeg HTTP error 503')),
                  leave_call=AsyncMock())
        with self.assertRaises(RuntimeError):
            await main.play_with_reconnect(calls, -100, RelayAudioStream(relay_source('nfs8NYg7yQM')))
        calls.play.assert_awaited_once()
        calls.leave_call.assert_not_awaited()

    async def test_named_resolution_skips_relay_during_cooldown(self):
        from test_music_extensions import closure
        race = N(race=AsyncMock(return_value=('fallback.mp3', 'Fallback', 30)))
        guard = N(available=Mock(return_value=False))
        namespace = dict(vars(main), lybot=race, relay_cooldown=guard,
                         stream_only_enabled=lambda: False,
                         youtube_relay_enabled=lambda: True,
                         PreparedAudio=Mock(), resolve_youtube_relay=AsyncMock())
        resolve = closure('resolve_for_queue', namespace)
        await resolve(main.Track('Attention', 'Attention', youtube=True), True)
        self.assertIsNone(race.race.call_args.kwargs['extra'])
        namespace['resolve_youtube_relay'].assert_not_awaited()

    async def test_queue_clear_cancels_async_resolver_without_late_metadata(self):
        from prefetch import AsyncPreparedAudio
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def resolver(track, prefer):
            started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        track = main.Track('Pending', 'query', youtube=True)
        slot = AsyncPreparedAudio(track, resolver, Mock())
        notify = slot.on_metadata = Mock()
        player = main.Player(current=main.Track('Playing', 'now'),
                             queue=deque([track]), prepared=slot)
        await started.wait()
        player.queue.clear()
        main.prepare_next_song(player)
        await asyncio.gather(slot.task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(player.prepared)
        notify.assert_not_called()


class NativePlaybackBackoffTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.health = RelayPlaybackHealth(clock=lambda: self.now, threshold=2,
                                          window=120, cooldown=60)

    def test_first_stall_does_not_disable_provider(self):
        self.assertTrue(self.health.failure(main.AudioFramesStalled("no clock")))
        self.assertTrue(self.health.available())

    def test_two_real_native_stalls_temporarily_skip_provider(self):
        self.assertTrue(self.health.failure(main.AudioFramesStalled("no clock")))
        self.assertTrue(self.health.failure(main.AudioStartupTimeout("native timeout")))
        self.assertFalse(self.health.available())
        self.now += 59
        self.assertFalse(self.health.available())
        self.now += 1
        self.assertTrue(self.health.available())
        self.assertFalse(self.health.failures)

    def test_unrelated_telegram_audio_transport_or_song_restriction_ignored(self):
        for bad in [main.AudioTransportUnavailable("muted"), TimeoutError("network"),
                    RuntimeError("DRM protected"), RuntimeError("No matching track")]:
            self.assertFalse(self.health.failure(bad))
        self.assertTrue(self.health.available())

    def test_success_resets_streak_before_window_closed(self):
        self.health.failure(main.AudioFramesStalled("stalled"))
        self.health.success()
        self.health.failure(main.AudioFramesStalled("stalled"))
        self.assertTrue(self.health.available())

    def test_old_stall_does_not_accumulate(self):
        self.health.failure(main.AudioFramesStalled("stalled"))
        self.now += 121
        self.health.failure(main.AudioFramesStalled("stalled"))
        self.assertTrue(self.health.available())

    def test_no_audio_source_counts_as_nonplayable(self):
        err = type("NoAudioSourceFound", (Exception,), {})("bad input")
        self.health.failure(err); self.health.failure(err)
        self.assertFalse(self.health.available())


class DirectLinkHealthRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_youtube_link_uses_nubcoders_during_vapters_stall_cooldown(self):
        from test_music_extensions import closure
        guard = N(available=Mock(return_value=False))
        link = "https://youtu.be/jNQXAC9IVRw"
        signed = "https://api.nubcoders.com/stream/resolver/fixture"
        named = main.Track("Sample", link, youtube=True, relay_requested=True)
        ns = dict(vars(main))
        ns.update(relay_playback_health=guard,
                  relay_cooldown=N(available=Mock(return_value=True)),
                  youtube_relay_enabled=lambda: True,
                  resolve_youtube_relay=AsyncMock(),
                  resolve_nubcoders_audio=AsyncMock(return_value=signed),
                  verify_nubcoders_audio=AsyncMock(return_value=True))
        handler = closure("resolve_for_queue", ns)
        with patch.dict(os.environ, {"NUBCODERS_ENABLED": "1", "NUBCODERS_API_TOKEN": "fixture"}):
            result = await handler(named, True)
        self.assertEqual(result[0], signed)
        ns["resolve_youtube_relay"].assert_not_awaited()
        ns["resolve_nubcoders_audio"].assert_awaited_once()

    async def test_named_youtube_candidate_skipped_during_native_stalls(self):
        from test_music_extensions import closure
        race = N(race=AsyncMock(return_value=("fallback.mp3", "Fallback", 30)))
        ns = dict(vars(main))
        ns.update(lybot=race, relay_cooldown=N(available=Mock(return_value=True)),
                  relay_playback_health=N(available=Mock(return_value=False)),
                  stream_only_enabled=lambda: False,
                  youtube_relay_enabled=lambda: True,
                  PreparedAudio=Mock(), resolve_youtube_relay=AsyncMock())
        handler = closure("resolve_for_queue", ns)
        with patch.dict(os.environ, {"NUBCODERS_API_TOKEN": "", "NUBCODERS_ENABLED": "0"}):
            await handler(main.Track("Attention", "Attention", youtube=True), True)
        self.assertIsNone(race.race.call_args.kwargs["extra"])
        ns["resolve_youtube_relay"].assert_not_awaited()
