import asyncio
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

from vc_memory_guard import (ContainerMemory, allow_prefetch, container_memory,
                             need_idle_reclamation, reclaim_oldest_idle)


def player(*, idle=None, song=None, started=10, loading=None, queue=None):
    return N(idle_mode=idle, current=song, queue=[] if queue is None else queue,
             loading_task=loading, idle_started_at=started, started_at=started,
             idle_notice_sent=False, lock=asyncio.Lock())


class CgroupBudgetTests(unittest.TestCase):
    def test_real_512_mib_pressure_thresholds(self):
        capacity=512*1024*1024
        self.assertTrue(allow_prefetch(ContainerMemory(300*1024*1024, capacity)))
        self.assertFalse(allow_prefetch(ContainerMemory(416*1024*1024, capacity)))
        self.assertFalse(need_idle_reclamation(ContainerMemory(420*1024*1024,capacity)))
        self.assertTrue(need_idle_reclamation(ContainerMemory(440*1024*1024,capacity)))

    def test_unlimited_cgroup_never_blocks(self):
        with patch('vc_memory_guard._integer', return_value=None):
            self.assertIsNone(container_memory())
            self.assertTrue(allow_prefetch())
            self.assertFalse(need_idle_reclamation())

    def test_reads_real_cgroup_v2_pair_without_host_ram_guess(self):
        def fake(path):
            if path.endswith('memory.current'):return 490*1024*1024
            if path.endswith('memory.max'):return 512*1024*1024
            return None
        with patch('vc_memory_guard._integer', side_effect=fake):
            mem=container_memory()
        self.assertAlmostEqual(mem.ratio, 490/512)


class OldestIdleMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_one_old_idle_without_touching_playing_song(self):
        active=player(song=object())
        old=player(idle='silence',started=1)
        new=player(idle='silence',started=5)
        calls=N(leave_call=AsyncMock())
        welcome=N(close_chat=Mock())
        bot=N(send_message=AsyncMock())
        result=await reclaim_oldest_idle({1:active,2:old,3:new},calls,welcome,bot)
        self.assertEqual(result,2)
        calls.leave_call.assert_awaited_once_with(2)
        self.assertIsNotNone(active.current)
        self.assertIsNone(old.idle_mode)
        self.assertEqual(new.idle_mode,'silence')
        welcome.close_chat.assert_called_once_with(2)
        bot.send_message.assert_awaited_once()

    async def test_no_idle_does_not_disconnect_playing_voice(self):
        p=player(song=object())
        calls=N(leave_call=AsyncMock())
        result=await reclaim_oldest_idle({1:p},calls,N(close_chat=Mock()),
                                         N(send_message=AsyncMock()))
        self.assertIsNone(result)
        calls.leave_call.assert_not_awaited()

    async def test_loader_or_queued_idle_protected(self):
        loader=asyncio.create_task(asyncio.sleep(20))
        try:
            a=player(idle='silence',loading=loader)
            b=player(idle='silence',queue=[object()])
            calls=N(leave_call=AsyncMock())
            result=await reclaim_oldest_idle({1:a,2:b},calls,N(close_chat=Mock()),
                                             N(send_message=AsyncMock()))
            self.assertIsNone(result)
            calls.leave_call.assert_not_awaited()
        finally:
            loader.cancel()
            await asyncio.gather(loader,return_exceptions=True)

    async def test_unavailable_native_release_keeps_idle_status(self):
        p=player(idle='silence')
        calls=N(leave_call=AsyncMock(side_effect=RuntimeError("temporarily busy")))
        self.assertIsNone(await reclaim_oldest_idle({1:p},calls,N(close_chat=Mock()),
                                                   N(send_message=AsyncMock())))
        self.assertEqual(p.idle_mode,'silence')

    async def test_already_left_native_idle_can_be_cleaned(self):
        p=player(idle='silence')
        native_error=type('NotInCallError',(Exception,),{})
        calls=N(leave_call=AsyncMock(side_effect=native_error()))
        self.assertEqual(await reclaim_oldest_idle({1:p},calls,N(close_chat=Mock()),
                                                   N(send_message=AsyncMock())),1)
        self.assertIsNone(p.idle_mode)
