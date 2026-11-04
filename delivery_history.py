"""Bounded, metadata-only delivery log for the administration panel."""
import sqlite3
import threading
from datetime import datetime, timezone

LOCK = threading.RLock()


class DeliveryHistory:
    def __init__(self, filename):
        self.filename = filename
        with LOCK, sqlite3.connect(filename, timeout=10) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS delivery_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                uid INTEGER NOT NULL,
                kind TEXT NOT NULL,
                target TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT NOT NULL
            )""")

    def add(self, mailbox, uid, destination, outcome, detail=""):
        kind, target = next(iter(destination.items()))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with LOCK, sqlite3.connect(self.filename, timeout=10) as db:
            db.execute(
                "INSERT INTO delivery_history(at,mailbox,uid,kind,target,outcome,detail) VALUES(?,?,?,?,?,?,?)",
                (now, mailbox[:254], int(uid), kind, target[:254], outcome, detail[:150])
            )
            db.execute("""DELETE FROM delivery_history WHERE id NOT IN
                          (SELECT id FROM delivery_history ORDER BY id DESC LIMIT 2000)""")

    def recent(self, limit=100):
        with LOCK, sqlite3.connect(self.filename, timeout=10) as db:
            return db.execute(
                """SELECT at,mailbox,uid,kind,target,outcome,detail
                   FROM delivery_history ORDER BY id DESC LIMIT ?""",
                (min(max(int(limit), 1), 200),)
            ).fetchall()
