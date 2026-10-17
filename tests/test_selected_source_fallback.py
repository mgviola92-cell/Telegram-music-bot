import asyncio
import os
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, MagicMock, patch
import main
import music_search
from test_music_extensions import closure


class SelectedSourceFallbackTests(unittest.IsolatedAsyncioTestCase):
    def environment(self,prepared,bridge,strict=False):
        ns=dict(vars(main));ns.update(PreparedAudio=Mock(return_value=prepared),lybot=bridge,
                                     stream_only_enabled=lambda:strict,telegram_lyrics=N(interrupt=Mock()))
        return closure('resolve_for_queue',ns),ns

    async def test_failed_selected_sc_uses_selected_title_on_bot_sources(self):
        song=main.Track('ကောင်းမလေး – Tay Chit Thu','https://soundcloud.com/a/blocked',youtube=True,metadata_resolved=True)
        prepared=N(consume=AsyncMock(side_effect=RuntimeError('SC restricted')))
        result=('bot.mp3','ကောင်းမလေး – Tay Chit Thu · LyBot',245)
        bridge=N(resolve=AsyncMock(return_value=result))
        resolver,ns=self.environment(prepared,bridge)
        self.assertEqual(await resolver(song,True),result)
        bridge.resolve.assert_awaited_once_with('ကောင်းမလေး – Tay Chit Thu')

    async def test_working_selected_direct_url_does_not_trigger_bot_transfer(self):
        song=main.Track('Selected','https://soundcloud.com/a/selected',youtube=True,metadata_resolved=True)
        result=('https://playback.media-streaming.soundcloud.cloud/song.m3u8','Selected',180)
        prepared=N(consume=AsyncMock(return_value=result));bridge=N(resolve=AsyncMock())
        resolver,_=self.environment(prepared,bridge)
        self.assertEqual(await resolver(song,True),result)
        bridge.resolve.assert_not_awaited()

    async def test_stop_cancellation_does_not_start_another_source(self):
        song=main.Track('Selected','https://soundcloud.com/a/selected',youtube=True,metadata_resolved=True)
        prepared=N(consume=AsyncMock(side_effect=asyncio.CancelledError()));bridge=N(resolve=AsyncMock())
        resolver,_=self.environment(prepared,bridge)
        with self.assertRaises(asyncio.CancelledError):await resolver(song,True)
        bridge.resolve.assert_not_awaited()

    async def test_strict_stream_mode_never_downloads_from_bot_fallback(self):
        song=main.Track('Selected','https://soundcloud.com/a/selected',youtube=True,metadata_resolved=True)
        prepared=N(consume=AsyncMock(side_effect=main.StreamOnlyPlaybackError('no stream')))
        bridge=N(resolve=AsyncMock())
        resolver,_=self.environment(prepared,bridge,True)
        with self.assertRaises(main.StreamOnlyPlaybackError):await resolver(song,True)
        bridge.resolve.assert_not_awaited()

    async def test_failed_selected_bot_matches_title_not_original_broad_query(self):
        failed=N(download=AsyncMock(side_effect=RuntimeError('unavailable')))
        other=object();candidate=N(source=failed)
        song=main.Track('Selected – Artist','broad query',youtube=True,metadata_resolved=True,selected=candidate)
        result=('other.mp3','Selected – Artist',180)
        prepared=N(consume=AsyncMock());bridge=N(sources=[failed,other])
        resolver,ns=self.environment(prepared,bridge)
        race=N(race=AsyncMock(return_value=result))
        ns['ParallelSources']=Mock(return_value=race)
        self.assertEqual(await resolver(song,True),result)
        self.assertEqual(ns['ParallelSources'].call_args.args[0],[other])
        self.assertEqual(race.race.call_args.args[1],'Selected – Artist')

    async def test_generic_selected_audio_failure_never_claims_youtube_verification(self):
        song=main.Track('Selected','https://soundcloud.com/a/selected',youtube=True,metadata_resolved=True)
        player=main.Player(queue=deque([song]),resolver=AsyncMock(side_effect=RuntimeError('no audio')))
        progress=N(edit_text=AsyncMock())
        ns=dict(vars(main));ns.update(players={-100:player},lybot=object(),calls=N(leave_call=AsyncMock()),
            start_idle=AsyncMock(return_value=False),clear_player_panel=AsyncMock(),clean_temp=Mock())
        await closure('advance',ns)(-100,progress)
        text=progress.edit_text.call_args.args[0]
        self.assertNotIn('YouTube',text)
        self.assertNotIn('Verification',text)
        self.assertIn('audio',text)


class RestrictedTrackTests(unittest.TestCase):
    def test_drm_failure_skips_file_download_and_caches_restriction(self):
        url='https://soundcloud.com/a/drm-selection'
        with patch.dict(music_search._BLOCKED,{},clear=True),patch('main.get_cached_song',return_value=None), \
             patch('main.resolve_soundcloud_https',side_effect=RuntimeError('This video is DRM protected')), \
             patch('yt_dlp.YoutubeDL') as downloader,patch.dict(os.environ,{'MUSIC_STREAM_ONLY':'0'}):
            with self.assertRaises(RuntimeError):main.download_song(url,False,prefer_stream=True)
            downloader.assert_not_called()
            self.assertTrue(music_search.source_blocked(url))

    def test_search_filters_known_restricted_and_already_failed_tracks(self):
        blocked='https://soundcloud.com/a/blocked'
        rows=[{'title':'Blocked','url':blocked},{'title':'DRM','url':'https://soundcloud.com/a/drm','has_drm':True},
              {'title':'Private','url':'https://soundcloud.com/a/private','availability':'private'},
              {'title':'Playable','url':'https://soundcloud.com/a/playable','duration':180}]
        with patch.dict(music_search._BLOCKED,{},clear=True),patch('yt_dlp.YoutubeDL') as downloader:
            music_search.block_source(blocked)
            downloader.return_value.__enter__.return_value.extract_info.return_value={'entries':rows}
            choices=music_search.soundcloud_choices('query')
        self.assertEqual([c.title for c in choices],['Playable'])

    def test_transient_network_failures_are_not_marked_as_drm(self):
        self.assertFalse(music_search.restricted_error(TimeoutError('connection timed out')))
        self.assertTrue(music_search.restricted_error(RuntimeError('DRM protected')))

    def test_local_fallback_respects_exact_sc_selection_without_url_title_match(self):
        def downloader(opts):
            def extract(*args,**kwargs):
                (Path(opts['outtmpl']).parent/'selected.mp3').write_bytes(b'audio')
                return {'title':'Selected Remix','duration':180,'uploader':'Artist'}
            context=MagicMock();context.__enter__.return_value.extract_info.side_effect=extract
            return context
        path=None
        with patch('yt_dlp.YoutubeDL',side_effect=downloader), \
             patch.dict(os.environ,{'MUSIC_STREAM_ONLY':'0','MUSIC_CACHE_ENABLED':'0'}):
            try:
                path,title,duration=main.download_song('https://soundcloud.com/a/selected-remix',False)
                self.assertTrue(Path(path).is_file());self.assertEqual(duration,180)
                self.assertIn('Selected Remix',title)
            finally:main.clean_temp(path)
