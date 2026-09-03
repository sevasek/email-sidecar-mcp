"""
Pure email logic for the basic-admin-agent sidecar.

No MCP dependency here — importable standalone and in tests.
server.py wraps these functions as MCP tools.
"""

from __future__ import annotations
import email
import imaplib
import mimetypes
import os
import smtplib
import ssl
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

from email_sidecar import send_lock

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

# Default cap on a single inbound attachment (decoded bytes). Read at call
# time via _max_attachment_bytes() so tests can override EMAIL_MAX_ATTACHMENT_BYTES.
_DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _max_attachment_bytes() -> int:
    raw = os.environ.get("EMAIL_MAX_ATTACHMENT_BYTES", "").strip()
    if raw:
        return int(raw)
    return _DEFAULT_MAX_ATTACHMENT_BYTES


def _size_limit_error(limit: int) -> str:
    return f"attachment exceeds {limit} byte limit"

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


def extract_attachments(msg: email.message.Message) -> list[dict]:
    """Return non-body parts (explicit attachments + named inline parts) with raw bytes.

    A part counts as an attachment if it's flagged Content-Disposition:
    attachment, or it carries a filename at all (covers inline images/files
    some clients send without an explicit disposition). get_body() already
    excludes anything with "attachment" in its disposition from the body
    text, so the two don't double up on a plain attachment.

    Parts over EMAIL_MAX_ATTACHMENT_BYTES (default 10 MiB) are returned with
    payload=None and an `error` instead of decoded bytes.
    """
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = str(part.get("Content-Disposition", ""))
        filename = part.get_filename()
        if not filename and "attachment" not in disposition.lower():
            continue
        limit = _max_attachment_bytes()
        encoded = part.get_payload(decode=False)
        # Skip decode when the encoded form already cannot fit the cap
        # (base64 is ~4/3 of decoded size). Avoids a second huge allocation.
        if isinstance(encoded, (str, bytes)) and len(encoded) > (limit * 4 // 3) + 8:
            attachments.append({
                "filename":     filename or "attachment",
                "content_type": part.get_content_type(),
                "size":         len(encoded),
                "payload":      None,
                "error":        _size_limit_error(limit),
            })
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > limit:
            attachments.append({
                "filename":     filename or "attachment",
                "content_type": part.get_content_type(),
                "size":         len(payload),
                "payload":      None,
                "error":        _size_limit_error(limit),
            })
            continue
        attachments.append({
            "filename":     filename or "attachment",
            "content_type": part.get_content_type(),
            "size":         len(payload),
            "payload":      payload,
        })
    return attachments


def _attachments_base_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data")) / "attachments"


def save_attachments(uid: str, attachments: list[dict]) -> list[dict]:
    """Persist extracted attachment bytes to disk, keyed by message uid.

    Returns metadata dicts (filename, content_type, size, path) with the raw
    payload dropped — the path is what Hermes/Willow actually needs to pick
    the file back up and forward it on (e.g. via Telegram).

    Oversized or already-errored parts are returned with an `error` key and
    no `path` (nothing is written). A write failure on one file does not
    abort the rest.
    """
    if not attachments:
        return []
    out_dir = _attachments_base_dir() / uid
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [{
            "filename":     att.get("filename", ""),
            "content_type": att.get("content_type", ""),
            "size":         att.get("size", 0),
            "error":        str(exc),
        } for att in attachments]
    saved = []
    limit = _max_attachment_bytes()
    for i, att in enumerate(attachments):
        meta = {
            "filename":     att["filename"],
            "content_type": att["content_type"],
            "size":         att["size"],
        }
        if att.get("error"):
            meta["error"] = att["error"]
            saved.append(meta)
            continue
        payload = att.get("payload")
        if not payload:
            meta["error"] = "empty attachment"
            saved.append(meta)
            continue
        if len(payload) > limit:
            meta["error"] = _size_limit_error(limit)
            saved.append(meta)
            continue
        safe_name = os.path.basename(att["filename"]) or f"attachment-{i}"
        path = out_dir / safe_name
        if path.exists():
            stem, suffix = os.path.splitext(safe_name)
            path = out_dir / f"{stem}-{i}{suffix}"
        try:
            path.write_bytes(payload)
        except OSError as exc:
            meta["error"] = str(exc)
            saved.append(meta)
            continue
        meta["path"] = str(path)
        saved.append(meta)
    return saved


def message_to_dict(uid: bytes, msg: email.message.Message) -> dict:
    """Convert a parsed email.message.Message to a plain dict for Hermes."""
    return {
        "id":          uid.decode(),
        "from":        msg.get("From", ""),
        "to":          msg.get("To", ""),
        "cc":          msg.get("Cc", ""),
        "subject":     msg.get("Subject", ""),
        "date":        msg.get("Date", ""),
        "message_id":  msg.get("Message-ID", "").strip(),
        "references":  parse_references(msg),
        "body":        get_body(msg),
        "attachments": [],
    }


# ── IMAP helpers ──────────────────────────────────────────────────────────────

def imap_connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    return conn


def append_to_sent(msg_bytes: bytes) -> None:
    """Best-effort copy of a just-sent message into the Sent folder.

    SMTP has no concept of folders, so a raw smtplib send delivers the
    message but leaves no trace in the mailbox's Sent view — that's a
    client-side responsibility every normal MUA (webmail included) handles
    by IMAP-appending a copy after sending. Failure here must never surface
    as a send failure: the message has already gone out over SMTP by the
    time this runs, so we swallow errors rather than raise.
    """
    try:
        conn = imap_connect()
        try:
            for folder in ("Sent", "INBOX.Sent", "[Gmail]/Sent Mail", "Sent Items"):
                try:
                    result, _ = conn.append(
                        folder, "\\Seen", imaplib.Time2Internaldate(time.time()), msg_bytes
                    )
                except Exception:
                    continue
                if result == "OK":
                    break
        finally:
            conn.logout()
    except Exception:
        pass


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
            typ2, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
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
            typ2, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
            if typ2 != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if is_automated(msg):
                move_to_archive(conn, uid)
                continue
            record = message_to_dict(uid, msg)
            try:
                record["attachments"] = save_attachments(
                    record["id"], extract_attachments(msg)
                )
            except Exception as exc:
                # One bad attachment must not abort the rest of the inbox poll.
                record["attachments"] = [{
                    "filename": "", "content_type": "", "size": 0, "error": str(exc),
                }]
            record["thread"] = fetch_thread_context(conn, record["references"])
            results.append(record)
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass
    return results


def do_queue_draft(email_id: str) -> dict:
    """Register a posted draft as pending owner approval, keyed by the 'id' field from fetch_unread (the IMAP uid, i.e. the Telegram message's Email-ID)."""
    return send_lock.queue_draft(email_id)


def do_approve_send(email_id: str) -> dict:
    """Approve a queued draft for sending. Only the owner-reply session should call this."""
    return send_lock.approve_send(email_id)


def do_discard_draft(email_id: str) -> dict:
    """Drop a queued draft without sending it."""
    return send_lock.discard(email_id)


def _resolve_outbound_attachment(path: str) -> Path:
    """Resolve path and require it to stay under $HERMES_HOME/attachments/.

    Follows symlinks, so a link planted inside the attachments dir that
    points outside is rejected. Missing files under the dir raise
    FileNotFoundError; anything that resolves outside raises ValueError.
    """
    base = _attachments_base_dir().resolve()
    file_path = Path(path).expanduser().resolve()
    try:
        file_path.relative_to(base)
    except ValueError:
        raise ValueError(
            f"attachment path not allowed: {path} (must be under {base})"
        ) from None
    if not file_path.is_file():
        raise FileNotFoundError(f"attachment not found: {path}")
    return file_path


def _attach_file(msg: MIMEMultipart, path: str) -> None:
    """Read an allowed local file and attach it to msg.

    Raises FileNotFoundError if missing, ValueError if the path resolves
    outside $HERMES_HOME/attachments/.
    """
    file_path = _resolve_outbound_attachment(path)
    content_type, _ = mimetypes.guess_type(file_path.name)
    maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
    part = MIMEBase(maintype, subtype or "octet-stream")
    part.set_payload(file_path.read_bytes())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=file_path.name)
    msg.attach(part)


