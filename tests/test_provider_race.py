import asyncio
import unittest
from provider_race import race_candidates

class ProviderRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_candidate_cancels_slow_candidate(self):
        cancelled=asyncio.Event()
        async def fast(): return ('https://example.org/audio','fast',90)
        async def slow():
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        result=await asyncio.wait_for(race_candidates([('fast',fast),('slow',slow)]),1)
        self.assertEqual(result[1],'fast')
        self.assertTrue(cancelled.is_set())

    async def test_provider_error_does_not_cancel_other_candidate(self):
        async def bad(): raise RuntimeError('temporary')
        async def good():
            await asyncio.sleep(0)
            return ('https://example.org/audio','good',90)
        self.assertEqual((await race_candidates([('bad',bad),('good',good)]))[1],'good')

    async def test_empty_candidate_is_not_winner(self):
        async def empty(): return None
        async def valid():
            await asyncio.sleep(0)
            return ('https://example.org/audio','valid',90)
        self.assertEqual((await race_candidates([('empty',empty),('valid',valid)]))[1],'valid')

    async def test_no_candidates(self):
        with self.assertRaises(LookupError): await race_candidates([])
