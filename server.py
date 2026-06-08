"""
Email sidecar MCP server.

Exposes four tools to Hermes Agent over stdio:
  - fetch_unread   : returns unread emails with headers and full thread context
  - send_reply     : sends a reply via SMTP (adds Date header explicitly)
  - mark_read      : marks a message as read after processing
  - archive        : moves a message to the archive folder without replying

Header-level reply-loop filtering runs here, before Hermes sees any email.
Filtered emails are silently archived — the agent never receives them.

Usage: python server.py  (Hermes spawns this as a subprocess via config.yaml)

TODO: implement using `mcp` SDK (pip install mcp) and imaplib/smtplib.
      See DESIGN_BRIEF.md §4.5 for the full tool spec.
"""

# Stub — implementation pending.
raise NotImplementedError("email_sidecar/server.py is not yet implemented.")
