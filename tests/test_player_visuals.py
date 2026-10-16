"""Offline UI rendering, safe artwork and direct-stream metadata checks."""
import threading
import unittest
from unittest.mock import MagicMock, patch

from player_visuals import (
    artwork_is_safe, allowed_audio_cdn, artwork_for, save_artwork,
    player_caption, format_clock, lyrics_search_url, stream_headers_for,
    save_stream_headers,
)
from music_ui import player_controls
from direct_stream import resolve_soundcloud_https


class ProgressCardTest(unittest.TestCase):
    def test_player_progress_and_animated_pulse(self):
        caption = player_caption("Hello World", 85, 250)
        self.assertIn("🎵 Hello World", caption)
        self.assertIn("01:25 / 04:10", caption)
        self.assertIn("━", caption)
        self.assertNotEqual(
            player_caption("Hello World", 10, 250),
            player_caption("Hello World", 50, 250),
        )

    def test_duration_unknown_pauses(self):
        caption = player_caption("A sample", 20, None, paused=True)
        self.assertIn("--:--", caption)
        self.assertIn("⏸", caption)
        self.assertEqual(format_clock(0), "00:00")

    def test_lyrics_button_requests_in_bot_text(self):
        markup=player_controls(-100,title='Hello World')
        lyrics=markup.inline_keyboard[-1][0]
        self.assertEqual(lyrics.callback_data,'lyrics:current')
        self.assertIsNone(lyrics.url)

    def test_safe_artwork_only_trusted_https_cdn(self):
        for bad in (
            "http://i1.sndcdn.com/artworks/abc",
            "https://i1.sndcdn.com.evil.com/abc",
            "https://127.0.0.1/private",
            "https://user@i1.sndcdn.com/private",
            "https://i1.sndcdn.com:8080/private",
            "https://i1.sndcdn.com:bad/private",
        ):
            self.assertFalse(artwork_is_safe(bad))
            self.assertFalse(allowed_audio_cdn(bad))
        thumb = "https://i1.sndcdn.com/artworks-test-t500x500.jpg"
        save_artwork("Test Track", thumb)
        self.assertEqual(artwork_for("test track"), thumb)
        self.assertTrue(allowed_audio_cdn("https://cf-hls-media.sndcdn.com/media/playlist.m3u8"))

    def test_only_safe_stream_headers_retained(self):
        url = "https://cf-hls-media.sndcdn.com/media/playlist.m3u8"
        save_stream_headers(url, {
            "User-Agent": "Test Client", "Authorization": "secret",
            "Cookie": "secret-cookie", "Referer": "https://soundcloud.com",
            "Origin": "https://soundcloud.com",
        })
        headers = stream_headers_for(url)
        self.assertEqual(headers.get("User-Agent"), "Test Client")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)


