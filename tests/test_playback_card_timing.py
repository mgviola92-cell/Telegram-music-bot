"""Live playback regression: slow card uploads cannot delay visible playing status.

The UI must never replace an already verified native audio stream.
"""
import asyncio
import time
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock

import main
from test_music_extensions import closure


class PlayingCardTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_shows_playing_before_voice_status_and_photo(self):
        events = []
        song = main.Track('Example — Artist', 'query', youtube=True)
        player = main.Player(current=song, started_at=time.monotonic())

        async def edit_text(*args, **kwargs):
            events.append('text_playing')
            self.assertIn('Example', args[0])
            return progress

        async def voice_check(*args, **kwargs):
            events.append('voice_check')
            return False

        async def photo(*args, **kwargs):
            events.append('photo_attempt')
            raise RuntimeError('Image CDN unavailable')

        progress = N(id=123, reply_to_message_id=None, edit_text=AsyncMock(side_effect=edit_text),
                     delete=AsyncMock())
        bot = N(send_message=AsyncMock())
        ns = dict(vars(main))
        ns.update(
            players={-100: player}, bot=bot,
            calls=N(play=AsyncMock()),
            identity=N(id=5),
            owner_settings=N(data={'default_photo': None}),
            photo_cache=N(send=AsyncMock()),
            send_default_photo=AsyncMock(side_effect=photo),
            ensure_voice_audio=AsyncMock(side_effect=voice_check),
            artwork_for=Mock(return_value=None),
            elapsed_for=lambda player: 0.0,
        )
        fn = closure('announce_now_playing', ns)
        await fn(-100, player, song, progress)
        self.assertEqual(events, ['text_playing', 'voice_check', 'photo_attempt'])
        self.assertEqual(player.panel_id, 123)
        self.assertFalse(player.panel_is_photo)
        progress.edit_text.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        ns['calls'].play.assert_not_awaited()

    async def test_native_frame_poll_accepts_late_but_progressing_audio(self):
        # Simulates a provider needing ~5.5s in production, with an accelerated clock.
        counter = 0
        async def native_time(*args):
            nonlocal counter
            counter += 1
            return 1 if counter >= 24 else 0
        calls = N(time=AsyncMock(side_effect=native_time))
        await main.wait_for_audio_frames(calls, -100, timeout=.35, interval=.01)
        self.assertGreaterEqual(counter, 24)
