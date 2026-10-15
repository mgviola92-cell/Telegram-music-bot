"""Stream-only named audio must not touch any song download/cache path."""
import threading
import unittest
from unittest.mock import patch

from main import download_song, StreamOnlyPlaybackError


@patch.dict('os.environ', {'MUSIC_STREAM_ONLY': '1'})
class StreamOnlyTest(unittest.TestCase):
    @patch('main.soundcloud_results', return_value=['https://soundcloud.com/artist/example'])
    @patch('yt_dlp.YoutubeDL')
    def test_extracts_https_audio_without_any_disk_download(self, ydl, search):
        url = 'https://playback.media-streaming.soundcloud.cloud/test/playlist.m3u8'
        ydl.return_value.__enter__.return_value.extract_info.return_value = {
            'title': 'Example', 'duration': 120, 'url': url, 'protocol': 'm3u8_native',
        }
        with patch('main.get_cached_song') as cache, patch('main.save_cached_song') as save, \
             patch('main.tempfile.mkdtemp') as folder, patch('main.get_archive_audio') as archive, \
             patch('main.fetch_audius_audio') as audius:
            result = download_song('Example', False, prefer_stream=False)
            self.assertEqual(result[0], url)
            for mock in (cache, save, folder, archive, audius):
                mock.assert_not_called()
        self.assertFalse(ydl.return_value.__enter__.return_value.extract_info.call_args.kwargs['download'])

    @patch('main.soundcloud_results', return_value=['https://soundcloud.com/artist/example'])
    @patch('main.resolve_soundcloud_https', return_value=None)
    def test_failed_url_never_falls_through_to_downloader(self, resolve, search):
        with patch('main.tempfile.mkdtemp') as folder, patch('yt_dlp.YoutubeDL') as ydl:
            with self.assertRaises(StreamOnlyPlaybackError):
                download_song('Example', False, prefer_stream=True)
            folder.assert_not_called()
            ydl.assert_not_called()

    @patch('main.soundcloud_results', return_value=[])
    def test_cancelled_lookup_remains_cancelled(self, search):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(InterruptedError):
            download_song('Example', False, cancelled, True)