class DirectStreamingMetadataTest(unittest.TestCase):
    @patch("yt_dlp.YoutubeDL")
    def test_public_audio_cdn_resolves_without_download(self, youtube_dl):
        meta = {
            "title": "Hello World", "uploader": "Test Artist",
            "duration": 123, "access": "playable", "is_drm": False,
            "url": "https://cf-hls-media.sndcdn.com/playlist.m3u8",
            "protocol": "m3u8_native",
            "thumbnail": "https://i1.sndcdn.com/artworks-test.jpg",
            "http_headers": {"User-Agent": "test", "Cookie": "do-not-copy"},
        }
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = meta
        result = resolve_soundcloud_https(
            "https://soundcloud.com/test-artist/hello-world", "Hello World",
            check=lambda title, query, artist: title == query,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[2], 123)
        self.assertEqual(artwork_for("Hello World"), meta["thumbnail"])
        self.assertEqual(
            stream_headers_for(result[0]).get("User-Agent"), "test",
        )
        self.assertNotIn("Cookie", stream_headers_for(result[0]))
        youtube_dl.return_value.__enter__.return_value.extract_info.assert_called_once()

    @patch("yt_dlp.YoutubeDL")
    def test_official_soundcloud_aac_hls_host_resolves(self, youtube_dl):
        url = "https://playback.media-streaming.soundcloud.cloud/track/aac_160k/id/playlist.m3u8?token=example"
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Example", "duration": 120, "url": url,
            "protocol": "m3u8_native", "http_headers": {"User-Agent": "test"},
        }
        result = resolve_soundcloud_https("https://soundcloud.com/artist/example", "Example")
        self.assertEqual(result[0], url)
        self.assertEqual(stream_headers_for(url), {"User-Agent": "test"})
        self.assertFalse(artwork_is_safe(url))
        for bad in (
            "http://playback.media-streaming.soundcloud.cloud/a.m3u8",
            "https://playback.media-streaming.soundcloud.cloud.evil.test/a.m3u8",
            "https://evil.playback.media-streaming.soundcloud.cloud/a.m3u8",
            "https://user:pass@playback.media-streaming.soundcloud.cloud/a.m3u8",
            "https://playback.media-streaming.soundcloud.cloud:8080/a.m3u8",
        ):
            self.assertFalse(allowed_audio_cdn(bad))

    @patch("yt_dlp.YoutubeDL")
    def test_long_signed_cdn_url_is_streamed(self, youtube_dl):
        url = "https://cf-hls-media.sndcdn.com/playlist.m3u8?Policy=" + "x" * 3000
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Example", "duration": 120, "url": url,
            "protocol": "m3u8_native",
        }
        result = resolve_soundcloud_https("https://soundcloud.com/artist/example", "Example")
        self.assertEqual(result[0], url)
        self.assertFalse(allowed_audio_cdn(url + "x" * 16384))

    @patch("yt_dlp.YoutubeDL")
    def test_alternate_public_format_used_when_best_host_not_allowed(self, youtube_dl):
        url = "https://cf-hls-media.sndcdn.com/playlist.m3u8"
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Example", "duration": 120,
            "url": "https://other.example.invalid/download", "protocol": "https",
            "formats": [
                {"url": url, "protocol": "m3u8_native", "format_id": "hls_mp3"},
                {"url": url + "?preview", "protocol": "m3u8_native", "format_id": "hls_preview"},
                {"url": url + "?drm", "protocol": "m3u8_native", "has_drm": True},
            ],
        }
        result = resolve_soundcloud_https("https://soundcloud.com/artist/example", "Example")
        self.assertEqual(result[0], url)

    @patch("yt_dlp.YoutubeDL")
    def test_blocks_non_soundcloud_destinations(self, youtube_dl):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Hello World", "duration": 120,
            "url": "https://private.example.invalid/file.mp3",
            "protocol": "https",
        }
        self.assertIsNone(resolve_soundcloud_https(
            "https://soundcloud.com/test/hello", "Hello World"
        ))

    @patch("yt_dlp.YoutubeDL")
    def test_drm_and_geo_block_are_not_extracted(self, youtube_dl):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Hello World", "duration": 120,
            "url": "https://cf-hls-media.sndcdn.com/test.m3u8",
            "is_drm": True,
        }
        self.assertIsNone(resolve_soundcloud_https(
            "https://soundcloud.com/test/hello", "Hello World"
        ))
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value["is_drm"] = False
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value["access"] = "blocked"
        self.assertIsNone(resolve_soundcloud_https(
            "https://soundcloud.com/test/hello", "Hello World"
        ))

    @patch("yt_dlp.YoutubeDL")
    def test_unrelated_recording_rejected(self, youtube_dl):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Another Song", "duration": 123,
            "url": "https://cf-hls-media.sndcdn.com/test.m3u8",
            "protocol": "m3u8",
        }
        self.assertIsNone(resolve_soundcloud_https(
            "https://soundcloud.com/test/hello", "Hello World",
            check=lambda title, query, artist: title == query,
        ))

    @patch("yt_dlp.YoutubeDL")
    def test_cancellation_prevents_network(self, youtube_dl):
        stopped = threading.Event()
        stopped.set()
        with self.assertRaises(InterruptedError):
            resolve_soundcloud_https(
                "https://soundcloud.com/test/hello", "Hello", cancel_signal=stopped,
            )
        youtube_dl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