def do_send_reply(
    to: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: list[str] | None = None,
    email_id: str = "",
    cc: list[str] | None = None,
    attachments: list[str] | None = None,
) -> dict:
    """Send a reply via SMTP with an explicit Date header.

    Refuses to send unless email_id (the Email-ID/uid the draft was queued
    under) has a matching lock that was explicitly approved via
    approve_send — see send_lock.py. email_id is independent of in_reply_to,
    which remains the RFC Message-ID used for the outgoing In-Reply-To header.

    cc, if given, is added both as an envelope recipient (so the addresses
    actually receive the message — setting the Cc header alone does not)
    and as the visible Cc header.

    attachments, if given, is a list of local file paths that resolve under
    $HERMES_HOME/attachments/ (typically a path fetch_unread saved). A
    missing file, or a path that escapes that directory (including via
    symlink), fails the whole send rather than going out silently without it.
    """
    if not send_lock.is_approved(email_id):
        return {
            "ok": False,
            "error": "send_locked: no approved draft for this email_id. "
                     "Call queue_draft after posting to Telegram, then approve_send "
                     "once the owner actually replies 'send'.",
        }
    try:
        cc = cc or []
        attachments = attachments or []
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body, "plain", "utf-8"))
        msg = MIMEMultipart("mixed") if attachments else body_part
        if attachments:
            msg.attach(body_part)

        from_header = EMAIL_ADDRESS if not SENDER_NAME else f"{SENDER_NAME} <{EMAIL_ADDRESS}>"
        msg["From"]    = from_header
        msg["To"]      = to
        if cc:
            msg["Cc"] = ", ".join(cc)
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
        for path in attachments:
            _attach_file(msg, path)

        use_implicit = SMTP_USE_IMPLICIT_SSL == "1" or (
            SMTP_PORT == 465 and SMTP_USE_STARTTLS != "1"
        )
        use_starttls  = SMTP_USE_STARTTLS == "1" or (
            SMTP_PORT == 587 and SMTP_USE_IMPLICIT_SSL != "1"
        )
        context = ssl.create_default_context()
        envelope_recipients = [to] + cc

        if use_implicit:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
                server.ehlo()
                server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                server.sendmail(EMAIL_ADDRESS, envelope_recipients, msg.as_bytes())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                if use_starttls:
                    server.starttls(context=context)
                server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                server.sendmail(EMAIL_ADDRESS, envelope_recipients, msg.as_bytes())

        append_to_sent(msg.as_bytes())
        send_lock.discard(email_id)
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
