"""Exercise the real nested queue loader without starting Telegram clients."""
import ast
import asyncio
from collections import deque
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock

import main


class DirectPlaybackFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def check_fallback(self, *, constructor_failure):
        # Compile the actual closure body with its collaborators injected.
        # No Telegram credentials, network request, or VC session is created.
        source = ast.parse(Path(main.__file__).read_text())
        outer = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'main')
        advance = next(n for n in outer.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'advance')
        song = main.Track('Example', 'Example', youtube=True)
        player = main.Player(queue=deque([song]), stream_enabled=True)
        remote = 'https://playback.media-streaming.soundcloud.cloud/example.m3u8'
        loader = MagicMock(side_effect=[(remote, 'Example', 120), ('local.mp3', 'Example', 120)])
        factory = MagicMock()
        factory.Flags.IGNORE = main.MediaStream.Flags.IGNORE
        factory.Flags.REQUIRED = main.MediaStream.Flags.REQUIRED
        if constructor_failure:
            factory.side_effect = [ValueError('remote probe failed'), object()]
        playback = AsyncMock()
        if not constructor_failure:
            playback.side_effect = [RuntimeError('remote playback failed'), None]
        announce = AsyncMock()
        namespace = dict(vars(main))
        namespace.update(players={123: player}, download_song=loader,
                         MediaStream=factory, calls=MagicMock(),
                         play_with_reconnect=playback,
                         prepare_next_song=MagicMock(), clean_temp=MagicMock(),
                         announce_now_playing=announce,
                         start_idle=AsyncMock(return_value=False),
                         clear_player_panel=AsyncMock(), bot=MagicMock())
        exec(compile(ast.Module(body=[advance], type_ignores=[]), main.__file__, 'exec'), namespace)
        await namespace['advance'](123)
        if player.announce_task:
            await player.announce_task
        self.assertIs(player.current, song)
        self.assertEqual(player.temp_file, 'local.mp3')
        self.assertEqual(loader.call_args_list[0].args[-1], True)
        self.assertEqual(loader.call_args_list[1].args[-1], False)
        self.assertEqual(factory.call_args_list[-1].args[0], 'local.mp3')
        self.assertEqual(factory.call_args_list[-1].kwargs['audio_flags'], main.MediaStream.Flags.REQUIRED)
        self.assertEqual(playback.await_count, 1 if constructor_failure else 2)
        self.assertIsNone(player.loading_task)

    async def test_remote_constructor_failure_uses_local_audio(self):
        await self.check_fallback(constructor_failure=True)

    async def test_remote_playback_failure_uses_local_audio(self):
        await self.check_fallback(constructor_failure=False)
