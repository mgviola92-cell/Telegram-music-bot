import asyncio
import shlex
import subprocess
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

import main
from playback_prepare import CheckedAudioStream, warm_voice
from prefetch import AsyncPreparedAudio
from audio_options import AudioSettings
from test_music_extensions import closure


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_once_and_mixer_mutation_is_not_reused(self):
        original = N(check_stream=AsyncMock(), microphone=N(path='ffmpeg original'), camera=None)
        stream = CheckedAudioStream(original)
        await stream.check_stream()
        stream.microphone.path += ' welcome-mixer'
        await stream.check_stream()
        original.check_stream.assert_awaited_once()
        self.assertEqual(stream.microphone.path, 'ffmpeg original')

    async def test_resolved_next_track_is_probed_before_consume_and_only_once(self):
        gate = asyncio.Event()
        async def resolve(*args):
            await gate.wait()
            return 'next.mp3', 'Next', 90
        song = main.Track('next', 'next', youtube=True)
        slot = AsyncPreparedAudio(song, resolve, Mock())
        original = N(check_stream=AsyncMock(), microphone=N(path='ffmpeg next'), camera=None)
        factory = Mock(return_value=original)
        settings = AudioSettings()
        slot.prime_stream(factory, settings)
        gate.set()
        await slot.stream_task
        original.check_stream.assert_awaited_once()
        source, *_ = await slot.consume()
        stream = await slot.consume_stream(source, settings)
        await stream.check_stream()
        original.check_stream.assert_awaited_once()
        self.assertIsNone(await slot.consume_stream(source, AudioSettings(effect='echo')))

    async def test_discard_keeps_input_until_probe_finishes_then_cleans_once(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def check():
            started.set()
            await finish.wait()
        cleanup = Mock()
        async def resolve(*args): return 'next.mp3', 'Next', 90
        slot = AsyncPreparedAudio(main.Track('next','next'), resolve, cleanup)
        slot.prime_stream(lambda *a, **k: N(check_stream=check, microphone=None), AudioSettings())
        await started.wait()
        slot.discard()
        cleanup.assert_not_called()
        finish.set()
        await slot.stream_task
        await asyncio.sleep(0)
        cleanup.assert_called_once_with('next.mp3')

    async def test_third_background_probe_does_not_wait_behind_two_groups(self):
        from playback_prepare import decoder_slots
        slots = decoder_slots()
        await slots.acquire(); await slots.acquire()
        async def resolve(*args): return 'next.mp3', 'Next', 90
        slot = AsyncPreparedAudio(main.Track('next','next'), resolve, Mock())
        factory = Mock()
        try:
            slot.prime_stream(factory, AudioSettings())
            await slot.consume()
            self.assertIsNone(await asyncio.wait_for(slot.consume_stream('next.mp3', AudioSettings()), .2))
            factory.assert_not_called()
        finally:
            slots.release(); slots.release()

    async def test_failed_preprobe_returns_to_normal_playback(self):
        async def resolve(*args): return 'next.mp3', 'Next', 90
        slot = AsyncPreparedAudio(main.Track('next','next'), resolve, Mock())
        slot.prime_stream(lambda *a, **k: N(check_stream=AsyncMock(side_effect=ValueError), microphone=None), AudioSettings())
        await slot.consume()
        self.assertIsNone(await slot.consume_stream('next.mp3', AudioSettings()))

    async def test_real_decoder_preserves_effect_and_welcome_without_double_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'song.mp3')
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                            'sine=frequency=440:duration=1','-y',path], check=True, capture_output=True)
            stream = CheckedAudioStream(main.audio_stream(path, settings=AudioSettings(effect='echo')))
            await stream.check_stream()
            pristine = stream.microphone.path
            self.assertEqual(pristine.count('aecho='), 1)
            from welcome_audio import WelcomeAudio
            welcome = WelcomeAudio(); welcome.ready = True
            wrapped, pipe = welcome.prepare(-100, stream)
            try:
                await wrapped.check_stream()
                await wrapped.check_stream()
                command = shlex.split(wrapped.microphone.path)
                self.assertEqual(command.count('-filter_complex'), 1)
                self.assertEqual(command[command.index('-filter_complex')+1].count('aecho='), 1)
                process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                             stderr=asyncio.subprocess.DEVNULL)
                try:
                    pcm, _ = await asyncio.wait_for(process.communicate(), 5)
                finally:
                    if process.returncode is None:
                        process.kill(); await process.communicate()
                self.assertEqual(process.returncode, 0)
                self.assertGreater(len(pcm), 48000)
            finally:
                pipe.close()


