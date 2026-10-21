import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import audius_source


class Response:
    status=200
    def __init__(self,data):
        self._body=json.dumps(data).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return self._body[:n]


class AudiusAuthRecoveryTests(unittest.TestCase):
    def setUp(self):audius_source._BACKOFF_UNTIL=0
    def tearDown(self):audius_source._BACKOFF_UNTIL=0

    def test_bearer_token_is_sent_only_in_header(self):
        opener=Mock()
        opener.open.return_value=Response({'data':[{'id':'public'}]})
        with patch.dict('os.environ',{'AUDIUS_BEARER_TOKEN':'trial-token'},clear=False):
            rows=audius_source.find_audius_tracks('song',opener=opener)
        req=opener.open.call_args.args[0]
        self.assertEqual(req.get_header('Authorization'),'Bearer trial-token')
        self.assertNotIn('trial-token',req.full_url)
        self.assertEqual(rows[0]['id'],'public')

    def test_403_cools_down_but_recovers_after_window(self):
        opener=Mock()
        opener.open.side_effect=HTTPError('https://api.audius.co/v1/tracks/search',403,
                                          'Forbidden',{},io.BytesIO(b''))
        with patch('audius_source.time.monotonic',return_value=100):
            self.assertEqual(audius_source.find_audius_tracks('song',opener=opener),[])
            self.assertEqual(audius_source.find_audius_tracks('song',opener=opener),[])
        self.assertEqual(opener.open.call_count,1)
        opener.open.side_effect=None
        opener.open.return_value=Response({'data':[]})
        with patch('audius_source.time.monotonic',return_value=200):
            self.assertEqual(audius_source.find_audius_tracks('song',opener=opener),[])
        self.assertEqual(opener.open.call_count,2)
