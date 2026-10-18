import unittest
from unittest.mock import patch
from bandwidth_status import BandwidthStatus

class BandwidthRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_filter_and_utc_request(self):
        captured = {}
        class Content:
            async def iter_chunked(self, size):
                yield b'[{"unit":"MB","values":[{"value":2,"timestamp":"2026-10-18T00:00:00Z"}]}]'
        class Response:
            status = 200
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                captured.update(kwargs['params'])
                return Response()
        status = BandwidthStatus()
        with patch.dict('os.environ', {'RENDER_METRICS_API_KEY':'test-only','MUSIC_RENDER_SERVICE_ID':'srv-test'}), patch('bandwidth_status.aiohttp.ClientSession', Session):
            await status.refresh()
        self.assertEqual(captured['service'], 'srv-test')
        self.assertNotIn('resource', captured)
        self.assertTrue(captured['startTime'].endswith('T00:00:00Z'))
        self.assertTrue(captured['endTime'].endswith('Z'))
        self.assertEqual(status.row[0], 2_000_000)
