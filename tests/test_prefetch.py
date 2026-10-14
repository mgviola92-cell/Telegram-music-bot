"""Offline regression tests for one-track-ahead VC prefetching."""
import asyncio
import threading
import unittest
from collections import deque
from unittest.mock import patch

from main import (
    Player, Track, discard_prepared, prepare_next_song,
)
from prefetch import PreparedAudio


class QueuePrefetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_preloads_only_first_next_song(self):
        seen = []
        calls = []
        def fake_loader(src, video, cancel_signal):
            seen.append(src)
            return "demo-track.mp3", "Demo", 180.0
        def fake_clean(path):
            calls.append(path)
        playing = Track("Now playing", "one", youtube=True)
        next_one = Track("Next", "two", youtube=True)
        next_two = Track("Later", "three", youtube=True)
        player = Player(current=playing, queue=deque([next_one, next_two]))
        with patch("main.download_song", side_effect=fake_loader), patch(
            "main.clean_temp", side_effect=fake_clean
        ):
            prepare_next_song(player)
            first = player.prepared
            self.assertIsNotNone(first)
            self.assertIs(first.track, next_one)
            prepare_next_song(player)
            self.assertIs(player.prepared, first)
            first_result = await first.consume()
            self.assertEqual(first_result[1], "Demo")
            self.assertEqual(seen, ["two"])
            player.queue.popleft()
            player.current = next_one
            prepare_next_song(player)
            self.assertIs(player.prepared.track, next_two)
            player.prepared.discard()
            await player.prepared.task

    async def test_discarded_finished_result_is_cleaned_once(self):
        cleaned = []
        def dummy_download(*args):
            return "temporary.mp3", "Song", 70.0
        track = Track("Test", "query", youtube=True)
        slot = PreparedAudio(track, dummy_download, cleaned.append)
        await slot.task
        slot.discard()
        slot._on_done(slot.task)
        self.assertEqual(cleaned, ["temporary.mp3"])

    async def test_discarded_running_prefetch_cancels_cooperatively(self):
        started = threading.Event()
        stopped = threading.Event()
        def slow_download(src, video, cancel_signal):
            started.set()
            if cancel_signal.wait(timeout=2):
                stopped.set()
                raise InterruptedError("prefetch cancelled")
            return "ignored.mp3", "Song", 70.0
        track = Track("Test", "query", youtube=True)
        slot = PreparedAudio(track, slow_download, lambda x: None)
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
        slot.discard()
        with self.assertRaises(InterruptedError):
            await asyncio.wait_for(slot.task, timeout=3)
        self.assertTrue(stopped.is_set())

    async def test_changed_queue_abandons_old_prefetch(self):
        def dummy_download(src, video, cancel_signal):
            return src+".mp3", "Song", 80.0
        cleanup = []
        player = Player(
            current=Track("Playing","current",youtube=True),
            queue=deque([Track("Next","one",youtube=True)]),
        )
        with patch("main.download_song", side_effect=dummy_download), patch(
            "main.clean_temp", side_effect=cleanup.append
        ):
            prepare_next_song(player)
            previous = player.prepared
            await previous.task
            player.queue.clear()
            prepare_next_song(player)
            self.assertIsNone(player.prepared)
            self.assertTrue(previous.discarded)
            self.assertEqual(cleanup, ["one.mp3"])

    async def test_not_prefetched_when_no_active_song(self):
        player = Player(queue=deque([Track("Next", "one", youtube=True)]))
        prepare_next_song(player)
        self.assertIsNone(player.prepared)


if __name__ == "__main__":
    unittest.main()
