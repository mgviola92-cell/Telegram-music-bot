import asyncio
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, patch

from ntgcalls import ConnectionMode
from main import play_with_reconnect
from voice_transport import AudioStartupTimeout
from youtube_relay import RelayAudioStream, relay_source


def calls(resume):
    return N(play=AsyncMock(), leave_call=AsyncMock(), resume=AsyncMock(side_effect=resume),
             unmute=AsyncMock(), _binding=N(
                 get_connection_mode=AsyncMock(return_value=ConnectionMode.RTC),
                 get_state=AsyncMock(return_value=N(muted=False))))


class ProviderStartup(unittest.IsolatedAsyncioTestCase):
    async def test_cold_capture_past_old_eight_second_deadline_does_not_rejoin(self):
        # A 12-second cold decoder on a 100x faster clock. It previously timed
        # out at 8s and needlessly restarted the provider and Telegram call.
        async def resume(chat):
            await asyncio.sleep(.12)
        client = calls(resume)
        real_wait_for = asyncio.wait_for
        async def scaled(awaitable, timeout):
            return await real_wait_for(awaitable, timeout / 100 if timeout in (4, 16) else timeout)
        source = RelayAudioStream(relay_source('nfs8NYg7yQM'))
        wrapped = N(original=N(original=source))
        with patch('voice_transport.asyncio.wait_for', side_effect=scaled):
            await play_with_reconnect(client, -100, wrapped)
        client.play.assert_awaited_once()
        client.resume.assert_awaited_once()
        client.leave_call.assert_not_awaited()
        client.unmute.assert_awaited_once()

    async def test_exhausted_provider_capture_returns_to_source_fallback_without_rejoin(self):
        cancelled = asyncio.Event()
        async def resume(chat):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        client = calls(resume)
        real_wait_for = asyncio.wait_for
        async def scaled(awaitable, timeout):
            return await real_wait_for(awaitable, timeout / 1000 if timeout == 16 else timeout)
        with patch('voice_transport.asyncio.wait_for', side_effect=scaled):
            with self.assertRaises(AudioStartupTimeout):
                await play_with_reconnect(client, -100, RelayAudioStream(relay_source('nfs8NYg7yQM')))
        self.assertTrue(cancelled.is_set())
        client.play.assert_awaited_once()
        client.leave_call.assert_not_awaited()
        client.unmute.assert_not_awaited()

    async def test_ordinary_audio_retains_its_existing_reconnect_recovery(self):
        attempts = 0
        async def resume(chat):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.Event().wait()
        client = calls(resume)
        real_wait_for = asyncio.wait_for
        async def scaled(awaitable, timeout):
            return await real_wait_for(awaitable, timeout / 1000 if timeout == 4 else timeout)
        with patch('voice_transport.asyncio.wait_for', side_effect=scaled):
            await play_with_reconnect(client, -100, object())
        self.assertEqual(client.play.await_count, 2)
        client.leave_call.assert_awaited_once()
        self.assertEqual(client.resume.await_count, 2)
