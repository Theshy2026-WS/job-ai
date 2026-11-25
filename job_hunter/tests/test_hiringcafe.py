from contextlib import contextmanager
import json
import unittest
from unittest.mock import MagicMock, patch
from job_hunter import hiringcafe, jobboards
from job_hunter.tests.test_digest import employer, job


class HiringCafeTests(unittest.TestCase):
    url = 'https://hiringcafe.com/job/data-engineer-company-abc123'

    def detail(self, date='2026-11-02T00:00:00Z'):
        return '<script type="application/ld+json">' + json.dumps({
            '@type': 'JobPosting', 'title': 'Data Engineer', 'identifier': {'value': 'abc123'},
            'description': job()['description'], 'datePosted': date,
            'hiringOrganization': {'name': employer()['company']},
            'jobLocation': {'address': {'addressCountry': 'US'}}, 'employmentType': 'FULL_TIME'}) + '</script>'

    def test_actual_new_domain_and_original_source_label(self):
        parsed = jobboards.parse_detail(self.detail(), self.url, [employer()])
        self.assertEqual(parsed[0]['discovery_source'], 'Hiring.cafe')
        self.assertEqual(parsed[0]['source_listing_url'], self.url)
        self.assertEqual(parsed[0]['published_at'], '2026-11-02T00:00:00Z')
        self.assertEqual(jobboards.provider_for(self.url.replace('hiringcafe.com', 'hiring.cafe')), 'Hiring.cafe')

    def test_fetch_or_discovery_age_is_not_publication(self):
        self.assertIsNone(jobboards.parse_detail(self.detail(date=''), self.url, [employer()]))

    def test_no_account_application_or_linkedin_navigation(self):
        for url in ['https://hiringcafe.com/auth', 'https://hiringcafe.com/myhiringcafe/tracker', 'https://linkedin.com/jobs/1', 'https://hiringcafe.com.evil.test/job/1']:
            self.assertFalse(hiringcafe.allowed(url))

    def test_search_browser_reads_real_job_links(self):
        page = MagicMock()
        page.url = hiringcafe.HOME
        def go(url, **kwargs):
            page.url = url
            return MagicMock(status=200)
        page.goto.side_effect = go
        page.title.return_value = 'Data Engineer | HiringCafe'
        page.content.return_value = self.detail()
        page.get_by_role.return_value.evaluate_all.return_value = [self.url]
        @contextmanager
        def fake_browser(timeout):
            yield page
        with patch.object(hiringcafe, 'browser_page', fake_browser):
            jobs, errors, stats = hiringcafe.discover([employer()], 1, {'pages': 1})
        self.assertEqual(stats, {'leads': 1, 'read': 1, 'verified_details': 1})
        self.assertEqual(errors, [])
        self.assertEqual(jobs[0][0]['source_listing_url'], self.url)
        self.assertEqual(page.goto.call_args_list[0].args[0], hiringcafe.SEARCH)

    def test_security_challenge_stops(self):
        page = MagicMock(url=hiringcafe.HOME)
        page.title.return_value = 'Verify you are human'
        with self.assertRaisesRegex(RuntimeError, 'without bypassing'):
            hiringcafe.check_page(page)

    def test_access_denial_stops_without_trying_other_details(self):
        page = MagicMock(url=hiringcafe.SEARCH)
        page.title.return_value = 'Data Engineer Jobs | HiringCafe'
        page.goto.side_effect = [MagicMock(status=200), MagicMock(status=403)]
        page.get_by_role.return_value.evaluate_all.return_value = [self.url, self.url + '-second']
        @contextmanager
        def fake_browser(timeout):
            yield page
        with patch.object(hiringcafe, 'browser_page', fake_browser):
            jobs, errors, stats = hiringcafe.discover([employer()], 1, {'pages': 1})
        self.assertEqual(page.goto.call_count, 2)
        self.assertEqual(jobs, [])
        self.assertEqual(stats['read'], 0)
        self.assertTrue(any('403' in error['reason'] for error in errors))
