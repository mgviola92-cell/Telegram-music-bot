import asyncio
import unittest
from collections import deque
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock
import main
from source_race import ParallelSources
from test_music_extensions import closure

class QueueYouTubeMetadata(unittest.IsolatedAsyncioTestCase):
    async def test_queue_title_finds_youtube_when_soundcloud_and_bots_have_no_match(self):
        async def unavailable(_): raise LookupError('no match')
        sources=ParallelSources([N(find=unavailable)],lambda _:None)
        track=main.Track('song query','song query',youtube=True)
        player=main.Player(queue=deque([track]));player.queue_changed=Mock()
        resolve=AsyncMock(return_value=N(title='Real Song · YouTube',duration=220))
        ns=dict(vars(main));ns.update(lybot=sources,metadata_slots=asyncio.Semaphore(2),
            resolve_youtube_relay=resolve,youtube_relay_enabled=lambda:True,
            describe_primary=unavailable)
        closure('describe_queued',ns)(player,track)
        await asyncio.gather(*list(player.metadata_tasks.values()))
        self.assertTrue(track.metadata_resolved);self.assertFalse(track.metadata_failed)
        self.assertEqual(track.title,'Real Song · YouTube')
        resolve.assert_awaited_once_with('song query');player.queue_changed.assert_called_once()

    async def test_queue_lookup_supports_no_configured_bot_sources(self):
        track=main.Track('song query','song query',youtube=True)
        player=main.Player(queue=deque([track]))
        ns=dict(vars(main));ns.update(lybot=None,metadata_slots=asyncio.Semaphore(2),
            resolve_youtube_relay=AsyncMock(return_value=N(title='Real Song',duration=220)),
            youtube_relay_enabled=lambda:True,describe_primary=AsyncMock(return_value=None))
        closure('describe_queued',ns)(player,track)
        await asyncio.gather(*list(player.metadata_tasks.values()))
        self.assertTrue(track.metadata_resolved)

    async def test_removed_queue_item_is_not_overwritten_by_late_lookup(self):
        track=main.Track('query','query',youtube=True)
        player=main.Player(queue=deque([track]))
        gate=asyncio.Event()
        async def lookup(_):
            await gate.wait();return N(title='Late title',duration=20)
        ns=dict(vars(main));ns.update(lybot=None,metadata_slots=asyncio.Semaphore(2),
            resolve_youtube_relay=lookup,youtube_relay_enabled=lambda:True,
            describe_primary=AsyncMock(return_value=None))
        closure('describe_queued',ns)(player,track)
        tasks=list(player.metadata_tasks.values());await asyncio.sleep(0)
        player.queue.clear();gate.set();await asyncio.gather(*tasks)
        self.assertFalse(track.metadata_resolved)
