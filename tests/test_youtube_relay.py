import asyncio
import io
import json
import shlex
import struct
import unittest
import wave
from unittest.mock import patch, AsyncMock, MagicMock, Mock
from aiohttp import web

from audio_options import AudioSettings
from youtube_relay import (ENDPOINT, RelayAudioStream, relay_source, source_id,
                           relay_command, resolve_request, RelayTrack, _CACHE)


class RelaySafety(unittest.TestCase):
    def test_marker_is_an_exact_fixed_endpoint_and_video_id(self):
        source = relay_source('jNQXAC9IVRw')
        self.assertEqual(source_id(source), 'jNQXAC9IVRw')
        for bad in [source + '&url=http://127.0.0.1', source.replace('https:', 'http:'),
                    source.replace('deno.net', 'deno.net.evil'), ENDPOINT,
                    'https://127.0.0.1/download#video_id=jNQXAC9IVRw']:
            self.assertIsNone(source_id(bad))
        with self.assertRaises(ValueError):relay_source('a; echo unsafe')

    def test_native_pcm_and_filters_survive_post_injection(self):
        command = relay_command(['ffmpeg', '-reconnect_at_eof', '1', '-i', 'placeholder',
                                 '-f', 's16le', 'pipe:1'], relay_source('jNQXAC9IVRw'))
        self.assertEqual(command[command.index('-reconnect_at_eof') + 1], '0')
        data = json.loads(bytes.fromhex(command[command.index('-post_data') + 1]))
        self.assertEqual(data, {'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw', 'type': 'audio'})
        self.assertEqual(command[-3:], ['-f', 's16le', 'pipe:1'])


class RelayIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_cold_relay_sender_keeps_one_connection_past_ordinary_deadline(self):
        from main import play_with_reconnect
        from ntgcalls import ConnectionMode
        from types import SimpleNamespace as N
        from youtube_relay import sender_startup_timeout
        source = RelayAudioStream(relay_source('jNQXAC9IVRw'))
        self.assertEqual(sender_startup_timeout(N(original=N(original=source))), 16)
        self.assertEqual(sender_startup_timeout(object()), 4)
        async def slow_resume(chat):
            await asyncio.sleep(.06)
        calls = N(play=AsyncMock(), leave_call=AsyncMock(), resume=AsyncMock(side_effect=slow_resume),
                  unmute=AsyncMock(), _binding=N(get_connection_mode=AsyncMock(return_value=ConnectionMode.RTC),
                                               get_state=AsyncMock(return_value=N(muted=False))))
        # Scale 4s/16s to .04s/.16s. The .06s first audio must exceed the old
        # budget yet succeed once with the relay-specific budget.
        real_wait_for = asyncio.wait_for
        async def scaled_wait_for(awaitable, timeout):
            return await real_wait_for(awaitable, timeout / 100 if timeout in (4, 16) else timeout)
        with patch('voice_transport.asyncio.wait_for', side_effect=scaled_wait_for):
            await play_with_reconnect(calls, -100, source)
        calls.play.assert_awaited_once()
        calls.leave_call.assert_not_awaited()

    async def test_explicit_trial_routes_without_changing_normal_play(self):
        import main
        from types import SimpleNamespace as N
        from test_music_extensions import closure
        for action, query, expected in [('ytplay', 'Attention', True),
                                         ('play', 'https://youtu.be/jNQXAC9IVRw', True),
                                         ('play', 'Attention', False)]:
            player = main.Player()
            progress = N(edit_text=AsyncMock())
            msg = N(command=[action, query], text='/' + action + ' ' + query,
                    reply_to_message=None, chat=N(id=-100), reply_text=AsyncMock(return_value=progress))
            factory = Mock(wraps=main.Track)
            ns = dict(vars(main));ns.update(Track=factory, private_songs=N(preempt=lambda: None),
                players={-100: player}, youtube_relay_enabled=lambda: True,
                resolve_for_queue=object(), start_autoplay=Mock(),
                ensure_assistant_in_group=AsyncMock(side_effect=RuntimeError('fixture gate')))
            await closure('play_command', ns)(None, msg)
            self.assertEqual(factory.call_args.kwargs.get('relay_requested', False), expected)

    async def test_link_metadata_is_cached_and_duration_checked(self):
        _CACHE.clear()
        track = RelayTrack(relay_source('jNQXAC9IVRw'), 'Real title · YouTube', 19)
        with patch('youtube_relay._link_metadata', return_value=track) as lookup:
            self.assertEqual(await resolve_request('https://youtu.be/jNQXAC9IVRw'), track)
            self.assertEqual(await resolve_request('https://youtube.com/watch?v=jNQXAC9IVRw'), track)
            self.assertEqual(lookup.call_count, 1)
        from youtube_relay import _duration
        for value in (601, 0, float('nan'), float('inf')):
            with self.assertRaises(ValueError):_duration(value)

    async def test_normal_sources_and_relay_do_not_share_a_cache_key(self):
        from types import SimpleNamespace as N
        from shared_audio import SharedAudio
        requests = []
        async def resolve(track, prefer):
            requests.append(track.relay_requested)
            await asyncio.sleep(.01)
            return ('https://example.com/audio', 'title', 20)
        resolver = SharedAudio(resolve, lambda source: None)
        normal = N(source='Attention', video=False, relay_requested=False)
        relay = N(source='Attention', video=False, relay_requested=True)
        with patch('shared_audio.get_cached_song', return_value=None):
            await asyncio.gather(resolver.resolve(normal, True), resolver.resolve(relay, True))
        self.assertEqual(sorted(requests), [False, True])

    async def test_real_native_command_posts_json_and_decodes_with_effects(self):
        memory = io.BytesIO()
        with wave.open(memory, 'wb') as f:
            f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            f.writeframes(struct.pack('<h', 1000) * 16000)
        seen = []
        async def audio(request):
            seen.append(await request.json())
            return web.Response(body=memory.getvalue(), content_type='audio/wav')
        app = web.Application()
        app.router.add_post('/download', audio)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            stream = RelayAudioStream(relay_source('jNQXAC9IVRw'), AudioSettings(effect='tiny'))
            await stream.check_stream()
            self.assertIsNone(stream.camera)
            command = shlex.split(stream.microphone.path)
            self.assertIn('vibrato=f=5:d=0.12', command[command.index('-af') + 1])
            # Only the fixture swaps the fixed public endpoint for loopback.
            command[command.index('-i') + 1] = f'http://127.0.0.1:{port}/download'
            command[command.index('-t') + 1] = '0.25'
            proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 5)
            self.assertEqual(proc.returncode, 0, err[-500:])
            self.assertGreater(len(out), 0)
            self.assertLessEqual(len(out), 48000 * 2 * 2)
            self.assertEqual(seen, [{'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw', 'type': 'audio'}])
            await stream.check_stream()
            self.assertEqual(stream.microphone.path.count('-post_data'), 1)
        finally:
            await runner.cleanup()
