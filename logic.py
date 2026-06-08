"""
Pure email logic for the basic-admin-agent sidecar.

No MCP dependency here — importable standalone and in tests.
server.py wraps these functions as MCP tools.
"""

from __future__ import annotations
import email
import imaplib
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

# ── Config from environment ───────────────────────────────────────────────────

EMAIL_ADDRESS      = os.environ.get("EMAIL_ADDRESS",              "")
EMAIL_PASSWORD     = os.environ.get("EMAIL_PASSWORD",             "")
IMAP_HOST          = os.environ.get("EMAIL_IMAP_HOST",           "")
IMAP_PORT          = int(os.environ.get("EMAIL_IMAP_PORT",       "993"))
SMTP_HOST          = os.environ.get("EMAIL_SMTP_HOST",           "")
SMTP_PORT          = int(os.environ.get("EMAIL_SMTP_PORT",      "465"))
SMTP_USE_STARTTLS    = os.environ.get("EMAIL_SMTP_USE_STARTTLS",    "")
SMTP_USE_IMPLICIT_SSL = os.environ.get("EMAIL_SMTP_USE_IMPLICIT_SSL", "")
SENDER_NAME        = os.environ.get("EMAIL_SENDER_NAME",         "")

# ── Reply-loop detection ──────────────────────────────────────────────────────

AUTO_SENDER_PREFIXES = (
    "no-reply", "noreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce",
)

AUTO_SUBJECT_PREFIXES = (
    "delivery status notification",
    "undelivered mail",
    "out of office",
    "automatic reply",
    "auto:",
    "autosvar:",
    "automatische antwort",
    "réponse automatique",
    "abwesenheitsnotiz",
)


def is_automated(msg: email.message.Message) -> bool:
    """Return True if this email was sent by an automated system."""
    # RFC header signals
    if msg.get("Auto-Submitted", "no").strip().lower() != "no":
        return True
    if msg.get("Precedence", "").strip().lower() in ("bulk", "list", "junk"):
        return True
    if msg.get("X-Autoreply") or msg.get("X-Auto-Response-Suppress"):
        return True

    # Sender local-part check
    from_header = msg.get("From", "").lower()
    addr = from_header.split("<")[-1].split("@")[0].strip().rstrip(">")
    if any(addr.startswith(p) for p in AUTO_SENDER_PREFIXES):
        return True

    # Subject check
    subject = msg.get("Subject", "").lower().strip()
    if any(subject.startswith(p) for p in AUTO_SUBJECT_PREFIXES):
        return True

    return False


# ── Message helpers ───────────────────────────────────────────────────────────

