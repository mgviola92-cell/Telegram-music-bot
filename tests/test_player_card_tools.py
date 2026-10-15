import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock
from music_ui import player_controls, top_tracks_popup
from music_stats import MusicStats
import main
from test_music_extensions import closure


class PlayerCardToolsTest(unittest.IsolatedAsyncioTestCase):
    def test_card_has_controls_and_submenus_keep_playback_buttons(self):
        for menu in ('main','quality','effects','top'):
            keyboard=player_controls(-100,normalize=True,quality='low',effect='slowed',menu=menu)
            actions=[b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]
            self.assertIn('mud:pause:-100',actions)
            self.assertIn('mud:queue:-100',actions)
            if menu=='main':
                for action in ('normalize','quality','effects','top'):
                    self.assertIn('mud:'+action+':-100',actions)
                self.assertTrue(any('Normalize ON' in b.text for row in keyboard.inline_keyboard for b in row))
            else:self.assertIn('mud:back:-100',actions)

    def callback(self,player,*,admin=True):
        ns=dict(vars(main));update=AsyncMock();is_admin=AsyncMock(return_value=admin)
        ns.update(players={-100:player},update_player_panel=update,is_chat_admin=is_admin,
                  stats_popup=AsyncMock(return_value='🏆 All Groups\n1. Purple · 7'),
                  bot=N(send_message=AsyncMock()),calls=Mock())
        handler=closure('panel_callback',ns)
        def cb(action,id=20):
            return N(data=f'mud:{action}:-100',message=N(chat=N(id=-100),id=id),
                     from_user=N(id=7),answer=AsyncMock())
        return handler,cb,ns

    async def test_settings_menu_and_choice_update_existing_card_without_restart(self):
        player=main.Player(current=main.Track('Purple','Purple'),panel_id=20)
        handler,cb,ns=self.callback(player)
        await handler(None,cb('effects'))
        self.assertEqual(player.control_menu,'effects')
        await handler(None,cb('fxslowed'))
        self.assertEqual(player.audio_settings.effect,'slowed')
        self.assertEqual(player.playing_settings.effect,'normal')
        await handler(None,cb('qmedium'))
        self.assertEqual(player.audio_settings.quality,'medium')
        await handler(None,cb('normalize'))
        self.assertTrue(player.audio_settings.normalize)
        ns['bot'].send_message.assert_not_awaited()
        ns['calls'].assert_not_called()
        self.assertEqual(ns['update_player_panel'].await_count,4)

    async def test_non_admin_can_view_top_but_cannot_change_audio(self):
        player=main.Player(current=main.Track('Purple','Purple'),panel_id=20)
        handler,cb,ns=self.callback(player,admin=False)
        normalize=cb('normalize');await handler(None,normalize)
        self.assertFalse(player.audio_settings.normalize)
        self.assertTrue(normalize.answer.call_args.kwargs['show_alert'])
        await handler(None,cb('top'))
        self.assertEqual(player.control_menu,'top')
        top=cb('topall');await handler(None,top)
        self.assertTrue(top.answer.call_args.kwargs['show_alert'])
        ns['stats_popup'].assert_awaited_once_with(-100,global_scope=True)
        ns['bot'].send_message.assert_not_awaited()

    async def test_stale_card_cannot_change_settings(self):
        player=main.Player(current=main.Track('Purple','Purple'),panel_id=21)
        handler,cb,ns=self.callback(player)
        await handler(None,cb('qmedium',id=20))
        self.assertEqual(player.audio_settings.quality,'high')
        ns['update_player_panel'].assert_not_awaited()

    def test_all_groups_sums_same_song_without_changing_group_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            stats=MusicStats(str(Path(folder)/'stats.sqlite3'))
            stats.record(-100,'Purple');stats.record(-101,'Purple');stats.record(-101,'Purple')
            stats.record(-100,'Other')
            rows,total=stats.snapshot(None)
            self.assertEqual(rows[0],('Purple',3))
            self.assertEqual(total,(4,2))
            self.assertEqual(stats.snapshot(-100)[1],(2,2))
            stats.close()

    def test_unicode_top_popup_fits_telegram_limit(self):
        popup=top_tracks_popup([('🎵မြန်မာသီချင်း'*40,1000000)]*3,global_scope=True)
        self.assertLessEqual(len(popup.encode('utf-16-le'))//2,198)
        self.assertIn('All Groups',popup)
