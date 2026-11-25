from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_hunter import digest
from job_hunter.email_delivery import send_digest
from job_hunter.tests.test_digest import employer, job, profile


class DigestTransportTests(unittest.TestCase):
    def test_same_title_different_team_is_not_removed(self):
        first = {**job(), 'posted_at': '2026-11-24', 'match': {'score': 70}}
        duplicate = {**first, 'id': 'city-version', 'location': 'Seattle, WA'}
        different = {**first, 'id': 'different-team', 'description': job()['description'] + ' Own the payments data domain.'}
        self.assertEqual(len(digest.deduplicate_jobs([first, duplicate, different])), 2)

    def test_smtp_message_recipient_html_and_no_resume(self):
        config = {'host': 'smtp.example.test', 'port': 465, 'username': 'sender@example.test', 'sender': 'sender@example.test'}
        with patch('job_hunter.email_delivery.smtplib.SMTP_SSL') as factory:
            client = factory.return_value.__enter__.return_value
            client.send_message.return_value = {}
            send_digest(config, 'applicant@example.test', 'Jobs', 'plain', '<p>Official jobs</p>', 'test-only')
            message = client.send_message.call_args.args[0]
            self.assertEqual(message['To'], 'applicant@example.test')
            self.assertIn('Official jobs', message.get_body(preferencelist=('html',)).get_content())
            self.assertEqual(list(message.iter_attachments()), [])

    def test_notified_job_is_not_revalidated_or_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'employers.json').write_text(json.dumps([employer()]))
            config = {'employers_path': 'employers.json', 'timeout_seconds': 1, 'max_workers': 1, 'min_score': 55}
            with closing(digest.digest_database(root / 'notifications.sqlite3')) as db:
                db.execute('INSERT INTO delivered VALUES (?,?)', (digest.identity(job()), 'now')); db.commit()
                with patch.object(digest, 'load_board', return_value=([job()], {})), patch.object(digest, 'validate_job') as validate:
                    jobs, stats = digest.search(root, config, root, profile(), db)
                    self.assertEqual(jobs, [])
                    self.assertEqual(stats['duplicates'], 1)
                    validate.assert_not_called()



if __name__ == '__main__':
    unittest.main()
