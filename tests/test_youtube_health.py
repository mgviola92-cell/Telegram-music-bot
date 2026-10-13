"""Offline YouTube login-verification diagnostics and non-bypass cooldown tests."""
import unittest
from unittest.mock import patch

from youtube_health import YouTubeGuard, is_verification_challenge
from main import download_song, source_candidates


class YouTubeSafetyTest(unittest.TestCase):
    def test_exact_challenge_triggers_only_expected_errors(self):
        self.assertTrue(is_verification_challenge(
            "ERROR: [youtube] video: Sign in to confirm you’re not a bot."
        ))
        self.assertTrue(is_verification_challenge("Unusual traffic"))
        self.assertFalse(is_verification_challenge("This video is DRM protected"))
        self.assertFalse(is_verification_challenge("HTTP 429"))
        self.assertFalse(is_verification_challenge("Sign in to confirm your age"))

    def test_two_verification_failures_pause_after_first_retry(self):
        guard = YouTubeGuard(max_failures=2, window_seconds=600,
                             cooldown_seconds=900)
        error = RuntimeError("Sign in to confirm you're not a bot")
        self.assertTrue(guard.record_error(error, now=100))
        self.assertEqual(guard.seconds_remaining(now=105), 0)
        self.assertTrue(guard.record_error(error, now=150))
        self.assertEqual(guard.seconds_remaining(now=150), 900)
        self.assertEqual(guard.seconds_remaining(now=1000), 50)
        self.assertEqual(guard.seconds_remaining(now=1050), 0)

    def test_normal_provider_error_does_not_open_circuit(self):
        guard = YouTubeGuard(max_failures=2)
        self.assertFalse(guard.record_error("No matching track", now=10))
        self.assertEqual(guard.seconds_remaining(now=11), 0)

    def test_old_failures_expire_before_threshold(self):
        guard = YouTubeGuard(max_failures=2, window_seconds=600)
        error = "Sign in to confirm you're not a bot"
        guard.record_error(error, now=100)
        guard.record_error(error, now=800)
        self.assertEqual(guard.seconds_remaining(now=810), 0)

    def test_success_resets_verification_cooldown(self):
        guard = YouTubeGuard(max_failures=2)
        error = "Sign in to confirm you're not a bot"
        guard.record_error(error, now=100)
        guard.record_error(error, now=101)
        self.assertGreater(guard.seconds_remaining(now=102), 0)
        guard.record_success()
        self.assertEqual(guard.seconds_remaining(now=102), 0)

    def test_blocked_youtube_skips_extractor_for_direct_link(self):
        with patch("main.youtube_guard.seconds_remaining", return_value=900), patch(
            "yt_dlp.YoutubeDL"
        ) as ydl:
            with self.assertRaisesRegex(RuntimeError, "YouTube verification cooldown"):
                download_song("https://youtube.com/watch?v=abc123", False)
            ydl.assert_not_called()

    def test_song_search_still_tries_other_providers_when_blocked(self):
        with patch("main.youtube_guard.seconds_remaining", return_value=900), patch(
            "main.soundcloud_results", return_value=[
                "https://soundcloud.com/artist/recording",
            ]
        ), patch("yt_dlp.YoutubeDL") as ydl:
            mocked = ydl.return_value.__enter__.return_value
            mocked.extract_info.side_effect = RuntimeError("SoundCloud not accessible")
            with self.assertRaisesRegex(RuntimeError, "SoundCloud"):
                download_song("Alan Walker Faded", False)
            # YouTube extractor was never tried: only the SoundCloud candidate.
            self.assertEqual(mocked.extract_info.call_count, 1)


if __name__ == "__main__":
    unittest.main()
