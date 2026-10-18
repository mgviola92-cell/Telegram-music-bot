import asyncio
import json
import signal
import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

import main
from audio_options import AudioSettings
from effect_exports import EffectExports, ExportJob, PlaybackPriority, export_command, export_key
from music_owner_store import MusicOwnerStore, parse_state
from music_owner_ui import MusicOwnerUI
from test_music_owner import SavedClient
from test_music_extensions import closure


class ExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=SavedClient();self.store=MusicOwnerStore(self.client,'x'*32)
        await self.store.load();await self.store.claim(7,'x'*32)
        await self.store.set_export_user(7,8,True)
        self.temp=tempfile.TemporaryDirectory()
        self.input=Path(self.temp.name)/'song.mp3';self.input.write_bytes(b'source')
        self.song=main.Track('Song – Artist · Bot','song',duration_seconds=120,original_duration_seconds=120)
        self.player=main.Player(current=self.song,temp_file=str(self.input),panel_id=42,
                                playing_settings=AudioSettings(effect='echo'))
        self.bot=N(send_audio=AsyncMock(return_value=N(audio=N(file_id='our-telegram-file'))),send_message=AsyncMock())
        self.exports=EffectExports(self.bot,self.store,{-100:self.player})
        self.exports._wait_for_slot=AsyncMock()
        async def encode(job,out):out.write_bytes(b'mp3')
        self.exports._encode=AsyncMock(side_effect=encode)
    async def asyncTearDown(self):
        await self.exports.close();await self.store.close();self.temp.cleanup()
    def cb(self,user=8,chat=-100,panel=42):
        return N(from_user=N(id=user),message=N(id=panel,chat=N(id=chat)),answer=AsyncMock())
    async def finish(self):
        await asyncio.gather(*list(self.exports.tasks))

    async def test_unauthorized_never_stages_encodes_or_sends_even_cached_file(self):
        key=export_key(self.song,self.player.playing_settings)
        await self.store.cache_export(key,{'file_id':'cached','title':'Song','duration':120})
        with patch('effect_exports.os.link') as link:
            await self.exports.request(self.cb(99),self.player)
        link.assert_not_called();self.exports._encode.assert_not_awaited();self.bot.send_audio.assert_not_awaited()
        self.assertFalse(self.exports.jobs)

    async def test_group_delivery_uses_current_effect_and_keeps_playback_file(self):
        self.player.audio_settings=AudioSettings(effect='tiny')
        await self.exports.request(self.cb(),self.player)
        await self.finish()
        self.assertEqual(self.input.read_bytes(),b'source')
        self.assertEqual(self.exports._encode.call_args.args[0].settings.effect,'echo')
        args,kw=self.bot.send_audio.call_args
        self.assertEqual(args[0],-100)
        self.assertIn('96 kbps',kw['caption']);self.assertIn('Echo',kw['caption'])
        self.assertIn('echo 96k.mp3',kw['file_name'])
        self.assertEqual(len(self.store.data['effect_exports']),1)
        self.assertFalse(self.exports.jobs)
        self.assertFalse(Path(args[1]).exists())

    async def test_telegram_file_id_reuse_skips_cpu_and_temporary_file(self):
        key=export_key(self.song,self.player.playing_settings)
        await self.store.cache_export(key,{'file_id':'cached','title':'Song','duration':120})
        self.input.unlink()
        with patch('effect_exports.os.link') as link:
            await self.exports.request(self.cb(),self.player);await self.finish()
        self.bot.send_audio.assert_awaited_once()
        self.assertEqual(self.bot.send_audio.call_args.args,(-100,'cached'))
        self.exports._encode.assert_not_awaited();link.assert_not_called()
        self.exports._wait_for_slot.assert_not_awaited()

    async def test_pin_survives_skip_deleting_source_then_only_export_pin_is_cleaned(self):
        async def encode(job,out):
            self.input.unlink()
            self.assertEqual(Path(job.source).read_bytes(),b'source')
            out.write_bytes(b'export')
        self.exports._encode.side_effect=encode
        await self.exports.request(self.cb(),self.player)
        folder=next(iter(self.exports.jobs.values())).folder
        await self.finish()
        self.assertFalse(folder.exists())
        self.bot.send_audio.assert_awaited_once()

    async def test_request_rejects_private_stale_normal_video_and_unbounded_sources(self):
        for cb in [self.cb(chat=8),self.cb(panel=1)]:await self.exports.request(cb,self.player)
        self.player.playing_settings=AudioSettings()
        await self.exports.request(self.cb(),self.player)
        self.player.playing_settings=AudioSettings(effect='echo');self.song.video=True
        await self.exports.request(self.cb(),self.player)
        self.song.video=False;self.song.original_duration_seconds=601
        await self.exports.request(self.cb(),self.player)
        self.exports._encode.assert_not_awaited();self.assertFalse(self.exports.jobs)

    async def test_queue_cap_and_requester_cooldown_prevent_export_spam(self):
        gate=asyncio.Event()
        async def encode(job,out):await gate.wait();out.write_bytes(b'file')
        self.exports._encode.side_effect=encode
        await self.store.set_export_user(7,9,True)
        await self.exports.request(self.cb(8),self.player)
        await self.exports.request(self.cb(8),self.player)
        await self.exports.request(self.cb(9),self.player)
        await self.exports.request(self.cb(7),self.player)
        self.assertEqual(len(self.exports.jobs),2)
        gate.set();await self.finish()
        # Identical second pending export reuses the first file_id.
        self.exports._encode.assert_awaited_once()
        self.assertEqual(self.bot.send_audio.await_count,2)

    async def test_revoke_before_work_sends_no_file_and_cleans_pin(self):
        await self.exports.lock.acquire()
        await self.exports.request(self.cb(),self.player)
        folder=next(iter(self.exports.jobs.values())).folder
        await self.store.set_export_user(7,8,False)
        self.exports.lock.release();await self.finish()
        self.bot.send_audio.assert_not_awaited();self.assertFalse(folder.exists())

    async def test_revoke_after_encoding_prevents_upload(self):
        async def encode(job,out):
            out.write_bytes(b'export')
            await self.store.set_export_user(7,8,False)
        self.exports._encode.side_effect=encode
        await self.exports.request(self.cb(),self.player);await self.finish()
        self.bot.send_audio.assert_not_awaited()

    async def test_shutdown_cancels_export_and_preserves_playback(self):
        begun=asyncio.Event()
        async def encode(*args):begun.set();await asyncio.Event().wait()
        self.exports._encode.side_effect=encode
        await self.exports.request(self.cb(),self.player);await begun.wait()
        folder=next(iter(self.exports.jobs.values())).folder
        await self.exports.close()
        self.assertFalse(folder.exists());self.assertTrue(self.input.exists());self.assertFalse(self.exports.jobs)

    async def test_transient_cached_delivery_failure_keeps_reusable_file_id(self):
        key=export_key(self.song,self.player.playing_settings)
        await self.store.cache_export(key,{'file_id':'cached','title':'Song','duration':120})
        self.bot.send_audio.side_effect=TimeoutError()
        await self.exports.request(self.cb(),self.player);await self.finish()
        self.assertIn(key,self.store.data['effect_exports'])
        self.exports._encode.assert_not_awaited()

    async def test_busy_gate_runs_without_holding_player_lock(self):
        async def wait(job,*args,**kw):self.assertFalse(self.player.lock.locked())
        self.exports._wait_for_slot.side_effect=wait
        await self.exports.request(self.cb(),self.player);await self.finish()
        self.assertEqual(self.exports._wait_for_slot.await_count,2)
        self.assertIs(self.player.current,self.song)

    async def test_card_export_branch_precedes_group_admin_check(self):
        cb=self.cb();cb.data='mud:export:-100'
        ns=dict(vars(main));request=AsyncMock()
        ns.update(players={-100:self.player},effect_exports=N(request=request),
                  is_chat_admin=AsyncMock(side_effect=AssertionError('Export permission is not group admin status')))
        await closure('panel_callback',ns)(None,cb)
        request.assert_awaited_once_with(cb,self.player)


class ExportPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_permissions_and_file_ids_survive_restart_without_resetting_other_settings(self):
        client=SavedClient();store=MusicOwnerStore(client,'x'*32);await store.load();await store.claim(7,'x'*32)
        await store.set_text(7,'vc','Hello {name}')
        await store.set_export_user(7,8,True)
        await store.cache_export('a'*64,{'file_id':'cached','title':'Song','duration':120})
        restart=MusicOwnerStore(client);await restart.load()
        self.assertTrue(restart.can_export(7));self.assertTrue(restart.can_export(8))
        self.assertFalse(restart.can_export(9));self.assertEqual(restart.data['texts']['vc'],'Hello {name}')
        self.assertEqual(restart.data['effect_exports']['a'*64]['file_id'],'cached')
        with self.assertRaises(PermissionError):await store.set_export_user(8,9,True)
        await store.set_export_user(7,8,False);self.assertFalse(store.can_export(8))
        await store.close();await restart.close()

    async def test_persisted_export_cache_is_bounded_metadata_only(self):
        client=SavedClient();store=MusicOwnerStore(client,'x'*32);await store.load();await store.claim(7,'x'*32)
        for i in range(26):
            await store.cache_export(f'{i:064x}',{'file_id':f'file-{i}','title':'Song','duration':120})
        self.assertEqual(len(store.data['effect_exports']),24)
        self.assertNotIn('0'*64,store.data['effect_exports'])
        self.assertLess(len(client.raw),16000)
        await store.close()

    def test_old_settings_are_compatible_and_invalid_permission_cache_rows_fail_closed(self):
        raw={'v':1,'owner':7,'texts':{},'groups':{}}
        self.assertEqual(parse_state(json.dumps(raw).encode())['effect_export_users'],[])
        for bad in [[True],[-100],[8,8],['8']]:
            with self.assertRaises(ValueError):parse_state(json.dumps({**raw,'effect_export_users':bad}).encode())
        with self.assertRaises(ValueError):parse_state(json.dumps({**raw,'effect_exports':{'a'*64:{'file_id':'x','title':'S','duration':float('nan')}}}).encode())

    async def test_owner_private_id_add_remove_flow(self):
        client=SavedClient();store=MusicOwnerStore(client,'x'*32);await store.load();await store.claim(7,'x'*32)
        handlers=[]
        bot=N(on_message=lambda *a,**k:lambda f:handlers.append(f) or f,
              on_callback_query=lambda *a,**k:lambda f:handlers.append(f) or f,send_message=AsyncMock())
        ui=MusicOwnerUI(bot,store,N(),{});ui.register();ui.panel=42
        callback=next(f for f in handlers if f.__name__=='owner_callback')
        handler=next(f for f in handlers if f.__name__=='owner_text')
        panel=N(id=42,chat=N(id=7),edit_text=AsyncMock())
        class Stop(Exception):pass
        def stop():raise Stop()
        for action,expected in [('export-add',True),('export-remove',False)]:
            await callback(None,N(from_user=N(id=7),message=panel,data='own:'+action,answer=AsyncMock()))
            msg=N(from_user=N(id=7),text='123456789',reply_text=AsyncMock(),stop_propagation=stop)
            with self.assertRaises(Stop):await handler(None,msg)
            self.assertEqual(store.can_export(123456789),expected)
        _,keys=ui.menu('exports')
        self.assertTrue(any(b.callback_data=='own:home' for row in keys.inline_keyboard for b in row))
        await store.close()


