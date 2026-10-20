import ast
import asyncio
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

import main
from audio_options import AudioSettings, apply_filters, ProcessedAudioStream, EFFECTS
from music_search import (SearchChoice, SearchSessions, collect_choices, choose_related,
                          youtube_id, youtube_title, related_query, title_key)
from music_stats import MusicStats
from music_cleanup import sweep


def closure(name, namespace):
    tree = ast.parse(Path(main.__file__).read_text())
    outer = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'main')
    node = next(n for n in outer.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node],type_ignores=[]), '<real-handler>', 'exec'), namespace)
    return namespace[name]


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def test_burmese_vowels_do_not_collapse_distinct_song_titles(self):
        self.assertNotEqual(title_key('ကာ'),title_key('ကိ'))

    def test_youtube_id_validation_prevents_other_hosts_and_credentials(self):
        for link in ('https://youtu.be/abcdefghijk','https://www.youtube.com/watch?v=abcdefghijk',
                     'https://youtube.com/shorts/abcdefghijk','https://music.youtube.com/watch?v=abcdefghijk'):
            self.assertEqual(youtube_id(link),'abcdefghijk')
        for link in ('https://youtube.com.evil.test/watch?v=abcdefghijk','https://user@youtube.com/watch?v=abcdefghijk',
                     'https://youtube.com/playlist?list=abc','http://127.0.0.1/watch?v=abcdefghijk'):
            self.assertIsNone(youtube_id(link))

    async def test_all_sources_metadata_only_dedup_and_five_results(self):
        choices=[SearchChoice('Song '+str(i), 'https://soundcloud.com/a/'+str(i),100) for i in range(6)]
        candidate=N(label='Song 0 · Bot',duration=100)
        source=N(find=AsyncMock(return_value=candidate),download=AsyncMock())
        with patch('music_search.soundcloud_choices',return_value=choices):
            result=await collect_choices('Song',[source])
        self.assertEqual(len(result),5)
        self.assertEqual(len({title_key(c.title) for c in result}),5)
        source.find.assert_awaited_once()
        source.download.assert_not_awaited()

    async def test_slow_source_canceled_without_waiting(self):
        canceled=[]
        async def slow(q):
            try:await asyncio.Event().wait()
            finally:canceled.append(True)
        source=N(find=slow)
        with patch('music_search.soundcloud_choices',return_value=[SearchChoice(str(i),'query') for i in range(5)]):
            result=await asyncio.wait_for(collect_choices('Song',[source]),1)
        self.assertEqual(len(result),5)
        self.assertTrue(canceled)

    def test_sessions_reject_other_user_chat_expired_and_duplicate_click(self):
        sessions=SearchSessions();choice=SearchChoice('Purple','Purple')
        sessions.put('token',-100,7,[choice])
        self.assertIsNone(sessions.select('token',0,-101,7))
        self.assertIsNone(sessions.select('token',0,-100,8))
        self.assertIs(sessions.select('token',0,-100,7),choice)
        self.assertIsNone(sessions.select('token',0,-100,7))
        with patch('music_search.time.monotonic',return_value=0):sessions.put('expired',-100,7,[choice])
        with patch('music_search.time.monotonic',return_value=181):
            self.assertIsNone(sessions.select('expired',0,-100,7))

    def test_related_tracks_avoid_recent_titles(self):
        choices=[SearchChoice('Purple – Wink','purple'),SearchChoice('Other – Wink','other')]
        self.assertEqual(related_query('Purple – Wink · SoundCloud'),'Wink')
        self.assertIs(choose_related(choices,[title_key('Purple – Wink')]),choices[1])
        self.assertIsNone(choose_related(choices,[title_key(c.title) for c in choices]))

    async def test_youtube_oembed_is_bounded_and_cached(self):
        import music_search
        music_search._YOUTUBE_TITLES.clear()
        response=N(status=200, content=N(read=AsyncMock(return_value=b'{"title":"Faded (Official Video)"}')))
        cm=N(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
        # MagicMock provides Python context-manager special methods.
        from unittest.mock import MagicMock
        request=MagicMock();request.__aenter__=AsyncMock(return_value=response)
        request.__aexit__=AsyncMock(return_value=False)
        session=MagicMock();session.get.return_value=request
        client=MagicMock();client.__aenter__=AsyncMock(return_value=session)
        client.__aexit__=AsyncMock(return_value=False)
        with patch('music_search.ClientSession',return_value=client):
            title=await youtube_title('https://youtu.be/abcdefghijk')
            self.assertEqual(await youtube_title('https://youtube.com/watch?v=abcdefghijk'),title)
        self.assertEqual(session.get.call_count,1)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    async def test_pick_plays_exact_candidate(self):
        choice=SearchChoice('Purple','original query',160,candidate=object())
        sessions=SearchSessions();sessions.put('a'*16,-100,7,[choice])
        play=AsyncMock()
        ns=dict(vars(main));ns.update(search_sessions=sessions,play_command=play,bot=object(),
                                     choice_track=lambda c: main.Track(c.title,c.source,youtube=True,selected=c.candidate))
        handler=closure('search_pick',ns)
        message=N(chat=N(id=-100),edit_reply_markup=AsyncMock(),edit_text=AsyncMock())
        cb=N(data='songpick:'+'a'*16+':0',message=message,from_user=N(id=7),answer=AsyncMock())
        await handler(None,cb)
        track=play.call_args.args[1].selected_track
        self.assertIs(track.selected,choice.candidate)
        self.assertEqual(track.source,'original query')

    async def test_youtube_link_maps_title_without_audio_extraction(self):
        player=main.Player()
        progress=N(edit_text=AsyncMock())
        msg=N(command=['play','https://youtu.be/abcdefghijk'],text='/play https://youtu.be/abcdefghijk',
              reply_to_message=None,chat=N(id=-100),reply_text=AsyncMock(return_value=progress))
        # Stop after assistant join to inspect mapped track without running a VC.
        ns=dict(vars(main));ns.update(private_songs=N(preempt=lambda:None),players={-100:player},youtube_audio_enabled=lambda:False,youtube_relay_enabled=lambda:False,
            youtube_title=AsyncMock(return_value='Faded – Alan Walker'),allowed_hosts=set(),
            resolve_for_queue=object(),start_autoplay=Mock(),ensure_assistant_in_group=AsyncMock(side_effect=RuntimeError('test gate')))
        handler=closure('play_command',ns)
        await handler(None,msg)
        ns['youtube_title'].assert_awaited_once_with('https://youtu.be/abcdefghijk')
        # Single progress message is reused, not a second acknowledgement.
        self.assertEqual(msg.reply_text.await_count,1)


class AudioFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_default_filters_leave_pcm_command_byte_for_byte_unchanged(self):
        cmd=['ffmpeg','-i','url','-f','s16le','-ar','48000','-ac','2','pipe:1']
        self.assertEqual(apply_filters(cmd,AudioSettings()),cmd)
        with self.assertRaises(ValueError):AudioSettings(effect='arbitrary ffmpeg args')

    async def test_direct_filtered_stream_retains_headers_eof_and_network_input(self):
        from network_audio import DirectAudioStream
        from pytgcalls.types import MediaStream
        with patch('network_audio.stream_headers_for', return_value={'User-Agent':'Test Client','Referer':'https://soundcloud.com/'}):
            stream=DirectAudioStream('https://cf-hls-media.sndcdn.com/test.m3u8',AudioSettings(normalize=True,effect='bassboost'))
        async def checked(obj):
            obj.microphone.path=shlex.join(['ffmpeg','-headers','old','-reconnect_at_eof','1','-i','https://cf-hls-media.sndcdn.com/test.m3u8','-f','s16le','pipe:1'])
        with patch.object(MediaStream,'check_stream',checked):await stream.check_stream()
        cmd=shlex.split(stream.microphone.path)
        self.assertEqual(cmd.count('-headers'),1)
        self.assertEqual(cmd[cmd.index('-reconnect_at_eof')+1],'0')
        self.assertTrue(cmd[cmd.index('-i')+1].startswith('https://'))
        self.assertIn('loudnorm',cmd[cmd.index('-af')+1])
        self.assertIn('bass=g=6',cmd[cmd.index('-af')+1])

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_all_quality_effect_normalization_combinations_decode_real_pcm(self):
        for quality in ('high','medium','low'):
            for effect in EFFECTS:
                for normalize in (False,True):
                    with self.subTest(quality=quality,effect=effect,normalize=normalize):
                        settings=AudioSettings(quality,normalize,effect)
                        rate,channels=settings.parameters.value
                        cmd=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-ar',str(rate),'-ac',str(channels),'-f','s16le','pipe:1']
                        result=subprocess.run(apply_filters(cmd,settings),capture_output=True,timeout=5)
                        self.assertEqual(result.returncode,0,result.stderr.decode())
                        self.assertGreater(len(result.stdout),1000)

    async def test_audio_settings_do_not_restart_current_stream(self):
        player=main.Player(current=main.Track('Now','query'))
        calls=Mock()
        player.panel_id=4
        cb=N(data='mud:qlow:-100',message=N(id=4,chat=N(id=-100)),from_user=N(id=7),answer=AsyncMock())
        ns=dict(vars(main));ns.update(players={-100:player},is_chat_admin=AsyncMock(return_value=True),calls=calls,
                                     update_player_panel=AsyncMock())
        await closure('panel_callback',ns)(None,cb)
        self.assertEqual(player.audio_settings.quality,'low')
        self.assertEqual(player.playing_settings,AudioSettings())
        calls.assert_not_called()


class CleanupAndStatsTests(unittest.TestCase):
    def test_cleanup_preserves_active_prefetched_fresh_unrelated_and_symlink_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def make(name):
                p=root/name;p.mkdir();(p/'audio.mp3').write_bytes(b'audio')
                os.utime(p/'audio.mp3',(1,1));os.utime(p,(1,1));return p
            active=make('mudyar_media_active');prepared=make('mudyar_media_prepared')
            orphan=make('mudyar_media_orphan');unrelated=make('user_files')
            fresh=make('mudyar_recognize_fresh');os.utime(fresh,(100000,100000))
            (root/'mudyar_media_link').symlink_to(unrelated,target_is_directory=True)
            removed=sweep([str(active/'audio.mp3'),str(prepared/'audio.mp3')],root=root,now=100000,max_age=100)
            self.assertEqual(removed,1)
            self.assertFalse(orphan.exists())
            for p in (active,prepared,unrelated,fresh):self.assertTrue(p.exists())

    def test_stats_persist_in_separate_sqlite_and_are_group_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'stats.sqlite3')
            stats=MusicStats(path)
            stats.record(-100,'Purple · VoiceShazamBot');stats.record(-100,'Purple')
            stats.record(-101,'Other')
            stats.close();stats=MusicStats(path)
            rows,total=stats.snapshot(-100)
            self.assertEqual(total,(2,1))
            self.assertEqual(rows,[('Purple',2)])
            stats.close()


class AutoplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_autoplay_manual_request_cancels_recommendations_and_prefetch(self):
        manual=main.Track('Manual','manual');auto=main.Track('Auto','auto',autoplay=True)
        task=asyncio.create_task(asyncio.sleep(10))
        prepared=N(discard=Mock())
        player=main.Player(queue=deque([auto,manual]),prepared=prepared,autoplay_task=task)
        main.drop_autoplay(player)
        await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(list(player.queue),[manual])
        prepared.discard.assert_called_once()
        self.assertTrue(task.cancelled())

    async def test_recommendation_respects_manual_queue_and_enqueues_only_one(self):
        seed=main.Track('Purple – Wink','Purple')
        player=main.Player(current=seed,autoplay_enabled=True)
        player.recent_tracks.append(title_key(seed.title))
        choice=SearchChoice('Other – Wink','other')
        ns=dict(vars(main));ns.update(search_slots=asyncio.Semaphore(2),lybot=None,
            collect_choices=AsyncMock(return_value=[SearchChoice(seed.title,'same'),choice]),
            choice_track=lambda c,automatic=False:main.Track(c.title,c.source,youtube=True,autoplay=automatic),
            prepare_next_song=Mock())
        start=closure('start_autoplay',ns)
        start(player)
        task=player.autoplay_task
        await task
        self.assertEqual(len(player.queue),1)
        self.assertEqual(player.queue[0].title,choice.title)
        self.assertTrue(player.queue[0].autoplay)
        player.queue.clear()
        gate=asyncio.Event()
        async def delayed(*args,**kwargs):await gate.wait();return [choice]
        ns['collect_choices']=delayed
        start(player);task=player.autoplay_task
        player.queue.append(main.Track('Manual','manual'));gate.set();await task
        self.assertEqual([t.title for t in player.queue],['Manual'])

    def test_shortcuts_keep_existing_admin_gates(self):
        source=Path(main.__file__).read_text()
        self.assertIn('filters.command(["play", "p", "vplay"])',source)
        self.assertIn('filters.command(["skip", "s", "next"])',source)
        self.assertIn('filters.command(["clear", "c"])',source)
        tree=ast.parse(source)
        for name in ('skip_command','clear_command'):
            node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            self.assertIn('require_admin',ast.unparse(node))


class EffectTimingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_nightcore_speed_is_consistent_for_44k_and_48k_sources(self):
        for input_rate in (44100,48000):
            settings=AudioSettings(effect='nightcore')
            command=['ffmpeg','-v','error','-f','lavfi','-i',f'sine=frequency=440:sample_rate={input_rate}:duration=1',
                     '-ar','48000','-ac','2','-f','s16le','pipe:1']
            result=subprocess.run(apply_filters(command,settings),capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0)
            seconds=len(result.stdout)/(48000*2*2)
            self.assertAlmostEqual(seconds,1/1.15,delta=.03)


class DirectPathIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_optional_filters_keep_remote_url_without_download_fallback(self):
        for settings in (AudioSettings(), AudioSettings('low',True,'nightcore')):
            with self.subTest(settings=settings):
                url='https://playback.media-streaming.soundcloud.cloud/example.m3u8'
                track=main.Track('Example','Example',youtube=True)
                player=main.Player(queue=deque([track]),audio_settings=settings)
                loader=Mock(return_value=(url,'Example · SoundCloud',180))
                factory=Mock(side_effect=main.audio_stream)
                ns=dict(vars(main));ns.update(players={-100:player},lybot=None,
                    fresh_voice_chats={-100},download_song=loader,audio_stream=factory,
                    play_with_reconnect=AsyncMock(),wait_for_audio_frames=AsyncMock(),calls=Mock(),
                    clean_temp=Mock(),prepare_next_song=Mock(),announce_now_playing=AsyncMock(),
                    record_play=AsyncMock())
                await closure('advance',ns)(-100)
                await player.announce_task
                loader.assert_called_once()
                self.assertTrue(loader.call_args.args[-1])
                self.assertIs(player.current,track)
                self.assertEqual(factory.call_args.args[0],url)
                self.assertEqual(player.temp_file,url)
                ns['wait_for_audio_frames'].assert_awaited_once()
                self.assertEqual(player.playing_settings,settings)

    async def test_manual_request_canceling_autoplay_does_not_cancel_queue_loader(self):
        player=main.Player(current=main.Track('Old','Old'),autoplay_enabled=True)
        url='https://playback.media-streaming.soundcloud.cloud/example.m3u8'
        manual=main.Track('Manual','Manual',youtube=True)
        task=asyncio.create_task(asyncio.sleep(100))
        player.autoplay_task=task
        started=asyncio.Event()
        def start(p):started.set()
        ns=dict(vars(main));ns.update(players={-100:player},lybot=None,start_autoplay=start,
            fresh_voice_chats={-100},download_song=Mock(return_value=(url,'Manual',120)),
            audio_stream=Mock(return_value=object()),play_with_reconnect=AsyncMock(),
            wait_for_audio_frames=AsyncMock(),calls=Mock(),clean_temp=Mock(),prepare_next_song=Mock(),
            announce_now_playing=AsyncMock(),record_play=AsyncMock())
        loader=asyncio.create_task(closure('advance',ns)(-100))
        await started.wait()
        main.drop_autoplay(player)
        player.queue.append(manual)
        await loader
        await player.announce_task
        self.assertIs(player.current,manual)
        self.assertFalse(loader.cancelled())


