"""Offline checks: idle VC media, group-slot caps and authorized radio config."""
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import idle_media
from main import Player, oldest_idle_chat, voice_slot_count


class VoiceCapacityTest(unittest.TestCase):
    def test_idle_uses_one_call_slot(self):
        players = {
            101: Player(),
            102: Player(),
            103: Player(),
        }
        players[101].idle_mode = "silence"
        players[101].idle_started_at = 100
        players[102].idle_mode = "radio"
        players[102].idle_started_at = 50
        self.assertEqual(voice_slot_count(players), 2)
        self.assertEqual(oldest_idle_chat(players, 103), 102)
        self.assertEqual(oldest_idle_chat(players, 102), 101)

    def test_playing_groups_are_never_evicted(self):
        players = {1: Player(), 2: Player()}
        players[1].idle_mode = "silence"
        players[1].current = object()  # A real playback must be protected.
        players[2].current = object()
        self.assertEqual(voice_slot_count(players), 2)
        self.assertIsNone(oldest_idle_chat(players, 3))

    def test_can_join_arbitrarily_many_groups_but_limit_vc_streams(self):
        players = {k: Player() for k in range(100)}
        self.assertEqual(voice_slot_count(players), 0)
        for k in range(4):
            players[k].idle_mode = "silence"
        self.assertEqual(voice_slot_count(players), 4)
        self.assertIsNotNone(oldest_idle_chat(players, 99))


class RadioConfigTest(unittest.TestCase):
    def test_radio_needs_explicit_host_permission(self):
        with patch.dict(os.environ, {"RADIO_STREAM_URL": "https://stream.example.org/live.mp3",
                                     "RADIO_STREAM_HOSTS": "stream.example.org"}):
            self.assertEqual(
                idle_media.configured_radio_url(),
                "https://stream.example.org/live.mp3"
            )

    def test_fails_closed_for_unapproved_or_private_hosts(self):
        for url in (
            "http://stream.example.org/live.mp3",
            "https://stream.example.org.evil.invalid/file",
            "https://127.0.0.1/internal",
            "https://localhost/stream",
            "https://stream.example.org:8080/file",
            "https://admin:pw@stream.example.org/live",
            "https://stream.example.org:bad/stream",
        ):
            with self.subTest(url=url), patch.dict(os.environ, {
                "RADIO_STREAM_URL": url, "RADIO_STREAM_HOSTS": "stream.example.org"
            }):
                self.assertIsNone(idle_media.configured_radio_url())

    def test_empty_or_unapproved_config_never_starts_radio(self):
        with patch.dict(os.environ, {
            "RADIO_STREAM_URL": "https://radio.invalid/live",
            "RADIO_STREAM_HOSTS": ""
        }):
            self.assertIsNone(idle_media.configured_radio_url())


class SilentLoopTest(unittest.TestCase):
    def test_idle_wav_is_silent_valid_mono_pcm_and_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            silence = Path(directory) / "idle.wav"
            with patch.object(idle_media, "_IDLE_PATH", silence), patch.object(
                idle_media, "IDLE_SECONDS", 2
            ):
                first = idle_media.ensure_idle_audio()
                second = idle_media.ensure_idle_audio()
                self.assertEqual(first, second)
                with wave.open(first, "rb") as wave_file:
                    self.assertEqual(wave_file.getframerate(), 8000)
                    self.assertEqual(wave_file.getnchannels(), 1)
                    self.assertEqual(wave_file.getsampwidth(), 2)
                    self.assertEqual(wave_file.getnframes(), 16000)
                    self.assertEqual(set(wave_file.readframes(16000)), {0})


if __name__ == "__main__":
    unittest.main()