def get_body(msg: email.message.Message) -> str:
    """Extract the best plain-text body from a possibly multipart message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition  = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(charset, errors="replace")
    return ""


def parse_references(msg: email.message.Message) -> list[str]:
    """Return a deduplicated list of Message-IDs from References + In-Reply-To."""
    raw = (msg.get("References", "") + " " + msg.get("In-Reply-To", "")).strip()
    seen, result = set(), []
    for ref in raw.split():
        ref = ref.strip()
        if ref and ref not in seen:
            seen.add(ref)
            result.append(ref)
    return result


def message_to_dict(uid: bytes, msg: email.message.Message) -> dict:
    """Convert a parsed email.message.Message to a plain dict for Hermes."""
    return {
        "id":         uid.decode(),
        "from":       msg.get("From", ""),
        "to":         msg.get("To", ""),
        "cc":         msg.get("Cc", ""),
        "subject":    msg.get("Subject", ""),
        "date":       msg.get("Date", ""),
        "message_id": msg.get("Message-ID", "").strip(),
        "references": parse_references(msg),
        "body":       get_body(msg),
    }


# ── IMAP helpers ──────────────────────────────────────────────────────────────

def imap_connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    return conn


def move_to_archive(conn: imaplib.IMAP4_SSL, uid: bytes) -> None:
    """Move a message to an archive folder; fall back to mark-read if none found."""
    for folder in ("[Gmail]/All Mail", "Archive", "INBOX.Archive", "Archived", "Trash"):
        try:
            result, _ = conn.copy(uid, folder)
        except Exception:
            continue
        if result == "OK":
            conn.store(uid, "+FLAGS", "\\Deleted")
            conn.expunge()
            return
    conn.store(uid, "+FLAGS", "\\Seen")


def fetch_thread_context(
    conn: imaplib.IMAP4_SSL,
    references: list[str],
    limit: int = 3,
) -> list[dict]:
    """Fetch up to `limit` ancestor messages for context, in chronological order."""
    thread = []
    for ref in reversed(references[-limit:]):
        ref_clean = ref.strip("<>")
        typ, data = conn.search(None, f'HEADER Message-ID "{ref_clean}"')
        if typ != "OK" or not data[0]:
            continue
        for uid in data[0].split()[:1]:
            typ2, msg_data = conn.fetch(uid, "(RFC822)")
            if typ2 != "OK" or not msg_data or not msg_data[0]:
                continue
            ancestor = email.message_from_bytes(msg_data[0][1])
            thread.append({
                "from":    ancestor.get("From", ""),
                "date":    ancestor.get("Date", ""),
                "subject": ancestor.get("Subject", ""),
                "body":    get_body(ancestor)[:800],
            })
    return list(reversed(thread))


# ── Core tool implementations ─────────────────────────────────────────────────

def do_fetch_unread() -> list[dict]:
    """Fetch unread emails; silently archive automated ones."""
    results = []
    conn = imap_connect()
    try:
        conn.select("INBOX")
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK" or not data[0]:
            return []
        for uid in data[0].split():
            typ2, msg_data = conn.fetch(uid, "(RFC822)")
            if typ2 != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if is_automated(msg):
                move_to_archive(conn, uid)
                continue
            record = message_to_dict(uid, msg)
            record["thread"] = fetch_thread_context(conn, record["references"])
            results.append(record)
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass
    return results


def do_send_reply(
    to: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: list[str] | None = None,
) -> dict:
    """Send a reply via SMTP with an explicit Date header."""
    try:
        msg = MIMEMultipart("alternative")
        from_header = EMAIL_ADDRESS if not SENDER_NAME else f"{SENDER_NAME} <{EMAIL_ADDRESS}>"
        msg["From"]    = from_header
        msg["To"]      = to
        msg["Subject"]    = subject
        msg["Date"]       = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=EMAIL_ADDRESS.split("@")[-1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            all_refs = references + (
                [in_reply_to] if in_reply_to and in_reply_to not in references else []
            )
            msg["References"] = " ".join(all_refs)
        msg.attach(MIMEText(body, "plain", "utf-8"))

        use_implicit = SMTP_USE_IMPLICIT_SSL == "1" or (
            SMTP_PORT == 465 and SMTP_USE_STARTTLS != "1"
        )
        use_starttls  = SMTP_USE_STARTTLS == "1" or (
            SMTP_PORT == 587 and SMTP_USE_IMPLICIT_SSL != "1"
        )
        context = ssl.create_default_context()

        if use_implicit:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
                server.ehlo()
                server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                server.sendmail(EMAIL_ADDRESS, [to], msg.as_bytes())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                if use_starttls:
                    server.starttls(context=context)
                server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                server.sendmail(EMAIL_ADDRESS, [to], msg.as_bytes())

        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def do_mark_read(message_id: str) -> dict:
    """Mark an email as \\Seen."""
    try:
        conn = imap_connect()
        conn.select("INBOX")
        conn.store(message_id.encode(), "+FLAGS", "\\Seen")
        conn.close()
        conn.logout()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def do_archive(message_id: str) -> dict:
    """Move an email to the archive folder."""
    try:
        conn = imap_connect()
        conn.select("INBOX")
        move_to_archive(conn, message_id.encode())
        conn.close()
        conn.logout()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
