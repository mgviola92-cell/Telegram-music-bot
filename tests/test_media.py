"""Offline checks for the music bot's two failure-prone download paths."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aiohttp import web

from main import Player, cancel_active_loading, download_bot_api_file, download_song, is_youtube_url, soundcloud_results, suitable_recording


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

    def test_suitable_recording_filters_variants(self):
        self.assertFalse(suitable_recording(
            "Alan Walker Faded slowed + reverb", "Alan Walker Faded"
        ))
        self.assertFalse(suitable_recording(
            "Alan Walker Faded (COVER)", "Alan Walker Faded"
        ))
        self.assertFalse(suitable_recording(
            "Faded Nightcore", "Alan Walker Faded", "Alan Walker"
        ))
        self.assertFalse(suitable_recording(
            "Other Song", "Alan Walker Faded", "Unknown Artist"
        ))
        self.assertTrue(suitable_recording(
            "Alan Walker - Faded", "Alan Walker Faded"
        ))
        self.assertTrue(suitable_recording(
            "Faded", "Alan Walker Faded", "Alan Walker"
        ))
        self.assertTrue(suitable_recording(
            "Alan Walker Faded (Remix)", "Alan Walker Faded Remix"
        ))

    def test_soundcloud_skips_drm_covers_remixes_and_mismatched_hosts(self):
        found = {
            "entries": [
                {"title": "Alan Walker Faded (drm)", "webpage_url": "https://soundcloud.com/user/drm", "is_drm": True},
                {"title": "Elsewhere", "webpage_url": "https://example.org/other"},
                {"title": "Alan Walker Faded cover", "webpage_url": "https://soundcloud.com/user/cover"},
                {"title": "Alan Walker Faded slowed reverb", "webpage_url": "https://soundcloud.com/user/reverb"},
                {"title": "Completely different", "webpage_url": "https://soundcloud.com/user/other"},
                {"title": "Alan Walker Faded", "webpage_url": "https://soundcloud.com/user/original"},
                {"title": "Faded", "uploader": "Alan Walker", "webpage_url": "https://soundcloud.com/user/artist"},
            ],
        }
        mocked = MagicMock()
        mocked.__enter__.return_value.extract_info.return_value = found
        with patch("yt_dlp.YoutubeDL", return_value=mocked):
            urls = soundcloud_results("Alan Walker Faded", max_tracks=6)
        self.assertEqual(
            urls, [
                "https://soundcloud.com/user/original",
                "https://soundcloud.com/user/artist",
            ],
        )


class PlaybackLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_preempts_loading_and_releases_lock(self):
        """Cancel the child loader without cancelling the Telegram handler."""
        player = Player()
        ready = asyncio.Event()

        async def fake_loader():
            async with player.lock:
                player.loading_task = asyncio.current_task()
                player.cancel_download = threading.Event()
                try:
                    ready.set()
                    await asyncio.Event().wait()
                finally:
                    player.loading_task = None
                    player.cancel_download = None

        child = asyncio.create_task(fake_loader())
        await asyncio.wait_for(ready.wait(), timeout=1)
        self.assertTrue(cancel_active_loading(player))
        with self.assertRaises(asyncio.CancelledError):
            await child
        self.assertIsNone(player.loading_task)
        await asyncio.wait_for(player.lock.acquire(), timeout=1)
        player.lock.release()

        # A new /play is allowed immediately after /stop.
        self.assertFalse(cancel_active_loading(player))
        self.assertIsNone(player.cancel_download)

    async def test_source_download_aborts_before_network(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            await asyncio.to_thread(
                download_song, "https://www.youtube.com/watch?v=abc", False, event
            )




if __name__ == "__main__":
    unittest.main()
