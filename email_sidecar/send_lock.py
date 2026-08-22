"""
Code-level send lock for the email sidecar.

Closes the gap noted in REVIEW.md and README roadmap v1.1: nothing stops the
cron drafting session (Step 1-4 in SOUL.md, which processes attacker-controlled
email content) from calling send_reply directly instead of stopping to wait
for owner approval — the "wait for approval" rule lived only in SOUL.md text.

Now send_reply refuses to send unless a matching lock has been explicitly
flipped to "approved". The drafting session can only queue a lock
(queue_draft); only the owner-approval session (Step 5, triggered by a real
inbound Telegram message from an allowed_user) is instructed to approve one
(approve_send). Persisted to disk under HERMES_HOME so the lock survives the
two sessions being separate process invocations.

queue_draft/approve_send/discard run as separate `python server.py`
processes (Step 4 drafting vs Step 5 owner-approval — see config.yaml's
mcp_servers entry), and nothing prevents those from overlapping in time. A
plain read-JSON/modify/write_text is not atomic across processes and can
silently lose an update if two sessions race on the same file. Every mutation
below runs inside _locked_store(), which holds an flock() across the full
read-modify-write, and writes go through a temp file + os.replace so a crash
mid-write can't leave send_locks.json truncated.
"""

from __future__ import annotations
import copy
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

# Guards against two threads in this process racing to open/flock the lock
# file at once; flock() itself is what serializes across separate processes.
_write_lock = Lock()


def _lock_path() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "/opt/data")
    return Path(hermes_home) / "send_locks.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(path: Path, data: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".send_locks.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise


@contextmanager
def _locked_store():
    """Yield the store dict with an exclusive cross-process lock held, saving on exit.

    Skips the write if the caller didn't actually change anything (e.g. an
    early return on an error path) — a deep copy of the loaded data is cheap
    at this store's size and avoids a pointless flock/tempfile/replace round
    trip on every failed queue_draft/approve_send/discard call.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _write_lock, open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            data = _load(path)
            before = copy.deepcopy(data)
            yield data
            if data != before:
                _save(path, data)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def queue_draft(key: str) -> dict:
    """Register a drafted reply as pending approval. Call right after posting the draft to Telegram."""
    if not key:
        return {"ok": False, "error": "missing key"}
    with _locked_store() as data:
        data[key] = {
            "state": "queued",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
    return {"ok": True}


def approve_send(key: str) -> dict:
    """Flip a queued draft to approved. Only call this from the owner-reply session (SOUL.md Step 5)."""
    if not key:
        return {"ok": False, "error": "missing key"}
    with _locked_store() as data:
        entry = data.get(key)
        if entry is None:
            return {"ok": False, "error": "no queued draft for this key"}
        entry["state"] = "approved"
        entry["approved_at"] = datetime.now(timezone.utc).isoformat()
        data[key] = entry
    return {"ok": True}


def is_approved(key: str) -> bool:
    """Read-only — os.replace() in _save() guarantees this never sees a torn write, so no lock needed."""
    if not key:
        return False
    entry = _load(_lock_path()).get(key)
    return bool(entry and entry.get("state") == "approved")


def discard(key: str) -> dict:
    """Drop a lock entry without sending — for the owner-reply 'discard' path, and to consume after a send."""
    if not key:
        return {"ok": False, "error": "missing key"}
    with _locked_store() as data:
        if key not in data:
            return {"ok": False, "error": "no queued draft for this key"}
        del data[key]
    return {"ok": True}
