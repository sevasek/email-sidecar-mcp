# email-sidecar-mcp

A small, self-contained MCP server that gives an LLM agent controlled access
to one email inbox via `imaplib`/`smtplib` — no vendor email API, no OAuth
app registration, just IMAP/SMTP credentials.

It was extracted from [basic-admin-agent](https://github.com/sevasek/basic-admin-agent),
which is its primary consumer today (see "Used by" below), but it has no
dependency on that project or on Hermes specifically — anything that speaks
MCP over stdio can use it.

## Why a sidecar instead of letting the agent frameworks own email

Two things stay out of the LLM's hands by design:

- **Send approval.** `queue_draft` / `approve_send` / `send_reply` implement
  a code-enforced lock (`send_lock.py`) — a drafted reply cannot be sent
  until a separate, explicit `approve_send` call has happened. An agent
  cannot talk itself into skipping the approval step.
- **Noise filtering.** `fetch_unread` silently archives automated mail
  (auto-replies, bounces, bulk mail) before the agent ever sees it, so the
  agent's context isn't spent triaging spam.

## Tools exposed

| Tool | Purpose |
|---|---|
| `fetch_unread` | Unread emails with up to 3 messages of thread context and any attachments saved to disk. Automated mail silently archived. |
| `queue_draft` | Register a posted draft as pending owner approval. |
| `approve_send` | Approve a queued draft — call only from the owner-reply/approval path. |
| `send_reply` | SMTP send with explicit RFC 5322 `Date` header and optional attachments. Refuses unsent/unapproved drafts. |
| `discard_draft` | Drop a queued draft without sending it. |
| `mark_read` | Mark a message as Seen. |
| `archive` | Move a message to archive without replying. |

### Attachments

`fetch_unread` saves each inbound attachment under
`$HERMES_HOME/attachments/<email-id>/<filename>` and returns its metadata —
`{filename, content_type, size, path}` — in that message's `attachments`
list. Nothing is inlined as base64 into the tool response; the path is a
local file the calling process (Hermes/Willow) reads directly, since the
stdio MCP transport means it already shares this filesystem.

`send_reply` accepts an `attachments` arg — a list of local file paths
(an inbound attachment's `path`, or any other file readable on this host)
to attach to the outgoing message. A missing path fails the send rather
than going out silently without it.

## Install

```bash
pip install "email-sidecar-mcp @ git+https://github.com/sevasek/email-sidecar-mcp.git"
```

Or for local development against a consuming project:

```bash
pip install -e /path/to/email-sidecar-mcp
```

## Configuration

Copy `.env.example` to `.env` and fill in IMAP/SMTP credentials. See that
file for the full variable list and which module reads each one — the
`SIDECAR_EMAIL_*` names exist alongside the plain `EMAIL_*` names because
`email_gate.py` (a pre-LLM inbox check) intentionally uses different names
so an orchestrator that auto-detects `EMAIL_*` env vars doesn't try to wire
its own email gateway on top of this one.

## Running standalone

```bash
email-sidecar-mcp          # after pip install, runs the MCP stdio server
# or
python -m email_sidecar.server
```

## Wiring into an MCP client (e.g. Hermes Agent)

```yaml
mcp_servers:
  email_sidecar:
    command: "email-sidecar-mcp"
    env:
      EMAIL_ADDRESS: "${SIDECAR_EMAIL_ADDRESS}"
      EMAIL_PASSWORD: "${SIDECAR_EMAIL_PASSWORD}"
      EMAIL_IMAP_HOST: "${SIDECAR_EMAIL_IMAP_HOST}"
      EMAIL_IMAP_PORT: "${SIDECAR_EMAIL_IMAP_PORT}"
      EMAIL_SMTP_HOST: "${SIDECAR_EMAIL_SMTP_HOST}"
      EMAIL_SMTP_PORT: "${SIDECAR_EMAIL_SMTP_PORT}"
      EMAIL_SMTP_USE_STARTTLS: "${SIDECAR_EMAIL_SMTP_USE_STARTTLS}"
      EMAIL_SMTP_USE_IMPLICIT_SSL: "${SIDECAR_EMAIL_SMTP_USE_IMPLICIT_SSL}"
      EMAIL_SENDER_NAME: "${SIDECAR_EMAIL_SENDER_NAME}"
    include:
      - fetch_unread
      - queue_draft
      - approve_send
      - discard_draft
      - send_reply
      - mark_read
      - archive
```

## Used by

- [basic-admin-agent](https://github.com/sevasek/basic-admin-agent) — a
  deployable Hermes Agent configured as a small-business inbox assistant.
  It installs this package (see its README's "Dependencies" section) and
  also runs `email_sidecar.email_gate` as a pre-cron IMAP check to avoid
  waking the LLM on empty polls.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

All tests mock `imaplib`/`smtplib` — no real inbox is touched, no network
credentials are required to run the suite.
