"""Regression checks for bounded search reuse and audio-only HLS startup."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import direct_stream
from direct_stream import cached_search, remember_search, forget_search
from pytgcalls.ffmpeg import build_command
from pytgcalls.types.raw import AudioParameters


class SearchReuseTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(direct_stream._SEARCHES)
        direct_stream._SEARCHES.clear()

    def tearDown(self):
        direct_stream._SEARCHES.clear()
        direct_stream._SEARCHES.update(self.saved)

    def test_search_cache_expires_and_returns_isolated_results(self):
        urls = ['https://soundcloud.com/artist/example']
        with patch('direct_stream.time.monotonic', return_value=100):
            remember_search('Example', urls)
            result = cached_search(' example ', 3)
            self.assertEqual(result, urls)
            result.clear()
            self.assertEqual(cached_search('Example', 3), urls)
        with patch('direct_stream.time.monotonic', return_value=701):
            self.assertIsNone(cached_search('Example', 3))

    def test_cache_is_bounded_and_failures_can_invalidate_it(self):
        for number in range(60):
            remember_search(str(number), ['https://soundcloud.com/artist/example'])
        self.assertEqual(len(direct_stream._SEARCHES), direct_stream._SEARCH_MAX)
        self.assertIsNone(cached_search('0', 3))
        forget_search('59')
        self.assertIsNone(cached_search('59', 3))
        remember_search('empty', [])
        self.assertIsNone(cached_search('empty', 3))

    @patch('yt_dlp.YoutubeDL')
    def test_repeated_song_search_does_not_repeat_network_lookup(self, downloader):
        from main import soundcloud_results
        downloader.return_value.__enter__.return_value.extract_info.return_value = {
            'entries': [{'title': 'Cache Example', 'uploader': 'Artist',
                         'url': 'https://soundcloud.com/artist/cache-example'}],
        }
        first = soundcloud_results('Cache Example', 3)
        self.assertTrue(first)
        self.assertEqual(first, soundcloud_results('Cache Example', 3))
        downloader.return_value.__enter__.return_value.extract_info.assert_called_once()


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
class AudioHlsProbeTest(unittest.TestCase):
    def test_default_probe_detects_and_decodes_aac_hls(self):
        with tempfile.TemporaryDirectory() as folder:
            playlist = str(Path(folder) / 'test.m3u8')
            subprocess.run([
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                '-c:a', 'aac', '-b:a', '96k', '-f', 'hls', '-hls_time', '1', playlist,
            ], check=True, capture_output=True, timeout=20)
            probe = build_command('ffprobe', None, playlist, AudioParameters(48000, 2))
            metadata = json.loads(subprocess.run(probe, check=True, capture_output=True,
                                                 timeout=20).stdout)
            self.assertTrue(any(s['codec_type'] == 'audio' for s in metadata['streams']))
            command = build_command('ffmpeg', None, playlist, AudioParameters(48000, 2))
            output = subprocess.run(command, check=True, capture_output=True, timeout=20).stdout
            self.assertGreater(len(output), 48000)
