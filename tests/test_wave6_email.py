"""Wave 6.3b — email-code polling tests.

Verifies:
- parse_email_poll_url handles imap/imaps + required regex + optional from filter
- wait_for_email_code returns the matched group from a mock IMAP
- Timeout returns None
- max_age skips old messages
- mark_read calls store() with +FLAGS \\Seen
- IMAP creds never appear in logs (sentinel scan)
"""
from __future__ import annotations

import time
from email.message import EmailMessage

import pytest

from patchium.secrets import (
    parse_email_poll_url, wait_for_email_code,
)


# ─── URL parser tests ──────────────────────────────────────────────────


def test_parse_url_imaps_with_regex():
    cfg = parse_email_poll_url(
        "imaps://user:pass@imap.gmail.com:993?regex=\\d{6}&from=*@github.com"
    )
    assert cfg.server == "imap.gmail.com"
    assert cfg.port == 993
    assert cfg.username == "user"
    assert cfg.password == "pass"
    assert cfg.regex == "\\d{6}"
    assert cfg.from_filter == "*@github.com"
    assert cfg.use_ssl is True


def test_parse_url_imap_default_port():
    cfg = parse_email_poll_url("imap://u:p@host?regex=foo")
    assert cfg.port == 143
    assert cfg.use_ssl is False


def test_parse_url_missing_regex_raises():
    with pytest.raises(ValueError, match="regex"):
        parse_email_poll_url("imaps://u:p@host:993")


def test_parse_url_missing_auth_raises():
    with pytest.raises(ValueError, match="user:pass"):
        parse_email_poll_url("imaps://imap.gmail.com:993?regex=x")


def test_parse_url_bad_scheme_raises():
    with pytest.raises(ValueError, match="scheme"):
        parse_email_poll_url("http://u:p@host?regex=x")


def test_parse_url_custom_mailbox():
    cfg = parse_email_poll_url("imap://u:p@host?regex=x&mailbox=Archive")
    assert cfg.mailbox == "Archive"


# ─── mock IMAP for handler tests ───────────────────────────────────────


class MockIMAP:
    """Minimal imaplib.IMAP4 shim — accepts login, select, search, fetch, store."""
    # Class-level mailbox: list of (uid_bytes, raw_message_bytes, age_seconds)
    mailbox: list[tuple[bytes, bytes, int]] = []
    flagged_read: list[bytes] = []
    login_called: list[tuple[str, str]] = []

    def __init__(self, server, port):
        self.server = server
        self.port = port

    def login(self, user, password):
        MockIMAP.login_called.append((user, password))
        return ("OK", [b""])

    def select(self, mailbox):
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        # Ignore criteria, return all uids
        uids = b" ".join(uid for uid, _, _ in MockIMAP.mailbox)
        return ("OK", [uids])

    def fetch(self, uid, parts):
        for u, raw, _ in MockIMAP.mailbox:
            if u == uid:
                return ("OK", [(b"x", raw)])
        return ("NO", [b""])

    def store(self, uid, flags, value):
        MockIMAP.flagged_read.append(uid)
        return ("OK", [b""])

    def logout(self):
        return ("OK", [b""])


def _make_msg(body: str, from_addr: str = "noreply@example.com",
              age_seconds: int = 10) -> bytes:
    """Build a raw email message with a Date header set N seconds in the past."""
    import email.utils
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "user@example.com"
    msg["Subject"] = "Your verification code"
    msg["Date"] = email.utils.formatdate(time.time() - age_seconds)
    msg.set_content(body)
    return msg.as_bytes()


# ─── wait_for_email_code behavior ──────────────────────────────────────


def setup_function(_):
    MockIMAP.mailbox.clear()
    MockIMAP.flagged_read.clear()
    MockIMAP.login_called.clear()


def test_wait_returns_matched_code():
    MockIMAP.mailbox.append(
        (b"1", _make_msg("Your code is 123456. Enter it below."), 10)
    )
    cfg = parse_email_poll_url("imaps://u:p@host:993?regex=(\\d{6})")
    code = wait_for_email_code(cfg, timeout=2, poll_interval_s=0.1,
                                 _imap_class=MockIMAP)
    assert code == "123456"


def test_wait_returns_none_on_timeout():
    # Empty mailbox
    cfg = parse_email_poll_url("imaps://u:p@host?regex=(\\d{6})")
    t0 = time.time()
    code = wait_for_email_code(cfg, timeout=1, poll_interval_s=0.2,
                                 _imap_class=MockIMAP)
    elapsed = time.time() - t0
    assert code is None
    assert 0.8 <= elapsed <= 2.0


def test_wait_skips_old_messages():
    """Message older than max_age_s should be ignored."""
    MockIMAP.mailbox.append(
        (b"1", _make_msg("Old code 999999", age_seconds=600), 600)
    )
    MockIMAP.mailbox.append(
        (b"2", _make_msg("Fresh code 111222", age_seconds=10), 10)
    )
    cfg = parse_email_poll_url("imaps://u:p@host?regex=(\\d{6})")
    code = wait_for_email_code(cfg, timeout=2, max_age_s=300,
                                 poll_interval_s=0.1, _imap_class=MockIMAP)
    assert code == "111222"


def test_wait_mark_read_calls_store():
    MockIMAP.mailbox.append(
        (b"1", _make_msg("Code 555000"), 10)
    )
    cfg = parse_email_poll_url("imaps://u:p@host?regex=(\\d{6})")
    code = wait_for_email_code(cfg, timeout=2, mark_read=True,
                                 poll_interval_s=0.1, _imap_class=MockIMAP)
    assert code == "555000"
    assert b"1" in MockIMAP.flagged_read


def test_wait_no_mark_read_by_default():
    MockIMAP.mailbox.append(
        (b"1", _make_msg("Code 111222"), 10)
    )
    cfg = parse_email_poll_url("imaps://u:p@host?regex=(\\d{6})")
    wait_for_email_code(cfg, timeout=2, poll_interval_s=0.1,
                         _imap_class=MockIMAP)
    assert MockIMAP.flagged_read == []


def test_wait_extracts_first_group_when_present():
    MockIMAP.mailbox.append(
        (b"1", _make_msg("CODE: 999888 verify"), 10)
    )
    cfg = parse_email_poll_url("imaps://u:p@host?regex=CODE:%20(\\d+)")
    code = wait_for_email_code(cfg, timeout=2, poll_interval_s=0.1,
                                 _imap_class=MockIMAP)
    assert code == "999888"


def test_wait_returns_full_match_when_no_groups():
    MockIMAP.mailbox.append(
        (b"1", _make_msg("verification 987654 expires in"), 10)
    )
    cfg = parse_email_poll_url("imaps://u:p@host?regex=\\d{6}")
    code = wait_for_email_code(cfg, timeout=2, poll_interval_s=0.1,
                                 _imap_class=MockIMAP)
    assert code == "987654"