class WarmVoiceTests(unittest.IsolatedAsyncioTestCase):
    def calls(self, active=()):
        async def current(): return dict.fromkeys(active)
        class Calls:
            play = AsyncMock()
            @property
            def calls(self): return current()
        return Calls()

    async def test_empty_media_join_does_not_create_vc(self):
        calls = self.calls(); fresh = set()
        with patch('playback_prepare.clear_stale_voice', AsyncMock()) as clear:
            await warm_voice(calls, object(), -100, 7, fresh)
        clear.assert_awaited_once()
        args = calls.play.call_args.args
        self.assertEqual(args[:2], (-100, None))
        self.assertFalse(args[2].auto_start)
        self.assertIn(-100, fresh)

    async def test_existing_connection_is_not_replaced(self):
        calls = self.calls([-100])
        await warm_voice(calls, None, -100, 7, set())
        calls.play.assert_not_awaited()

    async def test_warm_failure_is_nonfatal_but_cancel_propagates(self):
        calls = self.calls()
        calls.play = AsyncMock(side_effect=RuntimeError)
        await warm_voice(calls, None, -100, 7, {-100})
        calls.play.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await warm_voice(calls, None, -100, 7, {-100})

    async def test_advance_resolves_while_join_is_pending(self):
        join_started, resolved = asyncio.Event(), asyncio.Event()
        async def warm():
            join_started.set()
            await resolved.wait()
        async def resolve(*args):
            await join_started.wait()
            resolved.set()
            return 'song.mp3','Song',90
        song = main.Track('Song','Song',youtube=True)
        player = main.Player(queue=deque([song]), resolver=resolve, voice_warmer=warm)
        ns = dict(vars(main)); ns.update(players={-100:player}, fresh_voice_chats={-100},lybot=None,
            calls=N(),audio_stream=Mock(),play_with_reconnect=AsyncMock(),wait_for_audio_frames=AsyncMock(),
            clean_temp=Mock(),prepare_next_song=Mock(),announce_now_playing=AsyncMock())
        await asyncio.wait_for(closure('advance',ns)(-100), 1)
        await player.announce_task
        self.assertIs(player.current,song)
        self.assertTrue(resolved.is_set())

    async def test_stop_cancels_warm_join_and_resolution(self):
        begun = asyncio.Event(); canceled = asyncio.Event()
        async def warm():
            begun.set()
            try: await asyncio.Event().wait()
            finally: canceled.set()
        async def resolve(*args): await asyncio.Event().wait()
        player = main.Player(queue=deque([main.Track('Song','Song',youtube=True)]), resolver=resolve, voice_warmer=warm)
        ns=dict(vars(main));ns.update(players={-100:player}, calls=N(), lybot=None,clean_temp=Mock())
        task=asyncio.create_task(closure('advance',ns)(-100))
        await begun.wait()
        self.assertTrue(main.cancel_active_loading(player))
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(canceled.is_set())
        self.assertIsNone(player.loading_task)

    async def test_skip_pauses_old_stream_before_loading_next(self):
        song=main.Track('old','old');player=main.Player(current=song,skip_pending=True)
        order=[]
        async def pause(*args):order.append('pause')
        async def advance(*args):order.append('advance')
        ns=dict(vars(main));ns.update(players={-100:player},calls=N(pause=pause),advance=advance)
        await closure('skip_in_background',ns)(-100,song)
        self.assertEqual(order,['pause','advance'])
        self.assertFalse(player.skip_pending)
