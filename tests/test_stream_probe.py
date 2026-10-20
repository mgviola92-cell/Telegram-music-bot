"""Decode real test HLS over HTTP using the installed FFmpeg/PyTgCalls command."""
from pathlib import Path
import shutil
import asyncio
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from aiohttp import web

from stream_probe import probe_audio_url, safe_error
from network_audio import DirectAudioStream


class ProbeSafetyTest(unittest.TestCase):
    def test_signed_urls_and_tokens_are_not_logged(self):
        cleaned = safe_error('403 https://host/path?Policy=private&Signature=secret token=secret')
        self.assertNotIn('private', cleaned)
        self.assertNotIn('secret', cleaned)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class ProbeDecodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_muxed_video_input_streams_only_audio_with_bounded_decoder(self):
        with tempfile.TemporaryDirectory() as folder:
            movie=str(Path(folder)/'sample.mp4')
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                'color=c=black:s=320x180:r=5:d=2','-f','lavfi','-i',
                'sine=frequency=440:duration=2','-threads','1','-c:v','mpeg4',
                '-c:a','aac','-movflags','+faststart','-shortest',movie],
                check=True,capture_output=True,timeout=15)
            app=web.Application();app.router.add_static('/',folder)
            runner=web.AppRunner(app);await runner.setup()
            site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            port=site._server.sockets[0].getsockname()[1]
            try:
                with patch('network_audio.allowed_audio_cdn',return_value=True):
                    stream=DirectAudioStream(f'http://127.0.0.1:{port}/sample.mp4')
                stream._youtube_media=True
                await stream.check_stream()
                command=shlex.split(stream.microphone.path)
                self.assertLess(command.index('-vn'),command.index('-i'))
                self.assertEqual(command[command.index('-threads')+1],'1')
                command[-1:-1]=['-t','0.25']
                process=await asyncio.create_subprocess_exec(*command,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                out,err=await asyncio.wait_for(process.communicate(),5)
                self.assertEqual(process.returncode,0,err)
                self.assertGreater(len(out),0)
                self.assertIn('s16le',command)
            finally:await runner.cleanup()

    async def test_decodes_hls_http_in_memory_with_bounded_output(self):
        with tempfile.TemporaryDirectory() as folder:
            playlist = str(Path(folder) / 'test.m3u8')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=3', '-c:a', 'aac', '-f', 'hls',
                            '-hls_time', '1', playlist], check=True, capture_output=True, timeout=20)
            app = web.Application()
            app.router.add_static('/', folder)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                with patch('stream_probe.allowed_audio_cdn', return_value=True):
                    result = await probe_audio_url(f'http://127.0.0.1:{port}/test.m3u8', {},
                                                   timeout=3, variant='hls_no_eof')
                self.assertTrue(result['ok'], result)
                self.assertGreater(result['pcm_bytes'], 0)
                self.assertLessEqual(result['pcm_bytes'], 100000)
                self.assertGreater(result['peak'], 0)
                with patch('network_audio.allowed_audio_cdn', return_value=True):
                    stream = DirectAudioStream(f'http://127.0.0.1:{port}/test.m3u8')
                stream._direct_headers.update({'User-Agent': 'Test Client', 'Accept': '*/*'})
                # The ahead-of-time path must also decode the same HLS source.
                from playback_prepare import CheckedAudioStream
                stream = CheckedAudioStream(stream)
                await stream.check_stream()
                await stream.check_stream()
                command = shlex.split(stream.microphone.path)
                self.assertEqual(command[command.index('-reconnect_at_eof') + 1], '0')
                self.assertEqual(command.count('-headers'), 1)
                block = command[command.index('-headers') + 1]
                self.assertIn('User-Agent: Test Client\r\n', block)
                self.assertIn('Accept: */*\r\n', block)
                self.assertIn('s16le', command)
                command[-1:-1] = ['-t', '0.5']
                process = await asyncio.create_subprocess_exec(
                    *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertGreater(len(stdout), 0)
            finally:
                await runner.cleanup()
