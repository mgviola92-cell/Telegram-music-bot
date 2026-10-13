"""Offline tests for small ephemeral song cache."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import song_cache
from main import clean_temp


class LocalSongCacheTest(unittest.TestCase):
    def test_cache_keeps_private_copy_for_subsequent_play(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "original.mp3"
            original.write_bytes(b"valid example audio data")
            with patch.object(song_cache, "_CACHE_DIR", root / "cache"), patch.object(
                song_cache, "_ITEMS", {}
            ):
                song_cache._CACHE_DIR.mkdir()
                self.assertTrue(song_cache.save_cached_song(
                    "test track", False, str(original), "Track · Source", 180.0
                ))
                cached = song_cache.get_cached_song("test track", False)
                self.assertIsNotNone(cached)
                media, title, duration = cached
                self.assertEqual(title, "Track · Source")
                self.assertEqual(duration, 180.0)
                self.assertEqual(Path(media).read_bytes(), original.read_bytes())
                clean_temp(media)
                self.assertFalse(Path(media).exists())
                self.assertTrue(song_cache.get_cached_song("test track", False))
                media2, *_ = song_cache.get_cached_song("test track", False)
                clean_temp(media2)

    def test_large_audio_is_not_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "big.wav"
            original.write_bytes(b"short audio")
            with patch.object(song_cache, "_CACHE_DIR", root), patch.object(
                song_cache, "_ITEMS", {}
            ), patch.object(song_cache, "_MAX_ONE_FILE_BYTES", 4):
                self.assertFalse(song_cache.save_cached_song(
                    "huge", False, str(original), "large", 1
                ))
                self.assertIsNone(song_cache.get_cached_song("huge", False))


if __name__ == "__main__":
    unittest.main()
