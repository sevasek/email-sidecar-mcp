#!/usr/bin/env python3
"""IMAP wake gate for the Hermes email-poll cron job.

Hermes reads the last stdout line of a cron pre-script as a JSON gate:
  {"wakeAgent": false}  →  skip the LLM entirely (zero token cost)
  {"wakeAgent": true}   →  wake the agent; inject all prior stdout as context

Empty inbox → gate closes, LLM skipped.
Mail present → gate opens, agent gets a count hint and runs normally.
Any IMAP error → gate opens so the agent can surface the problem.
"""

from __future__ import annotations
import imaplib
import json
import os
import sys


def check_unread(host: str, port: int, address: str, password: str) -> int:
    """Return the number of unread messages in INBOX. Raises on connection failure."""
    m = imaplib.IMAP4_SSL(host, port)
    try:
        m.login(address, password)
        m.select("INBOX", readonly=True)
        _, data = m.search(None, "UNSEEN")
        uids = data[0].split() if data[0] else []
        return len(uids)
    finally:
        try:
            m.logout()
        except Exception:
            pass


def main() -> None:
    host    = os.environ.get("SIDECAR_EMAIL_IMAP_HOST", "")
    port    = int(os.environ.get("SIDECAR_EMAIL_IMAP_PORT", "993"))
    address = os.environ.get("SIDECAR_EMAIL_ADDRESS", "")
    password = os.environ.get("SIDECAR_EMAIL_PASSWORD", "")

    if not (host and address and password):
        # Missing config — wake the agent so it can report the misconfiguration.
        print(json.dumps({"wakeAgent": True}))
        return

    try:
        count = check_unread(host, port, address, password)
    except Exception as exc:
        # On IMAP error, wake the agent so the error surfaces to the owner.
        print(f"IMAP pre-check failed: {exc}")
        print(json.dumps({"wakeAgent": True}))
        return

    if count == 0:
        print(json.dumps({"wakeAgent": False}))
    else:
        print(f"{count} unread email(s) waiting. Call fetch_unread and process each per SOUL.md.")
        print(json.dumps({"wakeAgent": True}))


if __name__ == "__main__":
    main()
