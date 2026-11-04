"""Per-mailbox IMAP polling diagnostics kept separately from mail contents."""
import sqlite3
import threading
from datetime import datetime, timezone

LOCK = threading.RLock()


class StatusStore:
    def __init__(self, filename):
        self.filename = filename
        with LOCK, sqlite3.connect(self.filename) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS mailbox_status (
                email TEXT PRIMARY KEY,
                last_attempt TEXT,
                last_success TEXT,
                last_error TEXT,
                processed INTEGER NOT NULL DEFAULT 0,
                delivered INTEGER NOT NULL DEFAULT 0
            )""")

    def record(self, email, *, success, processed=0, delivered=0, error=None):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with LOCK, sqlite3.connect(self.filename, timeout=10) as conn:
            conn.execute("""INSERT INTO mailbox_status(email,last_attempt,last_success,last_error,processed,delivered)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(email) DO UPDATE SET
                    last_attempt=excluded.last_attempt,
                    last_success=CASE WHEN excluded.last_success IS NULL THEN mailbox_status.last_success ELSE excluded.last_success END,
                    last_error=excluded.last_error,
                    processed=mailbox_status.processed + excluded.processed,
                    delivered=mailbox_status.delivered + excluded.delivered""",
                (email.lower(), now, now if success else None, None if success else (error or "Unknown error")[:120],
                 processed, delivered))

    def list(self):
        with LOCK, sqlite3.connect(self.filename, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            return {row["email"]: dict(row) for row in conn.execute(
                "SELECT email,last_attempt,last_success,last_error,processed,delivered FROM mailbox_status")}
