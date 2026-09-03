"""
Tests for attachment support in email_sidecar/logic.py — receiving
(extract_attachments/save_attachments, wired into fetch_unread via
message_to_dict) and sending (do_send_reply's `attachments` arg).

Follows the isolation pattern from test_send_lock.py: HERMES_HOME points at
a fresh tmp_path per test so nothing touches a real /opt/data, and SMTP is
mocked so no network call happens.
"""

from __future__ import annotations
import sys
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("EMAIL_ADDRESS",    "test@example.com")
os.environ.setdefault("EMAIL_PASSWORD",   "password")
os.environ.setdefault("EMAIL_IMAP_HOST",  "imap.example.com")
os.environ.setdefault("EMAIL_SMTP_HOST",  "smtp.example.com")

from email_sidecar import send_lock
from email_sidecar.logic import (
    do_send_reply,
    extract_attachments,
    get_body,
    message_to_dict,
    save_attachments,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _smtp_mock():
    server = MagicMock()
    server.__enter__.return_value = server
    return server


def make_msg_with_attachment(
    filename="invoice.pdf",
    content=b"%PDF-1.4 fake pdf bytes",
    content_type="application/pdf",
    disposition="attachment",
) -> MIMEMultipart:
    msg = MIMEMultipart("mixed")
    msg["From"] = "customer@example.com"
    msg["Subject"] = "Here's the file"
    msg.attach(MIMEText("See attached.", "plain"))
    maintype, subtype = content_type.split("/", 1)
    from email.mime.base import MIMEBase
    from email import encoders
    part = MIMEBase(maintype, subtype)
    part.set_payload(content)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", disposition, filename=filename)
    msg.attach(part)
    return msg


# ── extract_attachments ─────────────────────────────────────────────────────

class TestExtractAttachments:

    def test_no_attachments_on_plain_message(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("Just text", "plain"))
        assert extract_attachments(msg) == []

    def test_non_multipart_message_has_no_attachments(self):
        msg = MIMEText("Just text", "plain")
        assert extract_attachments(msg) == []

    def test_finds_explicit_attachment(self):
        msg = make_msg_with_attachment()
        atts = extract_attachments(msg)
        assert len(atts) == 1
        assert atts[0]["filename"] == "invoice.pdf"
        assert atts[0]["content_type"] == "application/pdf"
        assert atts[0]["size"] == len(b"%PDF-1.4 fake pdf bytes")
        assert atts[0]["payload"] == b"%PDF-1.4 fake pdf bytes"

    def test_finds_inline_attachment_with_filename(self):
        # Some clients mark inline images "inline" rather than "attachment"
        # but still carry a filename — should still surface.
        msg = make_msg_with_attachment(
            filename="logo.png", content_type="image/png", disposition="inline",
        )
        atts = extract_attachments(msg)
        assert len(atts) == 1
        assert atts[0]["filename"] == "logo.png"

    def test_body_part_is_not_treated_as_attachment(self):
        msg = make_msg_with_attachment()
        atts = extract_attachments(msg)
        assert all(a["filename"] != "" for a in atts)
        assert len(atts) == 1  # the text/plain body part is excluded

    def test_get_body_still_ignores_attachment_part(self):
        msg = make_msg_with_attachment()
        assert get_body(msg) == "See attached."

    def test_oversized_part_is_flagged_and_not_decoded_as_payload(self, monkeypatch):
        monkeypatch.setenv("EMAIL_MAX_ATTACHMENT_BYTES", "16")
        content = b"x" * 64
        msg = make_msg_with_attachment(content=content)
        atts = extract_attachments(msg)
        assert len(atts) == 1
        assert atts[0]["payload"] is None
        assert "exceeds" in atts[0]["error"]
        assert atts[0]["size"] > 16


# ── save_attachments ─────────────────────────────────────────────────────────

class TestSaveAttachments:

    def test_no_attachments_returns_empty_list(self):
        assert save_attachments("42", []) == []

    def test_saves_file_and_returns_metadata(self, tmp_path):
        saved = save_attachments("42", [{
            "filename": "notes.txt", "content_type": "text/plain",
            "size": 5, "payload": b"hello",
        }])
        assert len(saved) == 1
        entry = saved[0]
        assert entry["filename"] == "notes.txt"
        assert entry["content_type"] == "text/plain"
        assert entry["size"] == 5
        assert "payload" not in entry
        saved_path = tmp_path / "attachments" / "42" / "notes.txt"
        assert saved_path.exists()
        assert saved_path.read_bytes() == b"hello"
        assert entry["path"] == str(saved_path)

    def test_sanitizes_path_traversal_in_filename(self, tmp_path):
        saved = save_attachments("42", [{
            "filename": "../../etc/evil.txt", "content_type": "text/plain",
            "size": 4, "payload": b"evil",
        }])
        out_dir = tmp_path / "attachments" / "42"
        assert saved[0]["path"] == str(out_dir / "evil.txt")
        assert not (tmp_path / "etc").exists()

    def test_collision_gets_a_distinct_filename(self):
        saved = save_attachments("42", [
            {"filename": "a.txt", "content_type": "text/plain", "size": 1, "payload": b"1"},
            {"filename": "a.txt", "content_type": "text/plain", "size": 1, "payload": b"2"},
        ])
        paths = {entry["path"] for entry in saved}
        assert len(paths) == 2
        contents = {open(p, "rb").read() for p in paths}
        assert contents == {b"1", b"2"}

    def test_oversized_payload_is_not_written(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EMAIL_MAX_ATTACHMENT_BYTES", "8")
        saved = save_attachments("42", [{
            "filename": "big.bin", "content_type": "application/octet-stream",
            "size": 32, "payload": b"x" * 32,
        }])
        assert len(saved) == 1
        assert "path" not in saved[0]
        assert "exceeds" in saved[0]["error"]
        assert not (tmp_path / "attachments" / "42" / "big.bin").exists()

    def test_extract_error_is_passed_through_without_writing(self, tmp_path):
        saved = save_attachments("42", [{
            "filename": "huge.pdf", "content_type": "application/pdf",
            "size": 99, "payload": None, "error": "attachment exceeds 16 byte limit",
        }])
        assert saved[0]["error"] == "attachment exceeds 16 byte limit"
        assert "path" not in saved[0]
        assert list((tmp_path / "attachments" / "42").iterdir()) == []

    def test_ok_part_still_saved_when_sibling_is_oversized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EMAIL_MAX_ATTACHMENT_BYTES", "8")
        saved = save_attachments("42", [
            {"filename": "ok.txt", "content_type": "text/plain",
             "size": 2, "payload": b"hi"},
            {"filename": "big.bin", "content_type": "application/octet-stream",
             "size": 32, "payload": b"x" * 32},
        ])
        assert saved[0]["path"] == str(tmp_path / "attachments" / "42" / "ok.txt")
        assert (tmp_path / "attachments" / "42" / "ok.txt").read_bytes() == b"hi"
        assert "path" not in saved[1]
        assert "exceeds" in saved[1]["error"]


# ── message_to_dict ───────────────────────────────────────────────────────────

class TestMessageToDictAttachments:

    def test_attachments_key_defaults_to_empty_list(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("hi", "plain"))
        d = message_to_dict(b"1", msg)
        assert d["attachments"] == []


# ── do_send_reply attachments ─────────────────────────────────────────────────

class TestSendReplyAttachments:

    def test_attaches_a_local_file(self, tmp_path):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        att_path = tmp_path / "attachments" / "1" / "report.txt"
        att_path.parent.mkdir(parents=True)
        att_path.write_bytes(b"quarterly numbers")

        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            result = do_send_reply(
                "a@b.com", "Re: hi", "body", email_id="1",
                attachments=[str(att_path)],
            )
        assert result == {"ok": True}
        _, _, raw_message = server.sendmail.call_args[0]
        assert b"report.txt" in raw_message
        assert b"quarterly numbers" not in raw_message  # base64-encoded, not raw
        import base64
        assert base64.b64encode(b"quarterly numbers") in raw_message

    def test_missing_attachment_fails_the_whole_send(self, tmp_path):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        missing_path = str(tmp_path / "attachments" / "1" / "does-not-exist.pdf")

        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            result = do_send_reply(
                "a@b.com", "Re: hi", "body", email_id="1",
                attachments=[missing_path],
            )
        assert result["ok"] is False
        assert "attachment not found" in result["error"]
        server.sendmail.assert_not_called()
        # Failure happens before the send — the approval isn't consumed,
        # so a corrected retry with the same email_id can still go through.
        assert send_lock.is_approved("1")

    def test_path_outside_attachments_dir_is_rejected(self, tmp_path):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        secret = tmp_path / "host-secret.env"
        secret.write_text("EMAIL_PASSWORD=hunter2")

        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            result = do_send_reply(
                "a@b.com", "Re: hi", "body", email_id="1",
                attachments=[str(secret)],
            )
        assert result["ok"] is False
        assert "not allowed" in result["error"]
        server.sendmail.assert_not_called()
        assert send_lock.is_approved("1")

    def test_relative_escape_outside_attachments_dir_is_rejected(self, tmp_path):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        (tmp_path / "attachments").mkdir()
        secret = tmp_path / "secret.env"
        secret.write_text("EMAIL_PASSWORD=hunter2")
        escaped = str(tmp_path / "attachments" / ".." / "secret.env")

        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            result = do_send_reply(
                "a@b.com", "Re: hi", "body", email_id="1",
                attachments=[escaped],
            )
        assert result["ok"] is False
        assert "not allowed" in result["error"]
        server.sendmail.assert_not_called()

    def test_symlink_escaping_attachments_dir_is_rejected(self, tmp_path):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        secret = tmp_path / "credentials"
        secret.write_text("password")
        link_dir = tmp_path / "attachments" / "1"
        link_dir.mkdir(parents=True)
        link = link_dir / "leak"
        link.symlink_to(secret)

        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            result = do_send_reply(
                "a@b.com", "Re: hi", "body", email_id="1",
                attachments=[str(link)],
            )
        assert result["ok"] is False
        assert "not allowed" in result["error"]
        server.sendmail.assert_not_called()

    def test_no_attachments_produces_no_attachment_part(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            do_send_reply("a@b.com", "Re: hi", "body", email_id="1")
        _, _, raw_message = server.sendmail.call_args[0]
        assert b"Content-Disposition: attachment" not in raw_message
