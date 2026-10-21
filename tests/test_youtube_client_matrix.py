import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, patch

import youtube_client_matrix as matrix


class SecretAndMediaTests(unittest.TestCase):
    def test_readonly_secret_is_scoped_and_copy_is_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / 'secret.txt'
            raw = ('# Netscape HTTP Cookie File\n'
                   '.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tprivate-session\n'
                   '.other.example\tTRUE\t/\tTRUE\t1999999999\tSID\tother-session\n'
                   '.youtube.com\tTRUE\t/\tTRUE\t1\tOLD\texpired-session\n')
            secret.write_text(raw)
            secret.chmod(0o400)
            with matrix.cookie_copy(secret, folder) as (copy, status):
                self.assertEqual(status, 'available')
                self.assertNotEqual(Path(copy), secret)
                contents = Path(copy).read_text()
                self.assertIn('private-session', contents)
                self.assertNotIn('other-session', contents)
                self.assertNotIn('expired-session', contents)
                self.assertEqual(Path(copy).stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(copy).exists())
            self.assertEqual(secret.read_text(), raw)

    def test_missing_cookie_does_not_attempt_anonymous_cookie_case(self):
        with tempfile.TemporaryDirectory() as folder:
            with matrix.cookie_copy(Path(folder) / 'missing', folder) as (copy, status):
                self.assertIsNone(copy)
                self.assertEqual(status, 'missing_or_invalid')

    def test_headers_retain_client_identity_without_cookie_or_injection(self):
        block = matrix.header_block({'User-Agent': 'ClientUA', 'Cookie': 'private',
                                     'Authorization': 'private',
                                     'Accept': 'audio/*\r\nCookie: private'})
        self.assertIn('User-Agent: ClientUA\r\n', block)
        self.assertIn('Referer: https://www.youtube.com/', block)
        self.assertNotIn('private', block)

    def test_actual_ffmpeg_decode_uses_headers_without_logging_url(self):
        output = N(returncode=0, stdout=b'x' * 32000, stderr=b'')
        url = 'https://r1.googlevideo.com/videoplayback?secret=private'
        with patch.object(matrix.subprocess, 'run', return_value=output) as run:
            row = matrix.decode_media(url, {'User-Agent': 'ClientUA'})
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-i') + 1], url)
        self.assertIn('User-Agent: ClientUA', command[command.index('-headers') + 1])
        self.assertEqual(command[command.index('-tls_verify') + 1], '1')
        self.assertEqual(row['result'], 'decoded_audio')
        self.assertNotIn('private', json.dumps(row))

    def test_extraction_exception_never_returns_private_details(self):
        with patch.object(matrix, 'extract_ytdlp', side_effect=RuntimeError(
                '403 private-cookie https://secret.example/?token=secret')):
            row = matrix.probe('yt-dlp', 'tv', matrix.VIDEOS[0], '/tmp')
        self.assertEqual(row['error'], 'http_403')
        self.assertNotIn('private-cookie', json.dumps(row))
        self.assertNotIn('secret.example', json.dumps(row))


class ProcessAndScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_probe_is_inert_before_any_files_or_processes(self):
        with patch.object(matrix.os, 'open', side_effect=AssertionError('not inert')):
            await matrix.run_client_matrix({}, until=1)

    async def test_playback_kills_child_group_and_drains_pipes(self):
        gate = asyncio.Event()
        class Process:
            pid = 1234
            returncode = None
            async def communicate(self):
                await gate.wait()
                self.returncode = -9
                return b'', b''
        with patch.object(matrix.asyncio, 'create_subprocess_exec', AsyncMock(return_value=Process())), \
             patch.object(matrix, 'busy', return_value=True), \
             patch.object(matrix.os, 'killpg', side_effect=lambda *a: gate.set()) as kill:
            output, status = await matrix.guarded_process(['python', 'probe'], {}, 10)
        self.assertIsNone(output)
        self.assertEqual(status, 'aborted_for_playback')
        kill.assert_called_once_with(1234, matrix.signal.SIGKILL)

    async def test_timeout_also_kills_child_group(self):
        gate = asyncio.Event()
        class Process:
            pid = 1234
            returncode = None
            async def communicate(self):
                await gate.wait()
                self.returncode = -9
                return b'', b''
        with patch.object(matrix.asyncio, 'create_subprocess_exec', AsyncMock(return_value=Process())), \
             patch.object(matrix.os, 'killpg', side_effect=lambda *a: gate.set()):
            output, status = await matrix.guarded_process(['python', 'probe'], {}, -1)
        self.assertIsNone(output)
        self.assertEqual(status, 'probe_timeout')

    async def test_sequential_matrix_private_dependencies_and_cleanup(self):
        commands = []
        async def process(command, players, timeout, env=None):
            commands.append((command, env))
            if '--target' in command:
                return b'setup-output-not-logged', 'completed'
            client = command[command.index('--client') + 1]
            return json.dumps({'result': 'decoded_audio', 'client': client,
                               'cookies': 'private-not-allowed'}).encode(), 'completed'
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(matrix, 'PROBE_MARKER', Path(folder) / 'started'), \
             patch.object(matrix.asyncio, 'sleep', AsyncMock()), \
             patch.object(matrix, 'guarded_process', side_effect=process), \
             self.assertLogs('telegram_music', level='INFO') as logs:
            await matrix.run_client_matrix({}, until=time.time() + 600)
            count = len(commands)
            await matrix.run_client_matrix({}, until=time.time() + 600)
        self.assertEqual(len(commands), count)
        self.assertEqual(count, len(matrix.CASES) * len(matrix.VIDEOS) + 1)
        samples = [c for c, env in commands if '--client' in c]
        clients = [c[c.index('--client') + 1] for c in samples]
        self.assertEqual(clients, [case[1] for case in matrix.CASES for _ in matrix.VIDEOS])
        install = next(c for c, env in commands if '--target' in c)
        self.assertIn('--no-deps', install)
        self.assertNotIn('--upgrade', install)
        target = Path(install[install.index('--target') + 1])
        self.assertFalse(target.parent.exists())
        self.assertNotIn('private-not-allowed', '\n'.join(logs.output))
        for command in samples:
            client = command[command.index('--client') + 1]
            self.assertEqual('--cookie-file' in command, client in {'tv', 'web_embedded'})

    async def test_playback_abort_stops_remaining_requests_and_cleans_folder(self):
        commands = []
        async def process(command, *args, **kwargs):
            commands.append(command)
            return None, 'aborted_for_playback'
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(matrix, 'PROBE_MARKER', Path(folder) / 'started'), \
             patch.object(matrix.asyncio, 'sleep', AsyncMock()), \
             patch.object(matrix, 'guarded_process', side_effect=process):
            await matrix.run_client_matrix({}, until=time.time() + 600)
        self.assertEqual(len(commands), 1)
        workdir = Path(commands[0][commands[0].index('--workdir') + 1])
        self.assertFalse(workdir.exists())
