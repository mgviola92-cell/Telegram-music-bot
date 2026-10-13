"""Fast deterministic tests for adapted queue and inline player features."""
import unittest
from collections import deque
from unittest.mock import patch

from music_ui import player_controls, queue_display, queue_remove, queue_shuffle


class MusicFeaturesTest(unittest.TestCase):
    def test_queue_remove_uses_one_based_position(self):
        songs = deque(["playing-next", "second", "third"])
        self.assertEqual(queue_remove(songs, 2), "second")
        self.assertEqual(list(songs), ["playing-next", "third"])
        with self.assertRaises(IndexError):
            queue_remove(songs, 0)
        with self.assertRaises(IndexError):
            queue_remove(songs, 99)

    def test_shuffle_preserves_all_tracks(self):
        songs = deque(["one", "two", "three"])
        with patch("music_ui.random.shuffle", side_effect=lambda x: x.reverse()):
            self.assertEqual(queue_shuffle(songs), 3)
        self.assertEqual(list(songs), ["three", "two", "one"])

    def test_player_controls_are_group_bound_and_state_aware(self):
        controls = player_controls(-1003924604662, paused=False, loop=False)
        first = controls.inline_keyboard[0]
        self.assertEqual(first[0].callback_data, "mud:pause:-1003924604662")
        self.assertEqual(first[1].callback_data, "mud:skip:-1003924604662")
        self.assertEqual(first[2].callback_data, "mud:stop:-1003924604662")
        second = player_controls(-1003924604662, paused=True, loop=True)
        self.assertEqual(
            second.inline_keyboard[0][0].callback_data,
            "mud:resume:-1003924604662",
        )
        self.assertIn("ON", second.inline_keyboard[1][0].text)

    def test_queue_message(self):
        msg = queue_display("Current original", ["Second", "Third"], loop=True)
        self.assertIn("Current original", msg)
        self.assertIn("1. Second", msg)
        self.assertIn("Repeat current: ON", msg)


if __name__ == "__main__":
    unittest.main()
