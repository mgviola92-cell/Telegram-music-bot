"""Exercise the real nested queue loader without starting Telegram clients."""
import ast
import asyncio
from collections import deque
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock

import main


class DirectPlaybackFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def check_fallback(self, *, constructor_failure, stalled=False, strict=False):
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
        if not constructor_failure and not stalled:
            playback.side_effect = [RuntimeError('remote playback failed'), None]
        frame_check = AsyncMock(side_effect=[main.AudioFramesStalled("stalled"), None] if stalled else None)
        announce = AsyncMock()
        namespace = dict(vars(main))
        namespace.update(players={123: player}, download_song=loader, lybot=None,
                         fresh_voice_chats={123},
                         MediaStream=factory, calls=MagicMock(),
                         audio_stream=lambda src: factory(src, audio_flags=main.MediaStream.Flags.REQUIRED),
                         play_with_reconnect=playback, stream_only_enabled=lambda: strict,
                         wait_for_audio_frames=frame_check,
                         prepare_next_song=MagicMock(), clean_temp=MagicMock(),
                         announce_now_playing=announce,
                         start_idle=AsyncMock(return_value=False),
                         clear_player_panel=AsyncMock(), bot=MagicMock())
        exec(compile(ast.Module(body=[advance], type_ignores=[]), main.__file__, 'exec'), namespace)
        await namespace['advance'](123)
        if player.announce_task:
            await player.announce_task
        if strict:
            loader.assert_called_once()
            self.assertIsNone(player.current)
            return
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

    async def test_accepted_but_stalled_direct_audio_uses_local_fallback(self):
        await self.check_fallback(constructor_failure=False, stalled=True)

    async def test_stream_only_stall_does_not_download_local_fallback(self):
        await self.check_fallback(constructor_failure=False, stalled=True, strict=True)

    async def test_named_source_failure_uses_lybot_file_and_checks_vc_frames(self):
        source_tree=ast.parse(Path(main.__file__).read_text())
        outer=next(n for n in source_tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        advance=next(n for n in outer.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='advance')
        song=main.Track('Example','Example',youtube=True)
        player=main.Player(queue=deque([song]),stream_enabled=True)
        lybot=MagicMock()
        lybot.cached.return_value=None
        lybot.resolve=AsyncMock(return_value=('lybot.mp3','Example · LyBot',120))
        namespace=dict(vars(main))
        playback=AsyncMock();frames=AsyncMock()
        namespace.update(players={123:player},fresh_voice_chats={123},lybot=lybot,download_song=MagicMock(side_effect=RuntimeError('no primary audio')),
                         calls=MagicMock(),audio_stream=MagicMock(return_value=object()),
                         play_with_reconnect=playback,wait_for_audio_frames=frames,
                         prepare_next_song=MagicMock(),clean_temp=MagicMock(),
                         announce_now_playing=AsyncMock(),stream_only_enabled=lambda:False)
        exec(compile(ast.Module(body=[advance],type_ignores=[]),main.__file__,'exec'),namespace)
        await namespace['advance'](123)
        await player.announce_task
        self.assertIs(player.current,song)
        self.assertEqual(player.temp_file,'lybot.mp3')
        lybot.resolve.assert_awaited_once_with('Example')
        frames.assert_awaited_once()
        playback.assert_awaited_once()
