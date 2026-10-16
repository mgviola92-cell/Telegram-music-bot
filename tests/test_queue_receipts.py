import ast
import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import AsyncMock,Mock
import main
from main import Player,Track
from music_ui import queue_popup,queue_track_title
from queue_receipts import acknowledge_queued,sync_receipts
from prefetch import AsyncPreparedAudio

class QueueReceiptTest(unittest.IsolatedAsyncioTestCase):
    async def test_existing_play_reply_changes_to_real_song_without_a_new_message(self):
        track=Track('purple pgb wink','purple pgb wink',youtube=True)
        player=Player(queue=deque([track]));message=Obj(edit_text=AsyncMock())
        await acknowledge_queued(player,track,message)
        self.assertIn('ရှာနေဆဲ',message.edit_text.call_args.args[0])
        track.title='Purple – Wink & pGb · VoiceShazamBot';track.metadata_resolved=True
        await sync_receipts(player)
        self.assertIn('▸ Title : Purple – Wink & pGb',message.edit_text.call_args.args[0])
        self.assertIn('Queue #1',message.edit_text.call_args.args[0])
        self.assertNotIn('purple pgb wink',message.edit_text.call_args.args[0])
        player.queue_refresh_task.cancel()
        await asyncio.gather(player.queue_refresh_task,return_exceptions=True)

    async def test_prefetch_metadata_updates_the_registered_receipt(self):
        track=Track('purple pgb wink','purple pgb wink',youtube=True)
        player=Player(queue=deque([track]));message=Obj(edit_text=AsyncMock())
        await acknowledge_queued(player,track,message)
        async def resolver(track,prefer_stream):return 'song.mp3','Purple – Wink & pGb · LyBot',163
        slot=AsyncPreparedAudio(track,resolver,lambda p:None)
        slot.on_metadata=lambda:None
        await slot.task
        await asyncio.sleep(0)
        await sync_receipts(player)
        self.assertIn('Purple – Wink & pGb',message.edit_text.call_args.args[0])
        self.assertTrue(track.metadata_resolved)
        player.queue_refresh_task.cancel()
        await asyncio.gather(player.queue_refresh_task,return_exceptions=True)
        slot.discard()

    async def test_queue_popup_button_does_not_send_any_group_message(self):
        tree=ast.parse(Path(main.__file__).read_text())
        outer=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        callback=next(n for n in outer.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='panel_callback')
        callback.decorator_list=[]
        song=Track('Purple – Wink & pGb · LyBot','query',youtube=True,metadata_resolved=True)
        player=Player(current=song,panel_id=50)
        cb=Obj(data='mud:queue:123',message=Obj(chat=Obj(id=123),id=50),answer=AsyncMock())
        bot=Obj(send_message=AsyncMock())
        ns=dict(vars(main));ns.update(players={123:player},bot=bot)
        exec(compile(ast.Module(body=[callback],type_ignores=[]),main.__file__,'exec'),ns)
        await ns['panel_callback'](None,cb)
        cb.answer.assert_awaited_once()
        self.assertTrue(cb.answer.call_args.kwargs['show_alert'])
        self.assertIn('Purple',cb.answer.call_args.args[0])
        bot.send_message.assert_not_called()

    def test_popup_fits_telegram_limit_without_source_or_filename_noise(self):
        text=queue_popup('Purple LYRICS VIDEO(MP3_160K).mp3 · SoundCloud (live)',
                         ['Purple – Wink & pGb · LyBot']*8)
        self.assertIn('Purple',text)
        self.assertNotIn('LYRICS',text)
        self.assertNotIn('SoundCloud',text)
        self.assertIn('+5 ပုဒ်',text)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,200)
        text=queue_popup('😀က'*200,['😀န'*200]*10)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,200)

    async def test_failed_lookup_does_not_leave_searching_forever(self):
        track=Track('query','query',youtube=True,metadata_failed=True)
        message=Obj(edit_text=AsyncMock());track.queue_receipt=message
        player=Player(queue=deque([track]))
        await sync_receipts(player)
        self.assertNotIn('ရှာနေဆဲ',message.edit_text.call_args.args[0])
        self.assertIn('ရှာမရသေး',message.edit_text.call_args.args[0])
