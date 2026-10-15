import time
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch
from lybot_source import LyBotSource, run_candidate_probes


class CandidateSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_probe_never_contacts_candidates(self):
        with patch('lybot_source.run_live_probe',new=AsyncMock()) as probe:
            await run_candidate_probes(object(),None,0)
            probe.assert_not_awaited()

    async def test_candidates_are_tested_separately_from_active_source_list(self):
        baseline=N(username='VoiceShazamBot')
        active=N(sources=[baseline])
        with patch('lybot_source.run_live_probe',new=AsyncMock()) as probe:
            await run_candidate_probes(object(),active,time.time()+60)
        self.assertEqual([c.args[0].username for c in probe.call_args_list],
                         ['VoiceShazamBot','VKMusicalRobot'])
        self.assertIs(probe.call_args_list[0].args[0],baseline)
        self.assertEqual(active.sources,[baseline])

    def test_unreviewed_usernames_still_rejected(self):
        with self.assertRaises(ValueError):LyBotSource(object(),'unapproved_bot')
        with self.assertRaises(ValueError):LyBotSource(object(),'mp3robot')
        for name in ('VKMusicalRobot','finder_muzbot','MusicToolsRobot','vkm_bot'):
            self.assertEqual(LyBotSource(object(),name).username,name)

    def test_vk_numbered_result_matches_track_without_duration_badge(self):
        from lybot_source import matches_selection, song_button
        label='1. pGb - Purple (2:45)'
        media=N(title='Purple',performer='pGb')
        self.assertTrue(matches_selection(media,label))
        self.assertFalse(matches_selection(N(title='Other',performer='pGb'),label))
        message=N(reply_markup=N(inline_keyboard=[[N(text=label,callback_data='track-1')]]))
        self.assertEqual(song_button(message,'Purple Wink pGb'),label)
