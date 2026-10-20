import unittest
from youtube_relay import _search_track, source_id

class SkipIneligibleResults(unittest.TestCase):
    def row(self,video,clock):
        return {'videoRenderer':{'videoId':video,'title':{'simpleText':'song'},'lengthText':{'simpleText':clock}}}

    def test_long_first_result_does_not_hide_valid_song(self):
        data={'contents':[self.row('abcdefghijk','30:00'),self.row('qtYb7nV4814','2:57')]}
        track=_search_track(data)
        self.assertEqual(source_id(track.source),'qtYb7nV4814')
        self.assertEqual(track.duration,177)

    def test_zero_duration_does_not_hide_valid_song(self):
        data={'contents':[self.row('abcdefghijk','0:00'),self.row('qtYb7nV4814','2:57')]}
        self.assertEqual(source_id(_search_track(data).source),'qtYb7nV4814')

    def test_all_ineligible_still_fails_without_sending_audio(self):
        with self.assertRaises(LookupError):
            _search_track({'contents':[self.row('abcdefghijk','30:00')]})
