#!/usr/bin/env python3
"""
Email sidecar MCP server for basic-admin-agent.

Thin MCP wrapper around email_sidecar/logic.py.
Exposes four tools to Hermes Agent over stdio transport:

  fetch_unread   Unread emails with thread context. Automated mail silently archived.
  send_reply     SMTP send with explicit RFC 5322 Date header.
  mark_read      Mark a message as Seen.
  archive        Move a message to archive without replying.

Usage: Hermes spawns this automatically via mcp_servers in config/config.yaml.
"""

from __future__ import annotations
from mcp.server.fastmcp import FastMCP

from email_sidecar.logic import (
    do_archive,
    do_fetch_unread,
    do_mark_read,
    do_send_reply,
)

mcp = FastMCP("email-sidecar")


@mcp.tool()
def fetch_unread() -> list[dict]:
    """
    Fetch all unread emails from the inbox.

    Automated emails (auto-replies, bounces, bulk mail) are silently archived —
    Hermes never sees them.

    Each returned email contains:
      id, from, to, cc, subject, date, message_id, references, body, thread

    Use 'id' in mark_read / archive / send_reply.
    Use 'message_id' as 'in_reply_to' when calling send_reply.
    'thread' is a list of up to 3 prior messages for context (chronological order).
    """
    return do_fetch_unread()


@mcp.tool()
def send_reply(
    to: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: list[str] | None = None,
) -> dict:
    """
    Send an email reply via SMTP.

    Args:
        to           Recipient email address.
        subject      Subject line. Include "Re: " prefix for replies.
        body         Plain-text message body.
        in_reply_to  message_id from fetch_unread (for threading).
        references   references list from fetch_unread (for threading).

    Returns {"ok": true} or {"ok": false, "error": "..."}.
    """
    return do_send_reply(to, subject, body, in_reply_to, references)


@mcp.tool()
def mark_read(message_id: str) -> dict:
    """
    Mark an email as read.

    Args:
        message_id  The 'id' field from fetch_unread.

    Returns {"ok": true} or {"ok": false, "error": "..."}.
    """
    return do_mark_read(message_id)


@mcp.tool()
def archive(message_id: str) -> dict:
    """
    Move an email to the archive folder without replying.

    Use for spam, cold outreach, and owner-discarded emails.

    Args:
        message_id  The 'id' field from fetch_unread.

    Returns {"ok": true} or {"ok": false, "error": "..."}.
    """
    return do_archive(message_id)


if __name__ == "__main__":
    mcp.run()
