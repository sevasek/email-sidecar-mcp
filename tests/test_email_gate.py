"""
Tests for email_sidecar/email_gate.py

All tests mock imaplib so no real IMAP connection is made.
"""

from __future__ import annotations
import json
import sys
import os
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from email_sidecar.email_gate import check_unread, main


BASE_ENV = {
    "SIDECAR_EMAIL_IMAP_HOST": "imap.example.com",
    "SIDECAR_EMAIL_IMAP_PORT": "993",
    "SIDECAR_EMAIL_ADDRESS":   "agent@example.com",
    "SIDECAR_EMAIL_PASSWORD":  "secret",
}


def _imap_mock(unread_uids: list[bytes]) -> MagicMock:
    uid_str = b" ".join(unread_uids)
    m = MagicMock()
    m.search.return_value = ("OK", [uid_str])
    m.select.return_value = ("OK", [b""])
    m.login.return_value  = ("OK", [b""])
    m.logout.return_value = ("OK", [b""])
    return m


# ── check_unread ──────────────────────────────────────────────────────────────

class TestCheckUnread:
    def test_returns_zero_when_empty(self):
        mock = _imap_mock([])
        with patch("imaplib.IMAP4_SSL", return_value=mock):
            assert check_unread("imap.example.com", 993, "a@b.com", "pw") == 0

    def test_returns_count_when_mail_present(self):
        mock = _imap_mock([b"1", b"2", b"3"])
        with patch("imaplib.IMAP4_SSL", return_value=mock):
            assert check_unread("imap.example.com", 993, "a@b.com", "pw") == 3

    def test_opens_inbox_readonly(self):
        mock = _imap_mock([])
        with patch("imaplib.IMAP4_SSL", return_value=mock):
            check_unread("imap.example.com", 993, "a@b.com", "pw")
        mock.select.assert_called_once_with("INBOX", readonly=True)

    def test_logs_out_after_search(self):
        mock = _imap_mock([b"1"])
        with patch("imaplib.IMAP4_SSL", return_value=mock):
            check_unread("imap.example.com", 993, "a@b.com", "pw")
        mock.logout.assert_called_once()

    def test_logs_out_even_on_search_error(self):
        mock = _imap_mock([])
        mock.search.side_effect = Exception("boom")
        with patch("imaplib.IMAP4_SSL", return_value=mock):
            with pytest.raises(Exception):
                check_unread("imap.example.com", 993, "a@b.com", "pw")
        mock.logout.assert_called_once()

    def test_raises_on_connection_failure(self):
        with patch("imaplib.IMAP4_SSL", side_effect=ConnectionRefusedError("refused")):
            with pytest.raises(ConnectionRefusedError):
                check_unread("imap.example.com", 993, "a@b.com", "pw")


# ── main / wake gate output ───────────────────────────────────────────────────

class TestMain:
    def _run(self, env: dict, unread: int | None = 0, imap_error: Exception | None = None):
        """Run main() with patched env and IMAP, capture stdout lines."""
        mock = _imap_mock([str(i).encode() for i in range(unread or 0)])
        if imap_error:
            mock.login.side_effect = imap_error

        captured = StringIO()
        with patch.dict(os.environ, env, clear=True):
            with patch("imaplib.IMAP4_SSL", return_value=mock):
                with patch("sys.stdout", captured):
                    main()

        lines = [l for l in captured.getvalue().splitlines() if l.strip()]
        return lines

    def _gate(self, lines: list[str]) -> bool:
        """Parse wakeAgent from the last line."""
        return json.loads(lines[-1]).get("wakeAgent", True)

    def test_empty_inbox_closes_gate(self):
        lines = self._run(BASE_ENV, unread=0)
        assert self._gate(lines) is False

    def test_mail_present_opens_gate(self):
        lines = self._run(BASE_ENV, unread=2)
        assert self._gate(lines) is True

    def test_mail_present_includes_count_in_context(self):
        lines = self._run(BASE_ENV, unread=3)
        context = "\n".join(lines[:-1])
        assert "3" in context

    def test_missing_host_opens_gate(self):
        env = {**BASE_ENV, "SIDECAR_EMAIL_IMAP_HOST": ""}
        lines = self._run(env)
        assert self._gate(lines) is True

    def test_missing_address_opens_gate(self):
        env = {**BASE_ENV, "SIDECAR_EMAIL_ADDRESS": ""}
        lines = self._run(env)
        assert self._gate(lines) is True

    def test_missing_password_opens_gate(self):
        env = {**BASE_ENV, "SIDECAR_EMAIL_PASSWORD": ""}
        lines = self._run(env)
        assert self._gate(lines) is True

    def test_imap_error_opens_gate(self):
        lines = self._run(BASE_ENV, imap_error=OSError("connection refused"))
        assert self._gate(lines) is True

    def test_imap_error_includes_error_message(self):
        lines = self._run(BASE_ENV, imap_error=OSError("connection refused"))
        context = "\n".join(lines[:-1])
        assert "connection refused" in context.lower() or "failed" in context.lower()

    def test_last_line_is_always_valid_json(self):
        for unread, err in [(0, None), (1, None), (0, OSError("x"))]:
            lines = self._run(BASE_ENV, unread=unread, imap_error=err)
            gate = json.loads(lines[-1])
            assert "wakeAgent" in gate
