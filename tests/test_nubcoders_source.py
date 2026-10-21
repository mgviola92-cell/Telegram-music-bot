import io
import json
import unittest
from unittest.mock import Mock
from nubcoders_source import resolve_sync, NubCodersUnavailable, usable_https_url

class Response:
    status=200
    headers={'Content-Type':'application/json'}
    def __init__(self, obj): self.payload=json.dumps(obj).encode()
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): pass
    def read(self, count): return self.payload[:count]

class NubCodersResolverTests(unittest.TestCase):
    def test_missing_token_does_not_request(self):
        with self.assertRaises(NubCodersUnavailable):
            resolve_sync('song', token='', opener=Mock())

    def test_audio_stream_key_and_bearer(self):
        opener=Mock()
        opener.open.return_value=Response({'stream_url':'https://cdn.example.org/a.mp3'})
        got=resolve_sync('test song',token='fake-token',opener=opener)
        self.assertEqual(got,'https://cdn.example.org/a.mp3')
        request=opener.open.call_args.args[0]
        self.assertIn('test+song',request.full_url)
        self.assertEqual(request.get_header('Authorization'),'Bearer fake-token')

    def test_secondary_audio_url(self):
        opener=Mock()
        opener.open.return_value=Response({'url':'https://audio.example.org/a.mp3'})
        self.assertEqual(resolve_sync('song',token='fake',opener=opener),'https://audio.example.org/a.mp3')

    def test_non_playable_result_fails(self):
        opener=Mock()
        opener.open.return_value=Response({'error':'quota_exceeded'})
        with self.assertRaises(NubCodersUnavailable):
            resolve_sync('song',token='fake',opener=opener)

    def test_reject_private_targets(self):
        for url in ('http://music.example.org/x','https://127.0.0.1/x','https://10.0.0.5/x','https://localhost/a','file:///etc/passwd','https://x.local/a','https://user:pass@example.org/a'):
            self.assertFalse(usable_https_url(url),url)

    def test_valid_https(self):
        self.assertTrue(usable_https_url('https://audio.example.org/stream?signature=abc'))

    def test_query_bound(self):
        with self.assertRaises(NubCodersUnavailable):
            resolve_sync('a'*161,token='fake',opener=Mock())
