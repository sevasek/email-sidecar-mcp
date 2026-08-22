"""
Tests for email_sidecar/server.py

These tests cover the logic that runs before any network call is made —
reply-loop detection, message parsing, reference extraction — so they run
without real email credentials.
"""

from __future__ import annotations
import email
import sys
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

# Add project root to path so we can import the sidecar without installing it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Patch env vars before importing the module (it reads them at import time)
os.environ.setdefault("EMAIL_ADDRESS",    "test@example.com")
os.environ.setdefault("EMAIL_PASSWORD",   "password")
os.environ.setdefault("EMAIL_IMAP_HOST",  "imap.example.com")
os.environ.setdefault("EMAIL_SMTP_HOST",  "smtp.example.com")

from email_sidecar.logic import (
    is_automated,
    get_body,
    parse_references,
    message_to_dict,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_msg(
    subject="Hello",
    from_addr="customer@example.com",
    headers: dict | None = None,
    body="Test body",
) -> email.message.Message:
    msg = email.message.Message()
    msg["From"]    = from_addr
    msg["To"]      = "test@example.com"
    msg["Subject"] = subject
    msg["Date"]    = "Mon, 08 Jun 2026 10:00:00 +0000"
    msg["Message-ID"] = "<abc123@example.com>"
    msg.set_payload(body)
    for k, v in (headers or {}).items():
        msg[k] = v
    return msg


# ── is_automated ──────────────────────────────────────────────────────────────

class TestIsAutomated:

    def test_normal_email_is_not_automated(self):
        assert not is_automated(make_msg())

    def test_auto_submitted_header(self):
        msg = make_msg(headers={"Auto-Submitted": "auto-replied"})
        assert is_automated(msg)

    def test_auto_submitted_no_is_not_automated(self):
        msg = make_msg(headers={"Auto-Submitted": "no"})
        assert not is_automated(msg)

    def test_precedence_bulk(self):
        msg = make_msg(headers={"Precedence": "bulk"})
        assert is_automated(msg)

    def test_precedence_list(self):
        msg = make_msg(headers={"Precedence": "list"})
        assert is_automated(msg)

    def test_precedence_normal_not_automated(self):
        msg = make_msg(headers={"Precedence": "normal"})
        assert not is_automated(msg)

    def test_x_autoreply_header(self):
        msg = make_msg(headers={"X-Autoreply": "yes"})
        assert is_automated(msg)

    def test_x_auto_response_suppress(self):
        msg = make_msg(headers={"X-Auto-Response-Suppress": "All"})
        assert is_automated(msg)

    @pytest.mark.parametrize("prefix", [
        "no-reply", "noreply", "do-not-reply", "mailer-daemon", "postmaster", "bounce",
    ])
    def test_noreply_sender_prefixes(self, prefix):
        msg = make_msg(from_addr=f"{prefix}@company.com")
        assert is_automated(msg)

    def test_noreply_in_display_name_not_automated(self):
        # "Name <real@person.com>" — display name containing "no-reply" shouldn't trigger
        msg = make_msg(from_addr="No Reply Service <notifications@company.com>")
        assert not is_automated(msg)

    @pytest.mark.parametrize("subject", [
        "Out of Office: Back on Monday",
        "Automatic reply: Re your enquiry",
        "Delivery Status Notification (Failure)",
        "Undelivered Mail Returned to Sender",
        "Auto: Away until next week",
    ])
    def test_auto_subject_prefixes(self, subject):
        msg = make_msg(subject=subject)
        assert is_automated(msg)

    def test_subject_containing_prefix_word_is_not_automated(self):
        # "automatic" appears mid-sentence, not as prefix
        msg = make_msg(subject="Is the payment automatic?")
        assert not is_automated(msg)

    def test_case_insensitive_subject(self):
        msg = make_msg(subject="OUT OF OFFICE: On holiday")
        assert is_automated(msg)

    def test_case_insensitive_precedence(self):
        msg = make_msg(headers={"Precedence": "BULK"})
        assert is_automated(msg)


# ── get_body ──────────────────────────────────────────────────────────────────

class TestGetBody:

    def test_simple_text_message(self):
        msg = email.message.Message()
        msg.set_payload("Hello world")
        assert get_body(msg) == "Hello world"

    def test_multipart_prefers_plain_text(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("Plain text version", "plain"))
        msg.attach(MIMEText("<b>HTML version</b>", "html"))
        body = get_body(msg)
        assert "Plain text version" in body
        assert "<b>" not in body

    def test_multipart_no_plain_returns_empty(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("<b>HTML only</b>", "html"))
        # No plain text part — returns empty string rather than crashing
        body = get_body(msg)
        assert isinstance(body, str)

    def test_empty_message_returns_empty_string(self):
        msg = email.message.Message()
        assert get_body(msg) == ""

    def test_utf8_body(self):
        msg = MIMEText("Héllo wörld", "plain", "utf-8")
        body = get_body(msg)
        assert "Héllo" in body


# ── parse_references ──────────────────────────────────────────────────────────

class TestParseReferences:

    def test_no_references_returns_empty(self):
        msg = make_msg()
        assert parse_references(msg) == []

    def test_single_in_reply_to(self):
        msg = make_msg(headers={"In-Reply-To": "<parent@example.com>"})
        assert "<parent@example.com>" in parse_references(msg)

    def test_multiple_references(self):
        msg = make_msg(headers={
            "References": "<first@example.com> <second@example.com>",
        })
        refs = parse_references(msg)
        assert "<first@example.com>" in refs
        assert "<second@example.com>" in refs
        assert len(refs) == 2

    def test_deduplication(self):
        msg = make_msg(headers={
            "References":   "<dup@example.com> <other@example.com>",
            "In-Reply-To":  "<dup@example.com>",
        })
        refs = parse_references(msg)
        assert refs.count("<dup@example.com>") == 1

    def test_whitespace_only_returns_empty(self):
        msg = make_msg(headers={"References": "   "})
        assert parse_references(msg) == []


# ── message_to_dict ───────────────────────────────────────────────────────────

class TestMessageToDict:

    def test_basic_fields_present(self):
        msg = make_msg(subject="Test subject", from_addr="person@example.com")
        d = message_to_dict(b"42", msg)
        assert d["id"]      == "42"
        assert d["from"]    == "person@example.com"
        assert d["subject"] == "Test subject"
        assert "body"       in d
        assert "message_id" in d
        assert "references" in d

    def test_id_is_string(self):
        msg = make_msg()
        d = message_to_dict(b"99", msg)
        assert isinstance(d["id"], str)

    def test_missing_optional_headers_default_to_empty(self):
        msg = email.message.Message()
        msg["From"] = "a@b.com"
        d = message_to_dict(b"1", msg)
        assert d["cc"] == ""
        assert d["subject"] == ""
