import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from job_hunter.recency import assess_posting, subtract_calendar_months


class RecencyTests(unittest.TestCase):
    def setUp(self):
        self.zone = patch('job_hunter.recency._new_york', return_value=timezone(timedelta(hours=-5)))
        self.zone.start()
        self.now = datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc)
        self.profile = {'preferences': {'max_posting_age_months': 2}}

    def tearDown(self):
        self.zone.stop()

    def check_date(self, value, **fields):
        return assess_posting({'published_at': value, **fields}, self.profile, self.now)

    def test_calendar_cutoff_is_not_sixty_days(self):
        result = self.check_date('2026-09-05')
        self.assertTrue(result['eligible'])
        self.assertEqual(result['cutoff_date'], '2026-09-05')
        self.assertEqual(result['posting_age_days'], 61)

    def test_cutoff_inclusive_and_previous_day_old(self):
        self.assertEqual(self.check_date('2026-09-05')['posting_recency'], 'recent')
        self.assertEqual(self.check_date('2026-09-04')['posting_recency'], 'old')

    def test_calendar_month_end_clamps_and_leap_year(self):
        self.assertEqual(subtract_calendar_months(date(2026, 8, 31), 2), date(2026, 6, 30))
        self.assertEqual(subtract_calendar_months(date(2024, 4, 30), 2), date(2024, 2, 29))
        self.assertEqual(subtract_calendar_months(date(2026, 1, 31), 2), date(2025, 11, 30))

    def test_updates_and_discovery_are_not_publication(self):
        result = assess_posting({'created_at': '2026-11-04', 'updated_at': '2026-11-05',
                                 'first_seen': '2026-11-05', 'fetched_at': self.now.isoformat()}, self.profile, self.now)
        self.assertEqual(result['posting_recency'], 'unknown')
        self.assertFalse(result['eligible'])

    def test_old_publication_not_refreshed_by_recent_update(self):
        self.assertEqual(self.check_date('2026-08-20', updated_at='2026-11-05')['posting_recency'], 'old')

    def test_future_date_and_instant_rejected(self):
        self.assertEqual(self.check_date('2026-11-06')['posting_recency'], 'future')
        self.assertEqual(self.check_date('2026-11-05T17:00:01Z')['posting_recency'], 'future')
        self.assertTrue(self.check_date('2026-11-05T17:00:00Z')['eligible'])

    def test_time_zone_boundaries_use_new_york_date(self):
        self.assertEqual(self.check_date('2026-09-05T03:59:59Z')['posting_recency'], 'old')
        self.assertEqual(self.check_date('2026-09-05T05:00:00Z')['posting_recency'], 'recent')

    def test_offsets_represent_same_publication_instant(self):
        a = self.check_date('2026-10-15T10:00:00-04:00')
        b = self.check_date('2026-10-15T14:00:00Z')
        self.assertEqual(a['posting_age_days'], b['posting_age_days'])
        self.assertTrue(a['eligible'] and b['eligible'])

    def test_epoch_seconds_and_milliseconds(self):
        stamp = datetime(2026, 10, 15, 14, tzinfo=timezone.utc).timestamp()
        for value in (stamp, stamp * 1000, str(int(stamp)), str(int(stamp * 1000))):
            with self.subTest(value=value):
                self.assertEqual(self.check_date(value)['posting_recency'], 'recent')

    def test_invalid_ambiguous_and_nonfinite_values_held(self):
        for value in ('yesterday', '2026-02-30', '2026-11-05T01:00:00', '20261105',
                      float('nan'), float('inf'), True, '', None, [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.check_date(value)['posting_recency'], 'unknown')

    def test_alternate_explicit_publication_field_and_source(self):
        result = assess_posting({'published_at': None, 'date_posted': '2026-10-01',
                                 'date_posted_source': 'https://example.invalid/job/1#datePosted'}, self.profile, self.now)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['publication_date_source'], 'https://example.invalid/job/1#datePosted')

    def test_date_only_now_compares_calendar_not_unknown_time_of_day(self):
        self.assertTrue(assess_posting({'published_at': '2026-11-05T23:00:00-05:00'}, self.profile, date(2026, 11, 5))['eligible'])
        self.assertEqual(assess_posting({'published_at': '2026-11-06'}, self.profile, date(2026, 11, 5))['posting_recency'], 'future')

    def test_naive_now_and_invalid_window_are_configuration_errors(self):
        with self.assertRaises(ValueError):
            assess_posting({'published_at': '2026-10-01'}, self.profile, datetime(2026, 11, 5))
        for months in (0, -1, True, 2.5, '2', None):
            with self.subTest(months=months), self.assertRaises(ValueError):
                assess_posting({'published_at': '2026-10-01'}, {'preferences': {'max_posting_age_months': months}}, self.now)


class RealTimezoneTests(unittest.TestCase):
    def test_missing_timezone_data_fails_clearly(self):
        with patch('job_hunter.recency.ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')):
            with self.assertRaisesRegex(RuntimeError, 'tzdata'):
                assess_posting({}, {}, date(2026, 11, 5))

    def test_summer_boundary_uses_dst_not_fixed_est(self):
        try:
            ZoneInfo('America/New_York')
        except ZoneInfoNotFoundError:
            self.skipTest('tzdata is required for the real New York daylight-saving test')
        result = assess_posting({'published_at': '2026-05-05'}, {}, datetime(2026, 7, 5, 4, 30, tzinfo=timezone.utc))
        self.assertEqual(result['cutoff_date'], '2026-05-05')
        self.assertTrue(result['eligible'])

    def test_publication_boundary_uses_its_own_daylight_saving_offset(self):
        try:
            ZoneInfo('America/New_York')
        except ZoneInfoNotFoundError:
            self.skipTest('tzdata is required for the real New York daylight-saving test')
        current = datetime(2026, 11, 5, 17, tzinfo=timezone.utc)
        self.assertTrue(assess_posting({'published_at': '2026-09-05T04:00:00Z'}, {}, current)['eligible'])
        self.assertEqual(assess_posting({'published_at': '2026-09-05T03:59:59Z'}, {}, current)['posting_recency'], 'old')

    def test_fall_back_fold_compares_real_instants(self):
        try:
            ny = ZoneInfo('America/New_York')
        except ZoneInfoNotFoundError:
            self.skipTest('tzdata is required for the real New York daylight-saving test')
        current = datetime(2026, 11, 1, 1, 15, tzinfo=ny, fold=1)
        posted = datetime(2026, 11, 1, 1, 30, tzinfo=ny, fold=0)
        self.assertTrue(assess_posting({'published_at': posted}, {}, current)['eligible'])


if __name__ == '__main__':
    unittest.main()