class ExactSelectionDirectTests(unittest.TestCase):
    def test_selected_soundcloud_url_skips_search_and_name_heuristic(self):
        url='https://soundcloud.com/artist/selected-remix'
        result=('https://playback.media-streaming.soundcloud.cloud/selected.m3u8','Actual Remix',180)
        with patch('main.soundcloud_results') as search, patch('main.resolve_soundcloud_https',return_value=result) as direct, \
             patch('main.get_cached_song',return_value=None), patch.dict(os.environ,{'MUSIC_STREAM_ONLY':'0'}):
            self.assertEqual(main.download_song(url,False,prefer_stream=True),result)
        search.assert_not_called()
        self.assertIsNone(direct.call_args.kwargs['check'])

    def test_strict_mode_selected_url_also_does_not_search_or_download(self):
        url='https://soundcloud.com/artist/selected'
        result=('https://playback.media-streaming.soundcloud.cloud/selected.m3u8','Selected',180)
        with patch('main.soundcloud_results') as search, patch('main.resolve_soundcloud_https',return_value=result), \
             patch('main.tempfile.mkdtemp') as create:
            self.assertEqual(main.resolve_stream_only(url),result)
        search.assert_not_called()
        create.assert_not_called()


class MaintenanceAndProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_disk_pressure_evicts_cache_copy_but_not_playback_copy(self):
        import song_cache
        with tempfile.TemporaryDirectory() as folder:
            cached=Path(folder)/'cached.mp3';playing=Path(folder)/'playing.mp3'
            cached.write_bytes(b'cached');playing.write_bytes(b'playing')
            rows={'key':(cached,'Title',180,time.monotonic()+100)}
            with patch.object(song_cache,'_ITEMS',rows), patch('song_cache.shutil.disk_usage',return_value=N(total=100,used=85,free=15)):
                song_cache.maintain_cache()
            self.assertFalse(cached.exists())
            self.assertTrue(playing.exists())
            self.assertEqual(rows,{})

    async def test_feature_probe_expiration_prevents_later_network_tests(self):
        from feature_probe import run_feature_probe
        with patch('feature_probe.youtube_title',new=AsyncMock()) as yt,patch('feature_probe.collect_choices',new=AsyncMock()) as search:
            await run_feature_probe(0,Mock())
            yt.assert_not_awaited();search.assert_not_awaited()

    async def test_feature_probe_checks_metadata_and_pcm_without_calling_telegram(self):
        from feature_probe import run_feature_probe
        choice=SearchChoice('Purple','https://soundcloud.com/a/b')
        url='https://playback.media-streaming.soundcloud.cloud/song.m3u8'
        resolve=Mock(return_value=(url,'Purple',160))
        with patch('feature_probe.youtube_title',new=AsyncMock(return_value='Faded')) as yt, \
             patch('feature_probe.collect_choices',new=AsyncMock(return_value=[choice])), \
             patch('feature_probe.probe_audio_url',new=AsyncMock(return_value={'ok':True,'pcm_bytes':96000})) as pcm:
            await run_feature_probe(time.time()+30,resolve)
        yt.assert_awaited_once();resolve.assert_called_once_with(choice.source)
        self.assertEqual(pcm.call_args.kwargs['variant'],'hls_no_eof')
