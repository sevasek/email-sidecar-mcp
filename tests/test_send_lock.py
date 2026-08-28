"""
Tests for the code-level send lock (email_sidecar/send_lock.py) and its
enforcement in email_sidecar/logic.py's do_send_reply.

Each test points HERMES_HOME at a fresh tmp_path so lock state never leaks
between tests or into a real /opt/data.
"""

from __future__ import annotations
import fcntl
import json
import sys
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("EMAIL_ADDRESS",    "test@example.com")
os.environ.setdefault("EMAIL_PASSWORD",   "password")
os.environ.setdefault("EMAIL_IMAP_HOST",  "imap.example.com")
os.environ.setdefault("EMAIL_SMTP_HOST",  "smtp.example.com")

from email_sidecar import send_lock
from email_sidecar.logic import do_send_reply


@pytest.fixture(autouse=True)
def isolated_lock_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


# ── send_lock module ─────────────────────────────────────────────────────────

class TestSendLock:

    def test_queue_then_not_approved(self):
        send_lock.queue_draft("42")
        assert not send_lock.is_approved("42")

    def test_queue_then_approve(self):
        send_lock.queue_draft("42")
        result = send_lock.approve_send("42")
        assert result == {"ok": True}
        assert send_lock.is_approved("42")

    def test_approve_without_queue_fails(self):
        result = send_lock.approve_send("no-such-id")
        assert result["ok"] is False

    def test_unqueued_id_is_not_approved(self):
        assert not send_lock.is_approved("never-queued")

    def test_discard_removes_queued_entry(self):
        send_lock.queue_draft("42")
        send_lock.discard("42")
        assert not send_lock.is_approved("42")

    def test_discard_missing_entry_fails(self):
        result = send_lock.discard("missing")
        assert result["ok"] is False

    def test_queue_missing_key_fails(self):
        assert send_lock.queue_draft("")["ok"] is False

    def test_lock_persists_across_loads(self, tmp_path):
        send_lock.queue_draft("42")
        send_lock.approve_send("42")
        # Simulate a fresh process re-reading the same HERMES_HOME
        assert send_lock.is_approved("42")
        assert (tmp_path / "send_locks.json").exists()


class TestCrossProcessLocking:
    """queue_draft and approve_send run as separate OS processes in
    production (Step 4 drafting vs Step 5 owner-approval — see
    config.yaml's mcp_servers entry), each with its own module-level
    _write_lock. A plain threading.Lock can't protect against that; only
    an OS-level flock on the shared file can. These tests hold the lock
    from one thread and probe it with a *second, independent* file handle
    that bypasses send_lock's Python-level lock entirely — mirroring what
    a second process would see — to prove the exclusion is enforced by
    flock() and not just by in-process synchronization.
    """

    def test_locked_store_blocks_a_second_independent_handle(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_lock():
            with send_lock._locked_store() as data:
                data["holding"] = {"state": "queued"}
                entered.set()
                release.wait(timeout=5)

        t = threading.Thread(target=hold_lock)
        t.start()
        assert entered.wait(timeout=5)

        path = send_lock._lock_path()
        lock_path = path.with_suffix(path.suffix + ".lock")
        with open(lock_path, "a+") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

        release.set()
        t.join(timeout=5)

    def test_lock_is_released_after_the_store_exits(self):
        with send_lock._locked_store() as data:
            data["42"] = {"state": "queued"}

        path = send_lock._lock_path()
        lock_path = path.with_suffix(path.suffix + ".lock")
        with open(lock_path, "a+") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)

    def test_save_is_atomic_no_partial_writes_left_behind(self, tmp_path):
        send_lock.queue_draft("42")
        assert list(tmp_path.glob(".send_locks.*.tmp")) == []
        on_disk = json.loads((tmp_path / "send_locks.json").read_text())
        assert on_disk["42"]["state"] == "queued"


# ── do_send_reply gating ─────────────────────────────────────────────────────

def _smtp_mock():
    server = MagicMock()
    server.__enter__.return_value = server
    return server


