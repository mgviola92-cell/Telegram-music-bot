import unittest
from provider_health import ProviderHealth

class ProviderHealthTests(unittest.TestCase):
    def setUp(self):
        self.now=100.0
        self.health=ProviderHealth(threshold=2,cooldown_seconds=45,clock=lambda:self.now)

    def test_first_failure_does_not_block(self):
        self.health.failure('nub')
        self.assertTrue(self.health.available('nub'))

    def test_second_failure_cools_down_only_failed_provider(self):
        self.health.failure('nub')
        self.health.failure('nub')
        self.assertFalse(self.health.available('nub'))
        self.assertTrue(self.health.available('vapters'))

    def test_time_expiry_recovers_provider(self):
        self.health.failure('nub')
        self.health.failure('nub')
        self.now+=46
        self.assertTrue(self.health.available('nub'))
        self.assertEqual(self.health.failures.get('nub'),None)

    def test_success_resets_failure_streak(self):
        self.health.failure('nub')
        self.health.success('nub')
        self.health.failure('nub')
        self.assertTrue(self.health.available('nub'))

    def test_success_after_block_resets_state(self):
        self.health.failure('nub')
        self.health.failure('nub')
        self.health.success('nub')
        self.assertTrue(self.health.available('nub'))

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):ProviderHealth(threshold=0)
        with self.assertRaises(ValueError):ProviderHealth(cooldown_seconds=0)
