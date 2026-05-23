"""Wave 7.4 — real-world IMAP test using the actual `imaplib.IMAP4` client
against an in-process RFC 3501 server fixture.

The mock tests in test_wave6_email.py exercise our parsing logic only.
These tests exercise:
  - imaplib's actual command formatter
  - The RFC 822 multi-line response parser
  - The literal-{SIZE} fetch body retrieval
  - The full TCP round-trip (no SSL — would require cert setup)

If THIS suite passes, we know wait_for_email_code works against a real
IMAP server, modulo SSL nuances (server cert validation etc).
"""
from __future__ import annotations

import email
import email.utils
import time

import pytest

from patchium.secrets import (
    parse_email_poll_url, wait_for_email_code,
)
from ._imap_server import MiniIMAPServer


def _build_msg(body: str, from_addr: str = "noreply@example.com",
                age_seconds: int = 10) -> bytes:
    """Build an RFC 822 message with a Date header N seconds in the past."""
    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "user@example.com"
    msg["Subject"] = "Your verification code"
    msg["Date"] = email.utils.formatdate(time.time() - age_seconds, usegmt=True)
    msg.set_content(body)
    return msg.as_bytes()


# ─── fixture: spin up MiniIMAPServer per test ──────────────────────────


@pytest.fixture
def imap_server():
    srv = MiniIMAPServer()
    srv.start()
    yield srv
    srv.stop()


# ─── live (against in-process server) tests ────────────────────────────


def test_live_imap_returns_matched_code(imap_server):
    """Full TCP round-trip: imaplib client → in-proc server → match."""
    imap_server.add_message(_build_msg("Your verification code is 654321."))
    cfg = parse_email_poll_url(
        f"imap://user:pw@127.0.0.1:{imap_server.actual_port}?regex=(\\d{{6}})"
    )
    code = wait_for_email_code(cfg, timeout=5, poll_interval_s=0.5)
    assert code == "654321"
    # Server saw a login attempt with our creds
    assert ("user", "pw") in imap_server.login_attempts


def test_live_imap_returns_none_on_timeout(imap_server):
    """Empty mailbox + short timeout → None."""
    cfg = parse_email_poll_url(
        f"imap://u:p@127.0.0.1:{imap_server.actual_port}?regex=(\\d{{6}})"
    )
    t0 = time.time()
    code = wait_for_email_code(cfg, timeout=2, poll_interval_s=0.5)
    elapsed = time.time() - t0
    assert code is None
    assert 1.8 <= elapsed <= 3.0


def test_live_imap_skips_old_messages(imap_server):
    """Message older than max_age_s is filtered out."""
    imap_server.add_message(
        _build_msg("Stale code 111111", age_seconds=900)
    )
    imap_server.add_message(
        _build_msg("Fresh code 222222", age_seconds=20)
    )
    cfg = parse_email_poll_url(
        f"imap://u:p@127.0.0.1:{imap_server.actual_port}?regex=(\\d{{6}})"
    )
    code = wait_for_email_code(cfg, timeout=5, max_age_s=300,
                                 poll_interval_s=0.5)
    assert code == "222222"


def test_live_imap_from_filter(imap_server):
    """FROM filter restricts which messages are considered."""
    imap_server.add_message(
        _build_msg("Spam code 999999", from_addr="spam@elsewhere.com")
    )
    imap_server.add_message(
        _build_msg("Real code 333333", from_addr="noreply@github.com")
    )
    cfg = parse_email_poll_url(
        f"imap://u:p@127.0.0.1:{imap_server.actual_port}"
        f"?regex=(\\d{{6}})&from=github.com"
    )
    code = wait_for_email_code(cfg, timeout=5, poll_interval_s=0.5)
    assert code == "333333"


def test_live_imap_mark_read_consumes_message(imap_server):
    """With mark_read=True, the message is flagged + not returned again."""
    uid = imap_server.add_message(_build_msg("Code 444444"))
    cfg = parse_email_poll_url(
        f"imap://u:p@127.0.0.1:{imap_server.actual_port}?regex=(\\d{{6}})"
    )
    code = wait_for_email_code(cfg, timeout=5, mark_read=True,
                                 poll_interval_s=0.5)
    assert code == "444444"
    assert uid in imap_server.flagged_read
    # Second poll: same message is now flagged → search returns no UIDs → timeout
    t0 = time.time()
    code2 = wait_for_email_code(cfg, timeout=2, poll_interval_s=0.5)
    elapsed = time.time() - t0
    assert code2 is None
    assert elapsed >= 1.8


def test_live_imap_multiline_message_body(imap_server):
    """Real RFC 822 fetch with multi-line body and literal {SIZE} format
    should parse cleanly through imaplib."""
    long_body = "Header\n\nVerification code: 777777\n\nFooter\n" + ("x " * 50)
    imap_server.add_message(_build_msg(long_body))
    cfg = parse_email_poll_url(
        f"imap://u:p@127.0.0.1:{imap_server.actual_port}?regex=(\\d{{6}})"
    )
    code = wait_for_email_code(cfg, timeout=5, poll_interval_s=0.5)
    assert code == "777777"


def test_live_imap_returns_first_of_multiple_matches(imap_server):
    """When multiple messages match, return the first one found (newest UID first)."""
    imap_server.add_message(_build_msg("Older code 100001", age_seconds=120))
    imap_server.add_message(_build_msg("Newer code 200002", age_seconds=10))
    cfg = parse_email_poll_url(
        f"imap://u:p@127.0.0.1:{imap_server.actual_port}?regex=(\\d{{6}})"
    )
    code = wait_for_email_code(cfg, timeout=5, max_age_s=300,
                                 poll_interval_s=0.5)
    # Implementation iterates newest first (reverse UID order); newer wins
    assert code == "200002"
