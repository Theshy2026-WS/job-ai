"""Local SMTP delivery; secrets live in environment/Windows Credential Manager."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import wintypes
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import getpass
import os
import smtplib
import ssl
import sys


class EmailSetupError(RuntimeError):
    pass


class UncertainDelivery(RuntimeError):
    """The DATA phase began but no reliable delivery acknowledgement was returned."""


@contextmanager
def smtp_connection(config):
    """Fallback is pre-authentication only; never retry a sent message."""
    host, port = config['host'], int(config['port'])
    ports = [port, 587] if host.lower() == 'smtp.gmail.com' and port == 465 else [port]
    connection = None
    for candidate in ports:
        try:
            if candidate == 587:
                connection = smtplib.SMTP(host, candidate, timeout=30)
                client = connection.__enter__()
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            else:
                connection = smtplib.SMTP_SSL(host, candidate, context=ssl.create_default_context(), timeout=30)
                client = connection.__enter__()
            break
        except ssl.SSLCertVerificationError as exc:
            if connection:
                connection.close()
            raise EmailSetupError('SMTP TLS certificate verification failed. Check the system clock and trusted certificates; verification was not disabled.') from exc
        except (OSError, smtplib.SMTPException) as exc:
            if connection:
                connection.close()
            if candidate == ports[-1]:
                raise EmailSetupError('SMTP connection/TLS failed before authentication on port(s) ' + ', '.join(map(str, ports)) + '. This does not establish that the password is wrong. Check network, VPN/proxy/firewall, and email.host/email.port; then retry --configure-email.') from exc
            print('SMTP 465 connection failed; trying Gmail STARTTLS on 587 (no email sent).')
    try:
        yield client
    finally:
        # Closing after a completed command must not turn an acknowledged send
        # or successful login into failure just because QUIT was disconnected.
        connection.close()


def authenticate(smtp, username, password):
    try:
        smtp.login(username, password)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailSetupError('SMTP authentication rejected. Check email.username and use a Google app password for that same account, not the normal account password. No new credential saved.') from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailSetupError('SMTP disconnected during authentication. No new credential saved; check network/VPN/firewall and retry --configure-email. This alone does not prove the password is wrong.') from exc


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


def _target(username):
    return 'WenfeiJobDigest/SMTP/' + username


def credential_read(username):
    if os.name != 'nt':
        return None
    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.POINTER(_CREDENTIALW)()
    if not api.CredReadW(_target(username), 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return None
        raise EmailSetupError('Windows Credential Manager is unavailable in this session. Run locally in your Windows terminal, or set JOB_DIGEST_SMTP_PASSWORD.')
    try:
        value = pointer.contents
        return ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode('utf-16-le')
    finally:
        api.CredFree(pointer)


def credential_write(username, password):
    if os.name != 'nt':
        raise EmailSetupError('On this OS set JOB_DIGEST_SMTP_PASSWORD in the environment.')
    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    encoded = password.encode('utf-16-le')
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    value = _CREDENTIALW(Type=1, TargetName=_target(username),
                         CredentialBlobSize=len(encoded), CredentialBlob=blob,
                         Persist=2, UserName=username)
    if not api.CredWriteW(ctypes.byref(value), 0):
        raise EmailSetupError('Cannot save the SMTP credential in Windows Credential Manager.')


def password_for(config, interactive=True, replace=False):
    value = None if replace else os.environ.get('JOB_DIGEST_SMTP_PASSWORD') or credential_read(config['username'])
    if value:
        return value
    if not interactive or not sys.stdin.isatty():
        raise EmailSetupError('Email is not configured. Run python run.py --configure-email locally, or set JOB_DIGEST_SMTP_PASSWORD. Never paste a password into chat.')
    print('First-time email setup: enter a Gmail app password, not your normal Gmail password.')
    print('Create it in your Google account: https://myaccount.google.com/apppasswords')
    value = getpass.getpass('Gmail app password (hidden): ').replace(' ', '').strip()
    if not value:
        raise EmailSetupError('No password supplied; no email sent.')
    # Authenticate before persisting anything. This sends no message.
    with smtp_connection(config) as smtp:
        authenticate(smtp, config['username'], value)
    credential_write(config['username'], value)
    return value


def send_digest(config, recipient, subject, text, html, password, message_id=None):
    message = EmailMessage()
    message['From'] = config['sender']
    message['To'] = recipient
    message['Subject'] = subject
    message['Date'] = formatdate(localtime=False)
    message['Message-ID'] = message_id or make_msgid()
    message.set_content(text)
    message.add_alternative(html, subtype='html')
    # Connect/auth errors are definitely pre-send. Errors after DATA begins are
    # uncertain, so the caller must retain the outbox rather than silently retry.
    with smtp_connection(config) as smtp:
        authenticate(smtp, config['username'], password)
        try:
            refused = smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused:
            raise
        except Exception as exc:
            raise UncertainDelivery('SMTP acknowledgement was not obtained; delivery may have occurred. Check your inbox before resolving the outbox.') from exc
        if refused:
            raise UncertainDelivery('SMTP returned a recipient refusal; check the outbox.')
    return str(message['Message-ID'])