class TestSendReplyLockGate:

    def test_send_without_queue_is_refused(self):
        result = do_send_reply("a@b.com", "Re: hi", "body", email_id="1")
        assert result["ok"] is False
        assert "send_locked" in result["error"]

    def test_send_queued_but_not_approved_is_refused(self):
        send_lock.queue_draft("1")
        result = do_send_reply("a@b.com", "Re: hi", "body", email_id="1")
        assert result["ok"] is False
        assert "send_locked" in result["error"]

    def test_send_missing_email_id_is_refused(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        result = do_send_reply("a@b.com", "Re: hi", "body")
        assert result["ok"] is False

    def test_send_approved_succeeds_and_consumes_lock(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        with patch("smtplib.SMTP_SSL", return_value=_smtp_mock()):
            result = do_send_reply("a@b.com", "Re: hi", "body", email_id="1")
        assert result == {"ok": True}
        # Lock consumed — replaying the same call must fail now.
        assert not send_lock.is_approved("1")
        with patch("smtplib.SMTP_SSL", return_value=_smtp_mock()):
            replay = do_send_reply("a@b.com", "Re: hi", "body", email_id="1")
        assert replay["ok"] is False

    def test_approval_for_one_email_id_does_not_unlock_another(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        result = do_send_reply("a@b.com", "Re: hi", "body", email_id="2")
        assert result["ok"] is False


# ── CC support ──────────────────────────────────────────────────────────────

class TestCcSupport:

    def test_cc_recipients_receive_the_envelope_and_the_header(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            result = do_send_reply(
                "craig@example.com", "Re: hi", "body", email_id="1",
                cc=["david@example.com"],
            )
        assert result == {"ok": True}
        args, _ = server.sendmail.call_args
        from_addr, envelope_recipients, raw_message = args
        assert envelope_recipients == ["craig@example.com", "david@example.com"]
        assert b"Cc: david@example.com" in raw_message

    def test_multiple_cc_recipients(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            do_send_reply(
                "craig@example.com", "Re: hi", "body", email_id="1",
                cc=["david@example.com", "sashka@example.com"],
            )
        _, envelope_recipients, raw_message = server.sendmail.call_args[0]
        assert envelope_recipients == [
            "craig@example.com", "david@example.com", "sashka@example.com",
        ]
        assert b"Cc: david@example.com, sashka@example.com" in raw_message

    def test_no_cc_omits_the_header_and_only_envelopes_to(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        server = _smtp_mock()
        with patch("smtplib.SMTP_SSL", return_value=server):
            do_send_reply("craig@example.com", "Re: hi", "body", email_id="1")
        _, envelope_recipients, raw_message = server.sendmail.call_args[0]
        assert envelope_recipients == ["craig@example.com"]
        assert b"Cc:" not in raw_message


# ── append_to_sent (Sent-folder visibility, separate from delivery) ───────────

class TestAppendToSent:

    def test_successful_send_appends_a_copy_to_sent(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        imap_conn = MagicMock()
        imap_conn.append.return_value = ("OK", [b""])
        with patch("smtplib.SMTP_SSL", return_value=_smtp_mock()), \
             patch("email_sidecar.logic.imap_connect", return_value=imap_conn):
            result = do_send_reply("a@b.com", "Re: hi", "body", email_id="1")
        assert result == {"ok": True}
        imap_conn.append.assert_called_once()
        folder = imap_conn.append.call_args[0][0]
        assert folder == "Sent"
        imap_conn.logout.assert_called_once()

    def test_imap_append_failure_does_not_fail_the_send(self):
        """The SMTP send has already gone out by the time we try to file a
        Sent copy — a broken/unreachable IMAP connection must never turn an
        otherwise-successful send into a reported failure."""
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        with patch("smtplib.SMTP_SSL", return_value=_smtp_mock()), \
             patch("email_sidecar.logic.imap_connect", side_effect=OSError("unreachable")):
            result = do_send_reply("a@b.com", "Re: hi", "body", email_id="1")
        assert result == {"ok": True}

    def test_falls_back_to_next_folder_name_if_first_fails(self):
        send_lock.queue_draft("1")
        send_lock.approve_send("1")
        imap_conn = MagicMock()
        imap_conn.append.side_effect = [("NO", [b"no such mailbox"]), ("OK", [b""])]
        with patch("smtplib.SMTP_SSL", return_value=_smtp_mock()), \
             patch("email_sidecar.logic.imap_connect", return_value=imap_conn):
            result = do_send_reply("a@b.com", "Re: hi", "body", email_id="1")
        assert result == {"ok": True}
        assert imap_conn.append.call_count == 2
