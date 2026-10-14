"""Offline tests for rights-filtered Internet Archive fallback."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_source import (
    allowed_license, archive_https_url, archive_search, get_archive_audio,
    matches_recording, parse_duration,
)
from main import clean_temp, suitable_recording


class FakeResponse:
    def __init__(self, body, url):
        self.body = io.BytesIO(body)
        self.url = url
        self.status = 200
    def geturl(self):
        return self.url
    def read(self, limit=-1):
        return self.body.read(limit)
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.body.close()


class FakeOpener:
    def __init__(self, media_url="https://archive.org/download/PublicArchive/Hello%20World.mp3"):
        self.media_url = media_url
        self.calls = []
    def open(self, request, timeout=0):
        self.calls.append((request.full_url, timeout))
        if "/download/" in request.full_url:
            return FakeResponse(b"ID3" + bytes(3000), self.media_url)
        if "/advancedsearch.php?" in request.full_url:
            return FakeResponse(json.dumps({
                "response": {"docs": [
                    {"identifier": "PublicArchive", "title": "Hello World", "creator": "Test Artist"}
                ]}
            }).encode(), request.full_url)
        raise AssertionError("Unexpected request " + request.full_url)


def sample(licenseurl="https://creativecommons.org/licenses/by/4.0/"):
    return {
        "metadata": {
            "licenseurl": licenseurl, "creator": "Test Artist",
            "title": "Hello World",
        },
        "files": [{
            "name": "Hello World.mp3", "size": "3003", "length": "02:00",
            "source": "original",
        }],
    }


class ArchiveValidationTest(unittest.TestCase):
    def test_license_only_cc_by_or_cc0(self):
        self.assertEqual(allowed_license(
            "https://creativecommons.org/licenses/by/4.0/"
        ), "CC BY 4.0")
        self.assertEqual(allowed_license(
            "http://creativecommons.org/publicdomain/zero/1.0/"
        ), "CC0 1.0")
        for licenseurl in (
            "https://creativecommons.org/licenses/by-nc/4.0/",
            "https://creativecommons.org/licenses/by-nd/4.0/",
            "https://example.invalid/cc-by-4.0",
            "", "https://creativecommons.org/licenses/by-sa/4.0/",
        ):
            self.assertIsNone(allowed_license(licenseurl))

    def test_archive_https_hosts_only(self):
        self.assertTrue(archive_https_url("https://archive.org/metadata/sample"))
        self.assertTrue(archive_https_url("https://ia902300.us.archive.org/1.mp3"))
        for url in (
            "http://archive.org/metadata/sample",
            "https://archive.org.evil.test/private",
            "https://127.0.0.1/private",
            "https://user:pw@archive.org/file",
            "https://archive.org:8080/private",
            "https://archive.org:bad/private",
            "https://localhost/private",
        ):
            self.assertFalse(archive_https_url(url))

    def test_duration_and_filename_match(self):
        self.assertEqual(parse_duration("02:30"), 150)
        self.assertEqual(parse_duration("01:02:03"), 3723 if 3723<=600 else None)
        self.assertIsNone(parse_duration("15:01"))
        self.assertTrue(matches_recording(
            "Hello World.mp3", "Hello World Test Artist", "Hello World",
            artist="Test Artist", check=suitable_recording
        ))
        self.assertFalse(matches_recording(
            "Completely Different.mp3", "Hello World Test Artist", "Wrong",
            artist="Unknown", check=suitable_recording
        ))

    def test_search_only_uses_archive_api(self):
        opener = FakeOpener()
        results = archive_search("Hello World", opener=opener)
        self.assertEqual(results[0]["identifier"], "PublicArchive")
        self.assertEqual(len(opener.calls), 1)


class ArchiveDownloadTest(unittest.TestCase):
    def test_allowed_license_with_matching_mp3_downloads(self):
        opener = FakeOpener()
        with patch("archive_source.archive_search", return_value=[
            {"identifier": "PublicArchive", "title": "Hello World"}
        ]), patch("archive_source.read_json", return_value=sample()):
            result = get_archive_audio(
                "Hello World Test Artist", check=suitable_recording,
                opener=opener,
            )
        self.assertIsNotNone(result)
        path, title, length = result
        self.assertEqual(length, 120)
        self.assertIn("CC BY 4.0", title)
        self.assertIn("Test Artist", title)
        self.assertIn("archive.org/details/PublicArchive", title)
        self.assertEqual(Path(path).stat().st_size, 3003)
        clean_temp(path)

    def test_skip_nonpermitted_license_before_download(self):
        opener = FakeOpener()
        with patch("archive_source.archive_search", return_value=[
            {"identifier": "PublicArchive"}
        ]), patch("archive_source.read_json", return_value=sample(
            "http://creativecommons.org/licenses/by-nc/4.0/"
        )):
            result = get_archive_audio(
                "Hello World Test Artist", check=suitable_recording, opener=opener,
            )
        self.assertIsNone(result)
        self.assertEqual(opener.calls, [])

    def test_redirected_to_nonarchive_host_rejected(self):
        opener = FakeOpener(media_url="https://127.0.0.1/private")
        with patch("archive_source.archive_search", return_value=[
            {"identifier": "PublicArchive"}
        ]), patch("archive_source.read_json", return_value=sample()):
            result = get_archive_audio(
                "Hello World Test Artist", check=suitable_recording, opener=opener,
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
