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
                         ['VoiceShazamBot','VKMusicalRobot','finder_muzbot','MusicToolsRobot','vkm_bot'])
        self.assertIs(probe.call_args_list[0].args[0],baseline)
        self.assertEqual(active.sources,[baseline])

    def test_unreviewed_usernames_still_rejected(self):
        with self.assertRaises(ValueError):LyBotSource(object(),'unapproved_bot')
        with self.assertRaises(ValueError):LyBotSource(object(),'mp3robot')
        for name in ('VKMusicalRobot','finder_muzbot','MusicToolsRobot','vkm_bot'):
            self.assertEqual(LyBotSource(object(),name).username,name)
