import asyncio
import json
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from lyrics_service import LyricsUI,LyricsLookup,choose_record,query_text
from music_owner_store import MusicOwnerStore,recording_choices,parse_state
from welcome_audio import WelcomeAudio,VOICES
from test_music_owner import SavedClient

class RecordingPoolTests(unittest.IsolatedAsyncioTestCase):
    async def store(self):
        client=SavedClient();s=MusicOwnerStore(client,'x'*32)
        await s.load();await s.claim(7,'x'*32);return s,client
    async def test_pool_persists_and_deleting_one_preserves_other(self):
        s,client=await self.store()
        await s.set_recording(7,'party_batch','admin-one',3)
        await s.set_recording(7,'party_batch','admin-two',4)
        again=MusicOwnerStore(client);await again.load()
        self.assertEqual(len(recording_choices(again.data,'party_batch')),2)
        await again.remove_recording(7,'party_batch','admin-one')
        self.assertEqual(recording_choices(again.data,'party_batch')[0]['file_id'],'admin-two')
        self.assertTrue(again.authorized(7));await s.close();await again.close()
    async def test_pool_limit_and_hybrid_duration_apply_to_every_file(self):
        s,_=await self.store()
        for i in range(10):await s.set_recording(7,'vc',str(i),5)
        await s.set_audio_mode(7,'vc','name_clip')
        with self.assertRaises(ValueError):await s.set_recording(7,'vc','too-many',5)
        await s.remove_recording(7,'vc','0');await s.set_recording(7,'vc','long',35)
        self.assertEqual(s.data['audio_modes']['vc'],'clip')
        with self.assertRaises(ValueError):await s.set_audio_mode(7,'vc','name_clip')
        await s.close()
    async def test_random_choice_never_repeats_and_styles_stay_separate(self):
        data={'voice':'male','audio':{'party':{'file_id':'long','seconds':5},'party_batch':{'file_id':'a','seconds':2,'variants':[{'file_id':'b','seconds':2}]}},'audio_modes':{'party':'clip','party_batch':'clip'}}
        w=WelcomeAudio(N(ready=True,data=data));w.recordings=N(get=AsyncMock(side_effect=lambda f:f.encode()))
        outputs=[await w.speech('','vc','party_batch',VOICES['male'],chat=7) for _ in range(10)]
        self.assertTrue(all(a!=b for a,b in zip(outputs,outputs[1:])))
        self.assertEqual(await w.speech('','vc','party',VOICES['male'],chat=7),b'long')
    async def test_group_arrival_never_uses_party_recordings(self):
        data={'voice':'male','audio':{'party':{'file_id':'party','seconds':5},'group':{'file_id':'group','seconds':5}},'audio_modes':{'party':'clip','group':'clip'}}
        settings=N(ready=True,data=data,text=lambda key,name:key)
        w=WelcomeAudio(settings);w.recordings=N(get=AsyncMock(side_effect=lambda f:f.encode()))
        self.assertEqual(await w.speech('Name','group','party',VOICES['male']),b'group')
        self.assertEqual(w.render('Name','group','party'),'group')
    def test_old_single_recording_is_valid_without_migration_loss(self):
        old={'v':1,'owner':7,'texts':{},'groups':{},'audio':{'vc':{'file_id':'old','seconds':5}},'audio_modes':{'vc':'name_clip'}}
        d=parse_state(json.dumps(old).encode());self.assertEqual(recording_choices(d,'vc')[0]['file_id'],'old')

class LyricsTests(unittest.IsolatedAsyncioTestCase):
    def record(self,**overrides):
        row={'trackName':'Perfect','artistName':'Ed Sheeran','duration':263,'plainLyrics':'sample test text'};row.update(overrides);return row
    def test_burmese_combining_marks_survive_search_query(self):
        self.assertEqual(query_text('မြန်မာ – သီချင်း'),'မြန်မာ သီချင်း')
    def test_matching_rejects_wrong_song_and_duration(self):
        self.assertIsNone(choose_record([self.record(trackName='Hello')],'Perfect Ed Sheeran',263))
        self.assertIsNone(choose_record([self.record(duration=400)],'Perfect Ed Sheeran',263))
        self.assertIsNotNone(choose_record([self.record()],'Perfect Ed Sheeran',263))
    def test_synced_text_and_oversize(self):
        r=choose_record([self.record(plainLyrics=None,syncedLyrics='[00:01.00]test line')],'Perfect Ed Sheeran')
        self.assertEqual(r[0],'test line')
        self.assertIsNone(choose_record([self.record(plainLyrics='x'*32001)],'Perfect Ed Sheeran'))
    def context(self,result=('test text '*400,'Perfect – Ed Sheeran')):
        handlers=[];bot=N(on_callback_query=lambda *a,**k:lambda f:handlers.append(f) or f)
        ui=LyricsUI(bot,lambda chat,panel:('Perfect Ed Sheeran',263));ui.lookup.find=AsyncMock(return_value=result);ui.register()
        panel=N(id=12,edit_text=AsyncMock(),delete=AsyncMock())
        source=N(id=10,chat=N(id=-100),reply_text=AsyncMock(return_value=panel))
        cb=N(data='lyrics:current',message=source,from_user=N(id=7),answer=AsyncMock())
        return ui,handlers[0],cb,panel
    async def test_fetch_only_on_click_pages_edit_same_message(self):
        ui,callback,cb,panel=self.context();ui.lookup.find.assert_not_awaited()
        await callback(None,cb);self.assertEqual(cb.message.reply_text.await_count,1)
        token=next(iter(ui.views));panel.chat=N(id=-100)
        cb.message=panel;cb.data='lyr:page:'+token+':1'
        await callback(None,cb);self.assertIn('2/',panel.edit_text.call_args.args[0]);self.assertEqual(ui.lookup.find.await_count,1)
    async def test_foreign_user_cannot_turn_pages_or_close(self):
        ui,callback,cb,panel=self.context();await callback(None,cb);token=next(iter(ui.views))
        cb.from_user.id=8;cb.message=panel;panel.chat=N(id=-100);cb.data='lyr:close:'+token
        await callback(None,cb);panel.delete.assert_not_awaited()
    async def test_not_found_and_network_error_have_search_fallback(self):
        for fail in (False,True):
            ui,callback,cb,panel=self.context(None)
            if fail:ui.lookup.find.side_effect=TimeoutError()
            await callback(None,cb)
            self.assertTrue(panel.edit_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url)
            self.assertFalse(ui.busy)


class LyricsResponseTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self,chunks):
        class Content:
            async def iter_chunked(self,size):
                for chunk in chunks:yield chunk
        class Response:
            status=200;content_type='application/json';content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        class Session:
            def __init__(self,*args,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def get(self,*args,**kwargs):return Response()
        with patch('lyrics_service.ClientSession',Session):return await LyricsLookup().find('Perfect Ed Sheeran',263)
    async def test_chunked_json_is_read_completely_before_parsing(self):
        payload=json.dumps([{'trackName':'Perfect','artistName':'Ed Sheeran','duration':263,'plainLyrics':'test text'}]).encode()
        result=await self.fetch([payload[:13],payload[13:29],payload[29:]])
        self.assertEqual(result[0],'test text')
    async def test_chunked_body_remains_bounded(self):
        with self.assertRaises(ValueError):await self.fetch([bytes(65536)]*17)
