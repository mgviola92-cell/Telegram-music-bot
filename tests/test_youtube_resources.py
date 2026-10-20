import unittest
from unittest.mock import patch
from youtube_resources import memory_snapshot,over_budget

MB=1024**2

class MemoryBudgetTest(unittest.TestCase):
    def test_observed_reclaimable_cache_is_not_treated_as_active_heap(self):
        with patch('runtime_status.memory_sample',return_value=(441*MB,512*MB)),patch('youtube_resources.Path.read_text',return_value=f'anon {int(285.9*MB)}\ninactive_file {int(56.6*MB)}\n'):
            sample=memory_snapshot()
        self.assertAlmostEqual(sample['working']/MB,384.4,places=1)
        self.assertFalse(over_budget(sample))
        self.assertTrue(over_budget(sample,starting=True))

    def test_high_working_set_stops_extraction(self):
        self.assertTrue(over_budget(dict(used=450*MB,working=440*MB,limit=512*MB)))

    def test_raw_ceiling_still_stops_cache_heavy_process(self):
        self.assertTrue(over_budget(dict(used=480*MB,working=300*MB,limit=512*MB)))

    def test_missing_stats_falls_back_to_original_conservative_total(self):
        with patch('runtime_status.memory_sample',return_value=(441*MB,512*MB)),patch('youtube_resources.Path.read_text',side_effect=OSError()):
            sample=memory_snapshot()
        self.assertEqual(sample['working'],sample['used'])
        self.assertTrue(over_budget(sample))

    def test_active_file_and_anonymous_memory_are_not_subtracted(self):
        with patch('runtime_status.memory_sample',return_value=(450*MB,512*MB)),patch('youtube_resources.Path.read_text',return_value=f'anon {300*MB}\nactive_file {140*MB}\ninactive_file {10*MB}\n'):
            sample=memory_snapshot()
        self.assertEqual(sample['working'],440*MB)
        self.assertTrue(over_budget(sample))
