import re
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace as N
from unittest.mock import patch
from bandwidth_status import BandwidthStatus
from music_home import MusicHome

class ResetNoticeTests(unittest.TestCase):
    def test_october_and_year_rollover(self):
        status=BandwidthStatus()
        oct=status.reset_notice(datetime(2026,10,17,15,tzinfo=timezone.utc))
        self.assertIn('နိုဝင်ဘာ ၁၊ 2026',oct);self.assertIn('15 ရက်ခန့်',oct)
        dec=status.reset_notice(datetime(2026,12,31,23,59,tzinfo=timezone.utc))
        self.assertIn('ဇန်နဝါရီ ၁၊ 2027',dec);self.assertIn('1 ရက်ခန့်',dec)
    def test_exact_new_month_targets_following_month(self):
        result=BandwidthStatus().reset_notice(datetime(2026,11,1,tzinfo=timezone.utc))
        self.assertIn('ဒီဇင်ဘာ ၁၊ 2026',result);self.assertIn('30 ရက်ခန့်',result)
    def test_leap_february(self):
        result=BandwidthStatus().reset_notice(datetime(2028,2,1,tzinfo=timezone.utc))
        self.assertIn('မတ် ၁၊ 2028',result);self.assertIn('29 ရက်ခန့်',result)
    def test_prior_month_usage_never_becomes_current_zero(self):
        status=BandwidthStatus();status.row=(123456789,datetime(2000,1,1,tzinfo=timezone.utc))
        self.assertIn('ဒီလ data မရသေး',status.text());self.assertNotIn('0 MB',status.text())
    def test_home_contains_one_box_and_fits_photo_caption(self):
        home=MusicHome(None,None,{},None)
        with patch('music_home.BANDWIDTH.text',return_value='▸ ဒီ Bot bandwidth : 383.9 MB\n▸ ကျန် : 4.62 GB\nနောက်ဆုံး data : 10/17 18:30'):
            text=home.text(N(first_name='A'*64,id=42))
        self.assertEqual(text.count('<blockquote>'),1)
        self.assertLessEqual(len(re.sub('<[^>]*>','',text).encode('utf-16-le'))//2,1024)
