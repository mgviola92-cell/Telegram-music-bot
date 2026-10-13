"""No-network regression checks for the third-party Audius streaming fallback."""
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from audius_source import (
    AudiusOnlyRedirect, fetch_audius_audio, find_audius_tracks,
    safe_audius_url,
)
from main import clean_temp, suitable_recording


class FakeResponse:
    def __init__(self, body, url, mime):
        self.body = io.BytesIO(body)
        self.url = url
        self.status = 200
        self.headers = {"Content-Type": mime}

    def read(self, length=-1):
        return self.body.read(length)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.body.close()


class FakeOpener:
    def __init__(self, tracks, media=b"ID3" + b"\x01" * 1900):
        self.tracks = tracks
        self.media = media
        self.stream_count = 0
        self.search_count = 0

    def open(self, req, timeout=0):
        url = req.full_url
        if "/tracks/search?" in url:
            self.search_count += 1
            return FakeResponse(
                json.dumps({"data": self.tracks}).encode(),
                "https://api.audius.co/v1/tracks/search", "application/json",
            )
        if "/stream?" in url:
            self.stream_count += 1
            return FakeResponse(
                self.media, "https://cn1.audius.co/streams/file.mp3", "audio/mpeg"
            )
        raise AssertionError("Unknown request to " + url)


def track(**kwargs):
    record = {
        "id": "A1b2C3", "title": "Hello World",
        "user": {"name": "Test Artist"}, "duration": 180,
        "is_stream_gated": False, "is_streamable": True,
    }
    record.update(kwargs)
    return record


class AudiusSafetyTest(unittest.TestCase):
    def test_strict_https_host_policy(self):
        self.assertTrue(safe_audius_url("https://api.audius.co/v1/tracks/search"))
        self.assertTrue(safe_audius_url("https://cn1.audius.co/streams/example.mp3"))
        for bad in (
            "http://api.audius.co", "https://api.audius.co.evil.test/",
            "https://127.0.0.1/", "https://api.audius.co:8888/",
            "https://user:password@api.audius.co/",
            "https://api.audius.co:invalid/",
            "file:///etc/passwd", "https://evil.test",
        ):
            self.assertFalse(safe_audius_url(bad))

    def test_search_response(self):
        opener = FakeOpener([track()])
        result = find_audius_tracks("Hello World", opener=opener)
        self.assertEqual(result[0]["id"], "A1b2C3")
        self.assertEqual(opener.search_count, 1)

    def test_reject_gated_recordings(self):
        opener = FakeOpener([track(is_stream_gated=True)])
        result = fetch_audius_audio(
            "Hello World Test Artist", match=suitable_recording, opener=opener,
        )
        self.assertIsNone(result)
        self.assertEqual(opener.stream_count, 0)

    def test_reject_unrelated_song(self):
        opener = FakeOpener([track()])
        self.assertIsNone(fetch_audius_audio(
            "Completely Different Song", match=suitable_recording, opener=opener,
        ))
        self.assertEqual(opener.stream_count, 0)

    def test_valid_public_audio_returns_temp_file_with_attribution(self):
        opener = FakeOpener([track()])
        result = fetch_audius_audio(
            "Hello World Test Artist", match=suitable_recording, opener=opener,
        )
        self.assertIsNotNone(result)
        path, title, duration = result
        self.assertEqual(duration, 180)
        self.assertIn("Audius", title)
        self.assertEqual(opener.stream_count, 1)
        clean_temp(path)

    def test_rejects_overlong_recording(self):
        opener = FakeOpener([track(duration=999)])
        self.assertIsNone(fetch_audius_audio(
            "Hello World", match=suitable_recording, opener=opener,
        ))

    def test_rejects_redirected_unknown_media_host(self):
        class MaliciousOpener(FakeOpener):
            def open(self, req, timeout=0):
                resp = super().open(req, timeout=timeout)
                if "/stream?" in req.full_url:
                    resp.url = "https://127.0.0.1/internal"
                return resp
        opener = MaliciousOpener([track()])
        self.assertIsNone(fetch_audius_audio(
            "Hello World Test Artist", match=suitable_recording, opener=opener,
        ))


if __name__ == "__main__":
    unittest.main()
