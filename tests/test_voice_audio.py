"""Audio must exist, and local capture must be enabled without overriding admin mute."""
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from main import ensure_voice_audio, play_with_reconnect
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
