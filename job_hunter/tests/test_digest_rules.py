import json
import unittest
from unittest.mock import patch
from job_hunter import digest, discovery
from job_hunter.tests.test_digest import job, profile, employer


class NewRules(unittest.TestCase):
    def test_experience_is_a_ceiling_not_a_keyword(self):
        for requirement in ['9+ years of data engineering experience', '10 years of Python experience', 'More than 8 years of professional experience']:
            with self.subTest(requirement=requirement):
                self.assertIsNone(digest.rank_job({**job(), 'description': job()['description'] + '\n' + requirement}, profile(), 45))
        for requirement in ['8+ years of data engineering experience', '8-10 years of data engineering experience', 'Preferred qualifications\n10 years of Python experience', 'Our company has existed for over 20 years.']:
            with self.subTest(requirement=requirement):
                self.assertIsNotNone(digest.rank_job({**job(), 'description': job()['description'] + '\n' + requirement}, profile(), 45))

    def test_tool_gap_is_a_warning(self):
        ranked = digest.rank_job({**job(), 'description': job()['description'] + '\nRequirements\nSubstantial dbt experience'}, profile(), 45)
        self.assertIn('dbt', ranked['qualification_warnings'])

    def test_company_policy(self):
        self.assertTrue(digest.company_is_preferred(employer()))
        self.assertFalse(digest.company_is_preferred({**employer(), 'company_priority': 'smaller_or_unreviewed'}))

    def test_only_observed_ats_tokens(self):
        self.assertEqual(discovery.board_from_link('https://jobs.ashbyhq.com/observed-token', employer())['board'], 'observed-token')
        self.assertIsNone(discovery.board_from_link('https://www.linkedin.com/jobs/123', employer()))
        self.assertIsNone(discovery.board_from_link('https://real.example/careers', employer()))

    def test_official_posting_uses_publication_not_creation(self):
        posting = {'@type': 'JobPosting', 'title': 'Data Engineer', 'description': job()['description'],
                   'dateCreated': '2026-11-24', 'jobLocation': {'address': {'addressCountry': 'US'}}, 'employmentType': 'FULL_TIME'}
        html = '<script type="application/ld+json">' + json.dumps(posting) + '</script>'
        record = discovery.official_job(html, 'https://real.example/job/1', employer())
        self.assertFalse(record['published_at'])

    def test_directory_discovers_board_without_guessing(self):
        e = {**employer(), 'source': 'discovery'}
        with patch.object(discovery, 'official_job'):
            boards, jobs, errors = discovery.discover_company(e, 1, lambda *_: ('https://real.example/careers', '<a href="https://jobs.lever.co/actual">Open jobs</a>'), digest.TARGET_TITLE)
        self.assertEqual([(b['source'], b['board']) for b in boards], [('lever', 'actual')])
        self.assertEqual(errors, [])
