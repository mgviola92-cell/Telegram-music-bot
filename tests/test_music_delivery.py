import asyncio
import io
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from greeting_recordings import decode_recording,encode_recording,fetch_bytes,RecordingCache,MAX_UPLOAD
from welcome_audio import WelcomeAudio,BYTES_PER_SECOND,VOICES
from music_owner_store import MusicOwnerStore,parse_state
from owner_broadcast import OwnerBroadcast
from private_songs import PrivateSongs,verify_song_file
from tests.test_music_owner import SavedClient

def wav(seconds=1):
    f=io.BytesIO()
    with wave.open(f,'wb') as w:
        w.setparams((2,2,48000,0,'NONE','not compressed'))
        w.writeframes(struct.pack('<hh',2000,-2000)*round(seconds*48000))
    return f.getvalue()
class AudioRecording(unittest.IsolatedAsyncioTestCase):
    async def test_real_voice_decode_and_mp3_roundtrip(self):
        pcm=await decode_recording(wav())
        result=await decode_recording(await encode_recording(pcm))
        self.assertGreater(len(result),BYTES_PER_SECOND*.9)
        self.assertLess(len(result),BYTES_PER_SECOND*1.1)
    async def test_invalid_remote_playlist_and_oversize_rejected(self):
        for value in [b'bad',b'#EXTM3U\nhttp://127.0.0.1/a.mp3',bytes(MAX_UPLOAD+1)]:
            with self.assertRaises(ValueError):await decode_recording(value)
    async def test_long_voice_is_rejected_without_truncating(self):
        # A tiny compressed file represents over forty seconds.
        encoded=await encode_recording(bytes(BYTES_PER_SECOND*41))
        with self.assertRaises(ValueError):await decode_recording(encoded)
    async def test_file_size_and_truncated_download_checked(self):
        class Bot:
            async def stream_media(self,*args,**kwargs):yield b'123'
        with self.assertRaises(ValueError):await fetch_bytes(Bot(),'file',4)
        with self.assertRaises(ValueError):await fetch_bytes(Bot(),'file',MAX_UPLOAD+1)
    async def test_recorded_party_keeps_normal_name_tts(self):
        store=N(ready=True,data={'voice':'male','audio':{'party':{'file_id':'clip'}},'audio_modes':{'party':'clip'}},
                text=lambda key,name:'Hello '+name)
        w=WelcomeAudio(store);w.recordings=N(get=AsyncMock(return_value=b'clip'))
        w.pcm=AsyncMock(return_value=b'TTS')
        self.assertEqual(await w.speech('ခန့်','vc','normal',VOICES['male']),b'TTS')
        self.assertIn('ခန့်',w.pcm.call_args.args[0])
        self.assertEqual(await w.speech('','vc','party',VOICES['male']),b'clip')
    async def test_hybrid_speaks_real_name_before_file(self):
        store=N(ready=True,data={'voice':'male','audio':{'vc':{'file_id':'clip'}},'audio_modes':{'vc':'name_clip'}})
        w=WelcomeAudio(store);w.recordings=N(get=AsyncMock(return_value=b'clip'));w.pcm=AsyncMock(return_value=b'name')
        out=await w.speech('ခန့်','vc','normal',VOICES['male'])
        self.assertTrue(out.startswith(b'name') and out.endswith(b'clip'))
        self.assertIn('ခန့်',w.pcm.call_args.args[0])
    async def test_missing_recording_falls_back_to_saved_text(self):
        store=N(ready=True,data={'voice':'male','audio':{'party':{'file_id':'bad'}},'audio_modes':{'party':'clip'}},text=lambda *a:'Fallback')
        w=WelcomeAudio(store);w.recordings=N(get=AsyncMock(side_effect=ValueError));w.pcm=AsyncMock(return_value=b'TTS')
        self.assertEqual(await w.speech('','vc','party',VOICES['male']),b'TTS')
    async def test_recording_cache_is_bounded(self):
        c=RecordingCache(None)
        for i in range(10):c.put(str(i),bytes(1024*1024))
        self.assertLessEqual(sum(map(len,c.items.values())),8*1024*1024)

class ExtendedPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_modes_recording_and_broadcast_survive_restart(self):
        client=SavedClient();s=MusicOwnerStore(client,'x'*32);await s.load();await s.claim(7,'x'*32)
        await s.set_recording(7,'party','real-bot-file-id',12.0)
        await s.set_broadcast(7,80)
        restart=MusicOwnerStore(client);await restart.load()
        self.assertTrue(restart.authorized(7));self.assertEqual(restart.data['audio_modes']['party'],'clip')
        self.assertEqual(restart.data['broadcast']['message_id'],80)
        with self.assertRaises(ValueError):await s.set_audio_mode(7,'party','name_clip')
        with self.assertRaises(PermissionError):await s.set_broadcast(8,90)
        await s.set_audio_mode(7,'party','tts');await s.remove_recording(7,'party')
        self.assertNotIn('party',s.data['audio'])
        await s.close()
    def test_older_settings_migrate_without_losing_owner(self):
        d=parse_state(json.dumps({'v':1,'owner':7,'texts':{},'groups':{},'voice':'male'}).encode())
        self.assertEqual(d['owner'],7);self.assertEqual(d['audio'],{});self.assertIsNone(d['broadcast'])

