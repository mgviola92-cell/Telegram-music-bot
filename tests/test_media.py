"""Offline checks for the music bot's two failure-prone download paths."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aiohttp import web

from main import download_bot_api_file, is_youtube_url, soundcloud_results


class TelegramHttpsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake_size = 6
        self.payload = b"sample audio"
        app = web.Application()

        async def get_file(request):
            body = await request.post()
            if body.get("file_id") != "mock_file_id":
                return web.json_response({"ok": False}, status=400)
            return web.json_response({
                "ok": True,
                "result": {"file_path": "audio/file.mp3", "file_size": self.fake_size},
            })

        async def file_data(request):
            return web.Response(body=self.payload)

        app.router.add_post("/botfake/getFile", get_file)
        app.router.add_get("/file/botfake/audio/file.mp3", file_data)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.folder = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.folder.cleanup()
        await self.runner.cleanup()

    async def test_https_download(self):
        target = str(Path(self.folder.name) / "audio.mp3")
        output = await download_bot_api_file(
            "mock_file_id", target, 20 * 1024 * 1024, "fake",
            api_root=self.url,
        )
        self.assertEqual(output, target)
        self.assertEqual(Path(target).read_bytes(), self.payload)

    async def test_size_rejected_and_partial_cleaned(self):
        self.fake_size = 30 * 1024 * 1024
        target = str(Path(self.folder.name) / "too_big.mp3")
        with self.assertRaisesRegex(RuntimeError, "Telegram HTTP media download failed"):
            await download_bot_api_file(
                "mock_file_id", target, 20 * 1024 * 1024, "fake",
                api_root=self.url,
            )
        self.assertFalse(Path(target).exists())


class MusicResolverTest(unittest.TestCase):
    def test_youtube_whitelist(self):
        self.assertTrue(is_youtube_url("https://www.youtube.com/watch?v=example"))
        self.assertFalse(is_youtube_url("https://youtube.com.evil.test/"))
        self.assertFalse(is_youtube_url("http://127.0.0.1/admin"))

    def test_soundcloud_skips_drm_and_mismatched_hosts(self):
        found = {
            "entries": [
                {"title": "Faded (drm)", "webpage_url": "https://soundcloud.com/user/drm", "is_drm": True},
                {"title": "Elsewhere", "webpage_url": "https://example.org/other"},
                {"title": "Alan Walker Faded cover", "webpage_url": "https://soundcloud.com/user/faded"},
                {"title": "Alan Walker Faded", "webpage_url": "https://soundcloud.com/user/original"},
            ]
        }
        mocked = MagicMock()
        mocked.__enter__.return_value.extract_info.return_value = found
        with patch("yt_dlp.YoutubeDL", return_value=mocked):
            urls = soundcloud_results("Alan Walker Faded", max_tracks=4)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://soundcloud.com/user/faded")
        self.assertEqual(urls[1], "https://soundcloud.com/user/original")


if __name__ == "__main__":
    unittest.main()
