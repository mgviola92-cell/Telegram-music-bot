import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from nubcoders_audio import (NubCodersAudioStream, is_nubcoders_stream,
                             verify_nubcoders_audio)

VALID = 'https://api.nubcoders.com/stream/resolver/sample-signed-id'


class NubCodersAudioTests(unittest.IsolatedAsyncioTestCase):
    def test_signed_url_allowlist_only(self):
        self.assertTrue(is_nubcoders_stream(VALID))
        for url in ('http://api.nubcoders.com/stream/resolver/id',
                    'https://bad.example/stream/resolver/id',
                    'https://api.nubcoders.com.evil.net/stream/resolver/id',
                    'https://api.nubcoders.com/other',
                    'https://user:pw@api.nubcoders.com/stream/resolver/id'):
            self.assertFalse(is_nubcoders_stream(url), url)

    async def test_native_stream_uses_ffmpeg_input_without_head_probe(self):
        stream = NubCodersAudioStream(VALID)
        with patch('nubcoders_audio.build_command',
                   return_value=['ffmpeg','-i',VALID,'-f','s16le','pipe:1']):
            await stream.check_stream()
        self.assertIn('api.nubcoders.com/stream/resolver/sample-signed-id',
                      stream.microphone.path)
        self.assertIn('-rw_timeout 8000000', stream.microphone.path)
        self.assertIn('pipe:1', stream.microphone.path)

    async def test_probe_accepts_real_pcm(self):
        process=SimpleNamespace(returncode=0,
                                communicate=AsyncMock(return_value=(bytes(4800),b'')))
        with patch('nubcoders_audio.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=process)) as start:
            result=await verify_nubcoders_audio(VALID)
        self.assertTrue(result)
        self.assertIn('-t',start.call_args.args)

    async def test_probe_rejects_empty_pcm_and_failed_decoder(self):
        process=SimpleNamespace(returncode=1,
                                communicate=AsyncMock(return_value=(b'',b'err')))
        with patch('nubcoders_audio.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=process)):
            self.assertFalse(await verify_nubcoders_audio(VALID))

    async def test_probe_rejects_other_hosts_before_launch(self):
        with patch('nubcoders_audio.asyncio.create_subprocess_exec',
                   new=AsyncMock()) as start:
            self.assertFalse(await verify_nubcoders_audio('https://localhost/stream/resolver/x'))
        start.assert_not_awaited()

    async def test_main_audio_stream_uses_signed_adapter(self):
        from main import audio_stream
        self.assertIsInstance(audio_stream(VALID),NubCodersAudioStream)
