from datetime import date
import json
import unittest
from unittest.mock import patch, MagicMock
from job_hunter import jobboards, digest
from job_hunter.recency import assess_posting
from job_hunter.tests.test_digest import employer, job, profile


class JobBoardsTests(unittest.TestCase):
    def page(self, **changes):
        schema = {'@type': 'JobPosting', 'identifier': {'value': 'req1'}, 'title': 'Data Engineer',
                  'description': job()['description'], 'datePosted': '2026-11-02',
                  'hiringOrganization': {'name': employer()['company']},
                  'jobLocation': {'address': {'addressCountry': 'US', 'addressLocality': 'New York'}}, 'employmentType': 'FULL_TIME'}
        schema.update(changes)
        return '<script type="application/ld+json">' + json.dumps(schema) + '</script>'

    def test_three_source_links_retained(self):
        for url, name in [('https://www.indeed.com/viewjob?jk=abc', 'Indeed'), ('https://www.dice.com/job-detail/abc-123', 'Dice'), ('https://builtin.com/job/data-engineer/123', 'Built In')]:
            result = jobboards.parse_detail(self.page(), url, [employer()])
            self.assertEqual(result[0]['source_listing_url'], url)
            self.assertEqual(result[0]['discovery_source'], name)
            self.assertEqual(result[0]['published_at'], '2026-11-02')

    def test_unknown_company_and_staffing_rejected(self):
        url = 'https://www.indeed.com/viewjob?jk=abc'
        self.assertIsNone(jobboards.parse_detail(self.page(hiringOrganization={'name': 'Agency for Amazon'}), url, [employer()]))
        self.assertIsNone(jobboards.parse_detail(self.page(), url, [{**employer(), 'employer_type': 'staffing'}]))

    def test_unknown_or_created_date_not_used(self):
        self.assertIsNone(jobboards.parse_detail(self.page(datePosted='', dateCreated='2026-11-24'), 'https://www.indeed.com/viewjob?jk=abc', [employer()]))

    def test_one_month_cutoff(self):
        p = profile(); p['preferences']['max_posting_age_months'] = 1
        for posted, eligible in [('2026-10-23', False), ('2026-10-24', True), ('2026-11-24', True)]:
            self.assertEqual(assess_posting({**job(), 'published_at': posted}, p, now=date(2026, 11, 24))['eligible'], eligible)

    def test_no_linkedin_or_search_result_pages(self):
        for url in ['https://linkedin.com/jobs/1', 'https://www.indeed.com/jobs?q=data', 'https://dice.com.evil.test/job-detail/1', 'http://www.dice.com/job-detail/1']:
            self.assertIsNone(jobboards.provider_for(url))

    def test_source_read_failure_is_explicit(self):
        with patch.object(jobboards, 'search_results', return_value=['https://www.indeed.com/viewjob?jk=abc']), patch.object(jobboards, 'get_page', side_effect=OSError('403')):
            jobs, errors, stats = jobboards.discover_jobboards([employer()], 1, 1, {'enabled': True})
        self.assertEqual(jobs, [])
        self.assertEqual(stats['Indeed']['read'], 0)
        self.assertTrue(any('not bypassed' in e['reason'] for e in errors))

    def test_same_job_keeps_multiple_links(self):
        first = {**job(), 'verified_job_url': job()['url'], 'posted_at': '2026-11-02', 'match': {'score': 70}}
        second = {**first, 'source': 'job_board', 'board': 'indeed.com', 'discovery_source': 'Indeed', 'verified_job_url': 'https://www.indeed.com/viewjob?jk=abc'}
        results = digest.deduplicate_jobs([first, second])
        self.assertEqual(len(results), 1)
        self.assertEqual({x['source'] for x in results[0]['source_links']}, {'Indeed', 'Ashby'})

    def test_optional_api_key_is_header_only_and_no_snippet_dates(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'web': {'results': [{'url': 'https://www.indeed.com/viewjob?jk=abc', 'age': '1 day ago'}]}}).encode()
        with patch.dict(jobboards.os.environ, {'BRAVE_SEARCH_API_KEY': 'dummy-key'}), patch.object(jobboards, 'build_opener') as opener, patch.object(jobboards, 'rss_results') as rss:
            opener.return_value.open.return_value = response
            self.assertEqual(jobboards.search_results('data engineer', 1), ['https://www.indeed.com/viewjob?jk=abc'])
            req = opener.return_value.open.call_args.args[0]
            self.assertNotIn('dummy-key', req.full_url)
            self.assertEqual(req.get_header('X-subscription-token'), 'dummy-key')
            rss.assert_not_called()