class ExportCpuAndCodecTests(unittest.IsolatedAsyncioTestCase):
    def test_priority_defers_for_transition_multiple_groups_and_cpu_pressure(self):
        player=main.Player(current=main.Track('S','s'));priority=PlaybackPriority({1:player})
        player.skip_pending=True;self.assertTrue(priority.busy());player.skip_pending=False
        priority.players={i:main.Player(current=main.Track('S','s')) for i in range(3)}
        self.assertTrue(priority.busy());priority.players={1:player}
        with patch('effect_exports.cpu_sample',side_effect=[(1,1),(1.9,1)]),patch('effect_exports.memory_sample',return_value=(1,100)),patch('effect_exports.time.monotonic',side_effect=[0,1]):
            self.assertFalse(priority.busy());self.assertTrue(priority.busy())

    async def test_running_encoder_yields_cpu_on_transition_and_resumes(self):
        done=asyncio.Event();signals=[]
        class Process:
            returncode=None
            async def wait(self):await done.wait();self.returncode=0;return 0
            def send_signal(self,sig):
                signals.append(sig)
                if sig==signal.SIGCONT:done.set()
            def kill(self):self.returncode=-9;done.set()
        exp=EffectExports(N(),N(can_export=lambda u:True),{})
        exp.priority=N(busy=Mock(side_effect=[True,False]))
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'out.mp3';out.write_bytes(b'mp3')
            job=ExportJob('a',-100,7,'Song',AudioSettings(effect='echo'),1,source='/tmp/in.mp3')
            with patch('effect_exports.asyncio.create_subprocess_exec',AsyncMock(return_value=Process())):
                await asyncio.wait_for(exp._encode(job,out),2)
        self.assertEqual(signals,[signal.SIGSTOP,signal.SIGCONT])

    async def test_real_96k_mp3_effect_output_decodes_and_changes_speed(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.wav'
            with wave.open(str(source),'wb') as audio:
                audio.setparams((2,2,48000,0,'NONE','not compressed'))
                import struct,math
                audio.writeframes(b''.join(struct.pack('<hh',int(6000*math.sin(i*2*math.pi*220/48000)),
                                                     int(6000*math.sin(i*2*math.pi*220/48000))) for i in range(48000*2)))
            for quality,effect in [('high','chipmunk'),('medium','echo'),('low','slowed')]:
                output=Path(folder)/(quality+'.mp3');settings=AudioSettings(quality=quality,effect=effect)
                command=export_command(str(source),output,settings,2)
                self.assertIn('96k',command);self.assertIn('1',command)
                subprocess.run(command,check=True,capture_output=True,timeout=15)
                result=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)]))
                stream=result['streams'][0]
                self.assertEqual(int(stream['bit_rate']),96000)
                self.assertEqual(stream['codec_name'],'mp3')
                expected=2/settings.speed
                self.assertLess(abs(float(result['format']['duration'])-expected),.5)
                pcm=subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-f','s16le','pipe:1'])
                self.assertGreater(len(pcm),10000)

    async def test_real_hls_export_keeps_headers_and_finishes_at_eof(self):
        from aiohttp import web
        with tempfile.TemporaryDirectory() as folder:
            playlist=Path(folder)/'song.m3u8';output=Path(folder)/'export.mp3'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=2',
                            '-c:a','aac','-f','hls','-hls_time','1',str(playlist)],check=True,capture_output=True)
            seen=[]
            async def serve(request):
                seen.append(request.headers.get('User-Agent'))
                return web.FileResponse(Path(folder)/request.match_info['name'])
            app=web.Application();app.router.add_get('/{name}',serve)
            runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            port=site._server.sockets[0].getsockname()[1]
            exp=EffectExports(N(),N(can_export=lambda user:True),{})
            exp.priority=N(busy=lambda:False)
            job=ExportJob('k',-100,7,'Song',AudioSettings(effect='echo'),2,source=f'http://127.0.0.1:{port}/song.m3u8')
            try:
                with patch('effect_exports.allowed_audio_cdn',return_value=True),patch('effect_exports.stream_headers_for',return_value={'User-Agent':'ExportTest'}):
                    await asyncio.wait_for(exp._encode(job,output),8)
                self.assertTrue(seen);self.assertEqual(set(seen),{'ExportTest'})
                self.assertGreater(output.stat().st_size,1000)
            finally:await runner.cleanup()

    def test_only_public_audio_cdn_urls_and_safe_explicit_format_are_exported(self):
        for url in ['https://127.0.0.1/a','http://soundcloud.com/a','https://evil.example/a']:
            with self.assertRaises(ValueError):export_command(url,'/tmp/out.mp3',AudioSettings(effect='echo'),120)
        command=export_command('/tmp/song.audio','/tmp/out.mp3',AudioSettings(effect='echo'),120)
        self.assertEqual(command[command.index('-protocol_whitelist')+1],'file,pipe')
        self.assertIn('-vn',command);self.assertIn('96k',command)
