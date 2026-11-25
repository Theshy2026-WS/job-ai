from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from job_hunter import digest
from job_hunter.email_delivery import UncertainDelivery


def profile():
    return {'skills': ['Python', 'SQL', 'GCP', 'AWS', 'Snowflake', 'Airflow', 'Spark', 'ETL'],
            'experience_summary': {'data_engineering_years_min': 8},
            'preferences': {'allow_unknown_employment_type': True, 'allow_experience_stretch': True,
                            'max_experience_stretch_years': 2, 'max_posting_age_months': 2}}


def job():
    return {'source': 'ashby', 'board': 'real', 'company': 'Real Product Co', 'id': 'abc-123',
            'title': 'Senior Data Engineer', 'location': 'Remote, United States', 'country': 'US',
            'employment_type': 'FullTime', 'url': 'https://jobs.ashbyhq.com/real/abc-123',
            'description': 'Build Python SQL ETL pipelines on AWS, Snowflake and Airflow. 5+ years of data engineering experience.',
            'published_at': datetime.now(timezone.utc).isoformat(), 'published_at_source': 'ashby.publishedAt (last published)'}


def employer():
    return {'company': 'Real Product Co', 'source': 'ashby', 'board': 'real',
            'official_domain': 'real.example', 'official_careers_url': 'https://real.example/careers',
            'company_priority': 'established', 'employer_type': 'direct_employer', 'company_description': 'Database software product company.'}


class Filtering(unittest.TestCase):
    def test_new_resume(self):
        text = ('Data Engineer with 8+ years of experience. Python SQL AWS GCP Snowflake Databricks Kafka Terraform Claude Cursor Codex. ' * 8)
        reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'resume.pdf').write_bytes(b'private resume placeholder')
            with patch('pypdf.PdfReader', return_value=reader):
                p, s = digest.resume_profile(root, {'resume_path': 'resume.pdf', 'max_posting_age_months': 2})
        self.assertEqual(s['data_engineering_years_min'], 8)
        self.assertTrue({'AWS', 'Snowflake', 'Databricks', 'Kafka', 'Terraform', 'AI coding tools'} <= set(p['skills']))
        self.assertNotIn('dbt', p['skills'])

    def test_mandatory_vs_preferred_skills(self):
        p = profile()
        self.assertEqual(digest.missing_required_skills('QUALIFICATIONS\nStrong Python/SQL; preferable with dbt.\nNICE TO HAVE\nLookML experience', p), [])
        self.assertEqual(digest.missing_required_skills('MUST-HAVES\nSubstantial hands-on experience with dbt.\nExperience developing LookML views.', p), ['LookML', 'dbt'])
        self.assertEqual(digest.missing_required_skills('QUALIFICATIONS\nExperience with dbt or similar tools.', p), [])

    def test_constraints(self):
        self.assertIsNotNone(digest.rank_job(job(), profile(), 55))
        for changes in [
            {'country': 'CA', 'location': 'Toronto, Canada'}, {'employment_type': 'Contract'},
            {'description': job()['description'] + ' We cannot sponsor visas.'},
            {'description': job()['description'] + ' US citizenship required.'},
            {'description': job()['description'] + ' Our client is seeking a data engineer.'},
            {'title': 'Director of Data Engineering'},
        ]:
            with self.subTest(changes=changes):
                self.assertIsNone(digest.rank_job({**job(), **changes}, profile(), 55))

    def test_creation_is_not_publication(self):
        j = job(); j.pop('published_at'); j.pop('published_at_source')
        j.update(created_at=datetime.now(timezone.utc).isoformat(), updated_at=datetime.now(timezone.utc).isoformat())
        with patch.object(digest, 'enrich_job_metadata', return_value=j), patch.object(digest, 'public_get') as get:
            self.assertIsNone(digest.validate_job(j, employer(), profile(), 55, 1))
            get.assert_not_called()

    def test_bad_and_closed_links(self):
        e = employer()
        self.assertFalse(digest.safe_url('https://www.linkedin.com/jobs/view/123', e))
        self.assertFalse(digest.safe_url('https://real.example.evil.test/job', e))
        self.assertFalse(digest.is_board_link('https://jobs.ashbyhq.com/other/real', e))
        self.assertTrue(digest.is_board_link('https://jobs.ashbyhq.com/real/abc', e))
        for url, content in [('https://jobs.ashbyhq.com/real', 'Open jobs'), (job()['url'], 'This position has been filled')]:
            with patch.object(digest, 'enrich_job_metadata', side_effect=lambda j, **kw: j), patch.object(digest, 'public_get', return_value=(url, content)):
                self.assertIsNone(digest.validate_job(job(), e, profile(), 55, 1))

    def test_staffing_not_fetched(self):
        e = {**employer(), 'company_description': 'IT consulting and staff augmentation'}
        with patch.object(digest, 'fetch_json') as fetch:
            with self.assertRaises(ValueError): digest.load_board(e, 1)
            fetch.assert_not_called()

    def test_no_invented_official_board_proof(self):
        with patch.object(digest, 'public_get', return_value=('https://real.example/careers', '<p>No ATS link</p>')):
            with self.assertRaises(ValueError): digest.verify_employer(employer(), 1, [job()])


class Delivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'data_dir': 'digest', 'recipient': 'applicant@example.test', 'email': {'username': 'applicant@example.test'}}
        (self.root / 'digest_config.json').write_text(json.dumps(self.config))

    def run_mocked(self, arguments, sender):
        j = {**job(), 'digest_id': digest.identity(job()), 'verified_job_url': job()['url'], 'posted_at': job()['published_at'], 'publication_date_source': job()['published_at_source'], 'match': digest.rank_job(job(), profile(), 55)}
        stats = {'verified_employers': 1, 'configured_employers': 1, 'duplicates': 0, 'errors': []}
        with patch.object(digest, 'password_for', return_value='test'), patch.object(digest, 'resume_profile', return_value=(profile(), {'data_engineering_years_min': 8})), patch.object(digest, 'search', return_value=([j], stats)), patch.object(digest, 'send_digest', sender):
            return digest.main(arguments, root=self.root)

    def test_dry_run_does_not_send_or_mark(self):
        from unittest.mock import Mock
        sender = Mock()
        self.assertEqual(self.run_mocked(['--dry-run'], sender), 0)
        sender.assert_not_called()
        with closing(sqlite3.connect(self.root / 'digest/notifications.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivered').fetchone()[0], 0)

    def test_success_marks_only_after_send(self):
        from unittest.mock import Mock
        sender = Mock(return_value='message-id')
        self.assertEqual(self.run_mocked([], sender), 0)
        self.assertEqual(sender.call_args.args[1], 'applicant@example.test')
        with closing(sqlite3.connect(self.root / 'digest/notifications.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivered').fetchone()[0], 1)

    def test_uncertainty_blocks_silent_resend(self):
        from unittest.mock import Mock
        sender = Mock(side_effect=UncertainDelivery('test uncertainty'))
        self.assertEqual(self.run_mocked([], sender), 1)
        self.assertEqual(self.run_mocked([], sender), 1)
        self.assertEqual(sender.call_count, 1)
        with closing(sqlite3.connect(self.root / 'digest/notifications.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivered').fetchone()[0], 0)

    def test_configure_never_searches_or_sends(self):
        with patch.object(digest, 'password_for', return_value='test'), patch.object(digest, 'search') as search, patch.object(digest, 'send_digest') as send:
            self.assertEqual(digest.main(['--configure-email'], root=self.root), 0)
            search.assert_not_called(); send.assert_not_called()


if __name__ == '__main__': unittest.main()
