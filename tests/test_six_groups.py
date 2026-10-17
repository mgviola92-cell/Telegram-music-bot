import asyncio
import tempfile
import shutil
import unittest
from pathlib import Path
from collections import deque
from types import SimpleNamespace as N
from unittest.mock import patch
from main import Track,Player
from queue_guard import admit,collapse_resolved,duplicate_text
from shared_audio import SharedAudio
from bandwidth_status import parse_usage,BandwidthStatus
from song_links import safe_link,message_link

class QueueTests(unittest.TestCase):
    def test_atomic_admission_loading_and_distinct_version(self):
        p=Player();a=Track('Perfect','Perfect',requester='A',requester_id=42)
        self.assertIsNone(admit(p,a))
        self.assertIs(admit(p,Track('perfect',' perfect ')),a)
        self.assertEqual(len(p.queue),1)
        p.queue.clear();p.loading_track=a
        self.assertIs(admit(p,Track('Perfect','Perfect')),a)
        self.assertIsNone(admit(p,Track('Perfect sped up','Perfect sped up')))
        self.assertIn('tg://user?id=42',duplicate_text(p,a))
    def test_metadata_duplicates_keep_first_requester(self):
        a=Track('Purple','one',metadata_resolved=True);b=Track('Purple','two',metadata_resolved=True)
        p=Player(queue=deque([a,b]));self.assertEqual(collapse_resolved(p),[(b,a)])
        self.assertEqual(list(p.queue),[a]);self.assertNotEqual(a.queue_token,b.queue_token)

class SharedTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_resolution_separate_copies_sequential_cache(self):
        calls=[];gate=asyncio.Event()
        with tempfile.TemporaryDirectory() as folder:
            original=Path(folder)/'song.mp3';original.write_bytes(b'audio')
            async def resolver(*args):calls.append(1);await gate.wait();return str(original),'Title',123
            shared=SharedAudio(resolver,lambda p:Path(p).unlink(missing_ok=True))
            import song_cache
            with patch.object(song_cache,'_ITEMS',{}):
                a=asyncio.create_task(shared.resolve(Track('x','unique-six-test'),True))
                b=asyncio.create_task(shared.resolve(Track('x','unique-six-test'),True))
                await asyncio.sleep(0);gate.set();ra,rb=await asyncio.gather(a,b)
                await asyncio.sleep(0)
                self.assertEqual(calls,[1]);self.assertNotEqual(ra[0],rb[0]);self.assertFalse(original.exists())
                self.assertEqual(Path(ra[0]).read_bytes(),b'audio')
                rc=await shared.resolve(Track('x','unique-six-test'),True)
                self.assertEqual(calls,[1])
                for result in [ra,rb,rc]:shutil.rmtree(Path(result[0]).parent)
                for path,*_ in song_cache._ITEMS.values():path.unlink(missing_ok=True)
    async def test_cancel_one_waiter_preserves_stream_for_other(self):
        gate=asyncio.Event();calls=[]
        async def resolver(*args):calls.append(1);await gate.wait();return 'https://cdn.example/audio','Title',1
        shared=SharedAudio(resolver,lambda p:self.fail('URL cleanup'))
        a=asyncio.create_task(shared.resolve(Track('x','stream-six-test'),True));b=asyncio.create_task(shared.resolve(Track('x','stream-six-test'),True))
        await asyncio.sleep(0);a.cancel();await asyncio.gather(a,return_exceptions=True);gate.set()
        self.assertEqual((await b)[0],'https://cdn.example/audio');self.assertEqual(calls,[1])

class LinksMetricsTests(unittest.TestCase):
    def test_only_public_song_pages(self):
        self.assertTrue(safe_link('https://song.link/i/123'))
        for url in ['https://cdn.example/audio?token=secret','https://song.link/','https://song.link.evil/x','https://user:pass@song.link/x']:
            self.assertFalse(safe_link(url))
        self.assertEqual(message_link(N(entities=[N(url='https://song.link/i/123')])),'https://song.link/i/123')
    def test_metrics_units_and_invalid_usage(self):
        used,stamp=parse_usage([{'unit':'mb','values':[{'value':12.5,'timestamp':'2026-10-17T10:00:00Z'}]}])
        self.assertEqual(used,12500000);self.assertIsNotNone(stamp.tzinfo)
        with self.assertRaises(ValueError):parse_usage([{'unit':'mb','values':[{'value':'nan','timestamp':'2026-10-17T10:00:00Z'}]}])
        with patch.dict('os.environ',{},clear=True):self.assertIn('မချိတ်ရသေး',BandwidthStatus().text())

class RemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_requester_can_remove_own_but_other_member_cannot(self):
        import ast,main
        from unittest.mock import AsyncMock,Mock
        tree=ast.parse(Path(main.__file__).read_text())
        outer=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        node=next(n for n in outer.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='panel_callback');node.decorator_list=[]
        track=Track('Song','query',requester_id=42);p=Player(current=Track("Now","now"),queue=deque([track]),panel_id=50)
        cb=N(data='mud:qdel'+track.queue_token+':123',message=N(chat=N(id=123),id=50),from_user=N(id=99),answer=AsyncMock())
        ns=dict(vars(main));ns.update(players={123:p},is_chat_admin=AsyncMock(return_value=False),prepare_next_song=Mock(),update_player_panel=AsyncMock())
        exec(compile(ast.Module(body=[node],type_ignores=[]),main.__file__,'exec'),ns)
        await ns['panel_callback'](None,cb);self.assertEqual(list(p.queue),[track])
        cb.from_user.id=42;await ns['panel_callback'](None,cb);self.assertEqual(list(p.queue),[])
        ns['prepare_next_song'].assert_called_once_with(p)
        await ns['panel_callback'](None,cb);ns['prepare_next_song'].assert_called_once()

