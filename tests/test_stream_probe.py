"""Decode real test HLS over HTTP using the installed FFmpeg/PyTgCalls command."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from aiohttp import web

from stream_probe import probe_audio_url, safe_error


class ProbeSafetyTest(unittest.TestCase):
    def test_signed_urls_and_tokens_are_not_logged(self):
        cleaned = safe_error('403 https://host/path?Policy=private&Signature=secret token=secret')
        self.assertNotIn('private', cleaned)
        self.assertNotIn('secret', cleaned)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class ProbeDecodeTest(unittest.IsolatedAsyncioTestCase):
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
            finally:
                await runner.cleanup()
