"""Audio must exist, and local capture must be enabled without overriding admin mute."""
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from main import ensure_voice_audio, play_with_reconnect, wait_for_audio_frames, AudioFramesStalled
from pytgcalls.types import MediaStream
from pytgcalls.exceptions import NoAudioSourceFound


class RequiredAudioTest(unittest.IsolatedAsyncioTestCase):
    async def test_required_audio_rejects_the_silent_success_path(self):
        url = 'https://playback.media-streaming.soundcloud.cloud/test/playlist.m3u8'
        module = importlib.import_module('pytgcalls.types.stream.media_stream')
        with patch.object(module, 'check_stream', side_effect=NoAudioSourceFound(url)):
            automatic = MediaStream(url, video_flags=MediaStream.Flags.IGNORE)
            await automatic.check_stream()
            self.assertIsNone(automatic.microphone)
            required = MediaStream(url, video_flags=MediaStream.Flags.IGNORE,
                                   audio_flags=MediaStream.Flags.REQUIRED)
            with self.assertRaises(NoAudioSourceFound):
                await required.check_stream()

    async def test_missing_audio_goes_to_fallback_without_rejoining_vc(self):
        calls = SimpleNamespace(play=AsyncMock(side_effect=NoAudioSourceFound('example')),
                                leave_call=AsyncMock())
        with self.assertRaises(NoAudioSourceFound):
            await play_with_reconnect(calls, -100123, object())
        calls.play.assert_awaited_once()
        calls.leave_call.assert_not_awaited()


class VoiceMuteTest(unittest.IsolatedAsyncioTestCase):
    def calls_for(self, *, blocked=False, volume=100):
        own = SimpleNamespace(user_id=123, muted=True, muted_by_admin=blocked, volume=volume)
        return SimpleNamespace(get_participants=AsyncMock(return_value=[own]),
                               unmute=AsyncMock(return_value=True), change_volume_call=AsyncMock())

    async def test_admin_muted_assistant_is_reported_without_unmuting_it(self):
        calls = self.calls_for(blocked=True)
        self.assertTrue(await ensure_voice_audio(calls, -100123, 123))
        calls.unmute.assert_not_awaited()
        calls.change_volume_call.assert_not_awaited()

    async def test_local_mute_and_zero_volume_are_restored(self):
        calls = self.calls_for(volume=0)
        self.assertFalse(await ensure_voice_audio(calls, -100123, 123))
        calls.unmute.assert_awaited_once_with(-100123)
        calls.change_volume_call.assert_awaited_once_with(-100123, 100)

    async def test_existing_volume_is_preserved(self):
        calls = self.calls_for(volume=80)
        self.assertFalse(await ensure_voice_audio(calls, -100123, 123))
        calls.unmute.assert_awaited_once()
        calls.change_volume_call.assert_not_awaited()


class AudioFrameProgressTest(unittest.IsolatedAsyncioTestCase):
    async def test_progressing_decoder_passes(self):
        calls = SimpleNamespace(time=AsyncMock(side_effect=[0, 0, 10]))
        with patch('main.asyncio.sleep', new=AsyncMock()):
            await wait_for_audio_frames(calls, -100123)
        self.assertEqual(calls.time.await_count, 3)

    async def test_media_clock_timeout_is_explicit(self):
        calls = SimpleNamespace(time=AsyncMock(return_value=0))
        with self.assertRaises(AudioFramesStalled):
            await wait_for_audio_frames(calls, -100123, timeout=.02, interval=.001)
        self.assertGreater(calls.time.await_count, 1)

    async def test_cancellation_does_not_trigger_fallback(self):
        import asyncio
        calls = SimpleNamespace(time=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await wait_for_audio_frames(calls, -100123)
