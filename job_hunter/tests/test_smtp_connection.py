import smtplib
import unittest
from unittest.mock import patch
from job_hunter import email_delivery as mail


class SMTPConnectionTests(unittest.TestCase):
    config = {'host': 'smtp.gmail.com', 'port': 465, 'username': 'test@example.test', 'sender': 'test@example.test'}

    def test_closed_465_uses_starttls_before_login(self):
        with patch.object(mail.smtplib, 'SMTP_SSL', side_effect=smtplib.SMTPServerDisconnected('closed')), patch.object(mail.smtplib, 'SMTP') as fallback:
            client = fallback.return_value.__enter__.return_value
            with mail.smtp_connection(self.config) as smtp:
                mail.authenticate(smtp, self.config['username'], 'dummy')
            client.starttls.assert_called_once()
            client.login.assert_called_once()
            fallback.return_value.close.assert_called_once()

    def test_both_ports_closed_reports_connection_not_password(self):
        with patch.object(mail.smtplib, 'SMTP_SSL', side_effect=smtplib.SMTPServerDisconnected('closed')), patch.object(mail.smtplib, 'SMTP', side_effect=TimeoutError()):
            with self.assertRaisesRegex(mail.EmailSetupError, 'before authentication'):
                with mail.smtp_connection(self.config):
                    self.fail('must not reach authentication')

    def test_auth_rejection_does_not_fallback_or_save(self):
        with patch.object(mail.smtplib, 'SMTP_SSL') as ssl, patch.object(mail.smtplib, 'SMTP') as fallback, patch.object(mail, 'credential_write') as save:
            ssl.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'rejected')
            with self.assertRaisesRegex(mail.EmailSetupError, 'authentication rejected'):
                with mail.smtp_connection(self.config) as smtp:
                    mail.authenticate(smtp, self.config['username'], 'dummy')
            fallback.assert_not_called()
            save.assert_not_called()

    def test_no_retry_after_send_begins(self):
        with patch.object(mail.smtplib, 'SMTP_SSL') as ssl, patch.object(mail.smtplib, 'SMTP') as fallback:
            client = ssl.return_value.__enter__.return_value
            client.send_message.side_effect = smtplib.SMTPServerDisconnected('closed')
            with self.assertRaises(mail.UncertainDelivery):
                mail.send_digest(self.config, 'test@example.test', 'subject', 'text', '<p>html</p>', 'dummy')
            client.send_message.assert_called_once()
            fallback.assert_not_called()

    def test_quit_disconnect_does_not_hide_acknowledged_delivery(self):
        with patch.object(mail.smtplib, 'SMTP_SSL') as ssl:
            ssl.return_value.__exit__.side_effect = smtplib.SMTPServerDisconnected('quit closed')
            ssl.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertTrue(mail.send_digest(self.config, 'test@example.test', 'subject', 'text', '<p>html</p>', 'dummy'))
