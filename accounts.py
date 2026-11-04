"""Persistent multi-mailbox configuration. Secrets never appear in read API."""
import imaplib
import json
import os
import re
import ssl
import tempfile
import threading
from pathlib import Path

LOCK = threading.RLock()
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def normalize(value, old=None):
    if not isinstance(value, dict):
        raise ValueError("Account must be an object")
    address = str(value.get("email", "")).strip().lower()
    if len(address) > 254 or not EMAIL.fullmatch(address):
        raise ValueError("Invalid mailbox address")
    password = value.get("password", "")
    if not isinstance(password, str):
        raise ValueError("Invalid password")
    if not password and old:
        password = old["password"]
    if not password:
        raise ValueError("Application password is required")
    host = str(value.get("host", "imap.yandex.ru")).strip()
    if host not in ("imap.yandex.ru", "imap.ya.ru"):
        raise ValueError("Only official Yandex IMAP hosts are permitted")
    port = int(value.get("port", 993))
    if port != 993:
        raise ValueError("IMAP TLS port must be 993")
    folder = str(value.get("folder", "INBOX")).strip()
    if folder != "INBOX":
        raise ValueError("Only INBOX is supported in account manager")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    return {"email": address, "password": password, "host": host,
            "port": port, "folder": folder, "enabled": enabled}


class Accounts:
    def __init__(self, filename):
        self.path = Path(filename)
        if not self.path.is_file():
            self.save([])

    def load(self):
        with LOCK:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError("Invalid accounts file")
            records = [normalize(x) for x in value]
            if len({a["email"] for a in records}) != len(records):
                raise ValueError("Duplicate mailbox")
            return records

    def public(self):
        return [{k: v for k, v in entry.items() if k != "password"}
                for entry in self.load()]

    def save(self, entries):
        with LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".accounts-", dir=str(self.path.parent))
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(entries, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def upsert(self, value):
        with LOCK:
            entries = self.load()
            address = str(value.get("email", "")).strip().lower()
            prior = next((a for a in entries if a["email"] == address), None)
            account = normalize(value, prior)
            entries = [a for a in entries if a["email"] != address] + [account]
            self.save(entries)
            return {k: v for k, v in account.items() if k != "password"}

    def delete(self, email):
        with LOCK:
            entries = self.load()
            remaining = [a for a in entries if a["email"] != str(email).lower()]
            if len(remaining) == len(entries):
                raise ValueError("Mailbox not found")
            self.save(remaining)


class ImapAccountCheckError(RuntimeError):
    """Safe stage-specific account test failure; never expose IMAP server text."""


def test_connection(account):
    """Test TLS login and readonly INBOX, reporting only safe stage and type."""
    stage = "connect"
    try:
        with imaplib.IMAP4_SSL(
            account["host"], account["port"], ssl_context=ssl.create_default_context(),
            timeout=15
        ) as server:
            stage = "login"
            server.login(account["email"], account["password"])
            stage = "select"
            status, _ = server.select(account["folder"], readonly=True)
            if status != "OK":
                raise RuntimeError("folder unavailable")
        return True
    except (imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError, RuntimeError) as exc:
        if stage == "connect":
            hint = "connection or TLS greeting failed; check network and IMAP service"
        elif stage == "login":
            hint = "login rejected; check mailbox address, application password and IMAP permissions"
        else:
            hint = "INBOX unavailable; check folder permissions"
        raise ImapAccountCheckError(
            "IMAP test failed: stage=" + stage + "; " + hint +
            " (" + type(exc).__name__ + ")"
        ) from None