class BroadcastChecks(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        store=N(data={'groups':{'-1001':'A','-1002':'B'},'broadcast':{'chat_id':7,'message_id':8}},observed={},authorized=lambda u:u==7)
        bot=N(copy_message=AsyncMock())
        ui=N(bot=bot,store=store,pending={},record_pending={},panel=42,busy=False)
        return OwnerBroadcast(ui)
    async def test_group_selection_never_targets_unknown_chat(self):
        b=self.manager();b.selection={-1002,99,-999}
        self.assertEqual(b.targets(),[-1002])
    async def test_worker_copies_once_per_target_and_counts_errors(self):
        b=self.manager();b.bot.copy_message.side_effect=[None,RuntimeError()]
        await b.send_job(7,{'chat_id':7,'message_id':8},[-1001,-1002])
        self.assertEqual(b.counts,(1,1,0))
        self.assertEqual([c.args[0] for c in b.bot.copy_message.call_args_list],[-1001,-1002])
    async def test_cancel_prevents_remaining_copies(self):
        b=self.manager()
        async def first(*args):b.cancel.set()
        b.bot.copy_message.side_effect=first
        await b.send_job(7,{'chat_id':7,'message_id':8},[-1001,-1002])
        self.assertEqual(b.counts,(1,0,1));self.assertEqual(b.bot.copy_message.await_count,1)
    def test_review_token_is_single_use(self):
        import time
        b=self.manager();b.review={'token':'ok','until':time.monotonic()+30,'targets':[-1001]}
        self.assertIsNotNone(b.take_review('ok'));self.assertIsNone(b.take_review('ok'))
    async def test_unauthorized_or_group_callbacks_cannot_send(self):
        b=self.manager();handlers=[]
        b.bot.on_callback_query=lambda *a,**k:lambda f:handlers.append(f) or f
        b.bot.on_message=lambda *a,**k:lambda f:handlers.append(f) or f
        b.register();callback=next(f for f in handlers if f.__name__=='broadcast_callback')
        for user,chat in [(8,8),(7,-1001)]:
            cb=N(from_user=N(id=user),message=N(id=42,chat=N(id=chat)),data='bc:confirm:ok',answer=AsyncMock())
            await callback(None,cb)
        b.bot.copy_message.assert_not_awaited()

class PrivateChecks(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        handlers=[];bot=N()
        bot.on_message=lambda *a,**k:lambda f:handlers.append(f) or f
        bot.on_callback_query=lambda *a,**k:lambda f:handlers.append(f) or f
        m=PrivateSongs(bot,AsyncMock(),{},lambda p:None);m.register()
        return m,next(f for f in handlers if f.__name__=='private_song')
    async def test_voice_group_priority_preempts_lookup(self):
        m,handler=self.manager();started=asyncio.Event()
        async def resolve(*args):started.set();await asyncio.Event().wait()
        m.resolver=resolve
        panel=N(id=4,edit_text=AsyncMock(),delete=AsyncMock())
        msg=N(text='/play Purple',from_user=N(id=7),chat=N(id=7),id=3,reply_text=AsyncMock(return_value=panel))
        t=asyncio.create_task(handler(None,msg));await started.wait();m.preempt();await t
        self.assertIsNone(m.job);self.assertIn('Group VC',panel.edit_text.call_args.args[0])
    async def test_concurrent_private_requests_reserve_one_job(self):
        m,handler=self.manager();gate=asyncio.Event();entered=asyncio.Event()
        panel=N(id=4,edit_text=AsyncMock(),delete=AsyncMock())
        async def progress(*a,**k):entered.set();await gate.wait();return panel
        one=N(text='Purple',from_user=N(id=7),chat=N(id=7),id=3,reply_text=progress)
        two=N(text='Other',from_user=N(id=8),chat=N(id=8),id=5,reply_text=AsyncMock())
        task=asyncio.create_task(handler(None,one));await entered.wait()
        await handler(None,two);self.assertIn('ခဏနေ',two.reply_text.call_args.args[0])
        m.preempt();await task;self.assertIsNone(m.job)
    async def test_real_song_file_duration_verified(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'song.mp3';p.write_bytes(await encode_recording(await decode_recording(wav())))
            self.assertGreater(await verify_song_file(p),.9)

    async def test_private_delivery_uses_own_audio_and_cleans_source(self):
        m,handler=self.manager();m.cleanup=__import__('unittest').mock.Mock()
        m.resolver=AsyncMock(return_value=('/tmp/source.mp3','Purple',165))
        panel=N(id=4,edit_text=AsyncMock(),delete=AsyncMock())
        msg=N(text='/play Purple',from_user=N(id=7),chat=N(id=7),id=3,reply_text=AsyncMock(return_value=panel))
        audio=N(file_id='this-bot-file',duration=165)
        with patch('private_songs.verify_song_file',AsyncMock(return_value=165)),patch('private_songs.send_recognized_audio',AsyncMock(return_value=N(audio=audio))) as send:
            await handler(None,msg)
        self.assertEqual(send.call_args.args[2],'/tmp/source.mp3')
        self.assertEqual(m.cache.get('purple')[1],'this-bot-file')
        m.cleanup.assert_called_once_with('/tmp/source.mp3');self.assertIsNone(m.job)
