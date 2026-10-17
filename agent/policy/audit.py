"""Append-only JSONL audit log with a hash chain.

Every line carries `seq`, `ts`, `request_id`, `event` and the SHA-256 of the previous
line, so editing or deleting a line in the middle is detectable by `verify_audit_log`.
Truncating the tail (dropping the newest lines) cannot be detected from the file alone;
ship the log to a write-once store if that matters. The log never contains tokens.
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _digest(prev_hash: str, body: dict) -> str:
    payload = prev_hash + json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _last(self):
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open() as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        record = json.loads(last)
        return record["seq"], record["hash"]

    def append(self, request_id: str, event: str, **data) -> dict:
        with self._lock:
            seq, prev_hash = self._last()
            body = {
                "seq": seq + 1,
                "ts": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
                "event": event,
                **data,
                "prev_hash": prev_hash,
            }
            record = {**body, "hash": _digest(prev_hash, body)}
            # O_APPEND writes are atomic per line; fsync so a crash cannot lose an acknowledged event.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(record, sort_keys=True, default=str) + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            return record

    def read(self, request_id: str | None = None) -> list:
        if not self.path.exists():
            return []
        with self.path.open() as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        return [r for r in records if request_id in (None, r["request_id"])]


def verify_audit_log(path) -> tuple:
    """Return (ok, message). Checks sequence numbers and the hash chain."""
    prev_hash, expected_seq = GENESIS, 1
    path = Path(path)
    if not path.exists():
        return False, "audit log not found"
    with path.open() as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                claimed = record.pop("hash")
            except (ValueError, KeyError):
                return False, f"line {lineno}: not a valid audit record"
            if record.get("seq") != expected_seq:
                return False, f"line {lineno}: expected seq {expected_seq}, found {record.get('seq')}"
            if record.get("prev_hash") != prev_hash or _digest(prev_hash, record) != claimed:
                return False, f"line {lineno}: hash chain broken (record was modified or reordered)"
            prev_hash, expected_seq = claimed, expected_seq + 1
    return True, f"{expected_seq - 1} records, chain intact"
