import asyncio
import io
import shlex
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from audio_options import AudioSettings
from youtube_relay import (ENDPOINT, RelayAudioStream, audio_provider, enabled,
                           relay_command, relay_source, sender_startup_timeout,
                           vapters_url)


class ProviderSelection(unittest.TestCase):
    def test_default_is_vapters_and_legacy_is_explicit_only(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(audio_provider(), 'vapters')
            self.assertTrue(enabled())
            with patch('youtube_relay._legacy_relay_command') as legacy:
                command = relay_command(['ffmpeg', '-i', 'placeholder', '-f', 's16le', 'pipe:1'],
                                        relay_source('nfs8NYg7yQM'), fast_mp3=True)
                legacy.assert_not_called()
            self.assertEqual(command[command.index('-i') + 1], vapters_url('nfs8NYg7yQM'))
            self.assertNotIn(ENDPOINT, command)
            for flag in ('-post_data', '-method', '-headers'):
                self.assertNotIn(flag, command)
            self.assertEqual(command[command.index('-f') + 1], 's16le')
        with patch.dict('os.environ', {'YOUTUBE_AUDIO_PROVIDER': 'legacy'}):
            command = relay_command(['ffmpeg', '-i', 'placeholder'], relay_source('nfs8NYg7yQM'))
            self.assertIn('-post_data', command)
            self.assertIn(ENDPOINT, command)

    def test_invalid_ids_and_disabled_configuration_never_open_a_source(self):
        for value in ('nfs8NYg7yQM?url=evil', '../nfs8NYg7yQM', 'https://example.com', ''):
            with self.assertRaises(ValueError):
                vapters_url(value)
        for provider in ('off', 'unknown'):
            with patch.dict('os.environ', {'YOUTUBE_AUDIO_PROVIDER': provider}):
                self.assertFalse(enabled())
                with self.assertRaises(ValueError):
                    RelayAudioStream(relay_source('nfs8NYg7yQM'))

    def test_effect_export_uses_same_get_adapter_and_keeps_96k_limit(self):
        from effect_exports import export_command
        with patch.dict('os.environ', {'YOUTUBE_AUDIO_PROVIDER': 'vapters'}):
            command = export_command(relay_source('nfs8NYg7yQM'), Path('result.mp3'),
                                     AudioSettings(effect='echo'), 120)
        self.assertIn(vapters_url('nfs8NYg7yQM'), command)
        self.assertNotIn('-post_data', command)
        self.assertNotIn(ENDPOINT, command)
        self.assertEqual(command[command.index('-b:a') + 1], '96k')
        self.assertIn('aecho=', command[command.index('-af') + 1])


class VaptersCommand(unittest.IsolatedAsyncioTestCase):
    async def test_provider_is_frozen_and_wrapped_sender_budget_survives(self):
        with patch.dict('os.environ', {'YOUTUBE_AUDIO_PROVIDER': 'vapters'}):
            stream = RelayAudioStream(relay_source('nfs8NYg7yQM'))
        with patch.dict('os.environ', {'YOUTUBE_AUDIO_PROVIDER': 'legacy'}):
            await stream.check_stream()
        command = shlex.split(stream.microphone.path)
        self.assertIn(vapters_url('nfs8NYg7yQM'), command)
        self.assertNotIn('-post_data', command)
        self.assertEqual(sender_startup_timeout(N(original=N(original=stream))), 16)
        with patch.dict('os.environ', {'YOUTUBE_AUDIO_PROVIDER': 'legacy'}):
            legacy = RelayAudioStream(relay_source('nfs8NYg7yQM'))
        self.assertEqual(sender_startup_timeout(legacy), 16)
        self.assertEqual(sender_startup_timeout(object()), 4)

    async def test_real_native_get_command_decodes_mp3_and_m4a_with_effects(self):
        memory = io.BytesIO()
        with wave.open(memory, 'wb') as f:
            f.setparams((2, 2, 16000, 0, 'NONE', 'not compressed'))
            f.writeframes(struct.pack('<hh', 1000, -700) * 16000)
        with tempfile.TemporaryDirectory() as folder:
            for extension, codec in [('mp3', 'libmp3lame'), ('m4a', 'aac')]:
                with self.subTest(extension=extension):
                    target = Path(folder) / ('fixture.' + extension)
                    encode = await asyncio.create_subprocess_exec(
                        'ffmpeg', '-v', 'error', '-i', 'pipe:0', '-c:a', codec,
                        '-threads', '1', '-y', str(target), stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    _, err = await asyncio.wait_for(encode.communicate(memory.getvalue()), 5)
                    self.assertEqual(encode.returncode, 0, err)
                    payload = target.read_bytes()
                    seen = []
                    async def serve(request):
                        seen.append((request.method, request.query_string))
                        return web.Response(body=payload)
                    app = web.Application()
                    app.router.add_get('/audio', serve)
                    runner = web.AppRunner(app)
                    await runner.setup()
                    site = web.TCPSite(runner, '127.0.0.1', 0)
                    await site.start()
                    port = site._server.sockets[0].getsockname()[1]
                    try:
                        settings = AudioSettings(effect='tiny', normalize=True, quality='medium')
                        with patch.dict('os.environ', {'YOUTUBE_AUDIO_PROVIDER': 'vapters',
                                                      'YOUTUBE_RELAY_FAST_MP3': '1'}):
                            stream = RelayAudioStream(relay_source('nfs8NYg7yQM'), settings)
                            await stream.check_stream()
                        command = shlex.split(stream.microphone.path)
                        self.assertIsNone(stream.camera)
                        self.assertIn('loudnorm=', command[command.index('-af') + 1])
                        self.assertNotIn('-post_data', command)
                        self.assertEqual(command[command.index('-tls_verify') + 1], '1')
                        # Only the fixture changes HTTPS to loopback HTTP;
                        # FFmpeg rejects TLS-only options for an HTTP input.
                        for flag in ('-tls_verify', '-ca_file'):
                            if flag in command:
                                index = command.index(flag)
                                del command[index:index + 2]
                        command[command.index('-i') + 1] = f'http://127.0.0.1:{port}/audio?vid=false&quality=128'
                        command[command.index('-t') + 1] = '0.25'
                        process = await asyncio.create_subprocess_exec(
                            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        out, err = await asyncio.wait_for(process.communicate(), 5)
                        self.assertEqual(process.returncode, 0, err[-500:])
                        self.assertGreater(len(out), 0)
                        self.assertLessEqual(len(out), 32000 * 2 * 2)
                        self.assertTrue(all(method == 'GET' for method, _ in seen))
                        self.assertTrue(seen)
                    finally:
                        await runner.cleanup()

    async def test_named_play_still_races_all_providers_and_youtube_metadata(self):
        import main
        from test_music_extensions import closure
        track = N(source=relay_source('nfs8NYg7yQM'), title='Attention · YouTube', duration=231)
        lookup = AsyncMock(return_value=track)
        calls = []
        class Race:
            async def race(self, primary, query, *, extra):
                calls.append((primary, query))
                return await extra()
        primary = Mock()
        namespace = dict(vars(main), stream_only_enabled=lambda: False,
                         lybot=Race(), PreparedAudio=Mock(return_value=primary),
                         youtube_relay_enabled=lambda: True,
                         relay_cooldown=N(available=lambda: True),
                         resolve_youtube_relay=lookup)
        result = await closure('resolve_for_queue', namespace)(main.Track('Attention', 'Attention', youtube=True), True)
        self.assertEqual(calls, [(primary, 'Attention')])
        lookup.assert_awaited_once_with('Attention')
        self.assertEqual(result[0], track.source)
