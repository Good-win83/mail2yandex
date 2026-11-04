"""Yandex Mail IMAP collector: poll UNSEEN messages and forward to Messenger.

Delivery checkpoint is per UIDVALIDITY/UID/destination in SQLite, to avoid
resending to successful destinations after another destination fails.
"""
import imaplib
import json
import logging
import os
import re
import sqlite3
import ssl
import threading
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from mail2yandex import Router, audit, format_message, MessengerDeliveryError
from mailbox_status import StatusStore
from delivery_history import DeliveryHistory

LOG = logging.getLogger("mail2yandex.imap")


class ImapStageError(RuntimeError):
    """Diagnostic marker without untrusted server response or secrets."""


def describe_failure(exc):
    if isinstance(exc, ImapStageError):
        return str(exc)
    if isinstance(exc, imaplib.IMAP4.error):
        return "IMAP server rejected command (" + type(exc).__name__ + ")"
    if isinstance(exc, (TimeoutError, OSError, ssl.SSLError)):
        return "Network/TLS error (" + type(exc).__name__ + ")"
    return type(exc).__name__



def retry_delay(interval, failures):
    """Back off repeated connection failures, capped at five minutes."""
    return min(300, max(interval, 1) * (2 ** min(failures, 4)))


def is_connection_abort(exc):
    return isinstance(exc, ImapStageError) and "stage=connect;" in str(exc) and "(abort)" in str(exc)


def config_int(key, default):
    value = int(os.getenv(key, str(default)))
    if value < 1:
        raise ValueError(key + " must be positive")
    return value


def recipient_addresses(raw, mailbox, mode):
    """IMAP has no SMTP envelope; choose stable mailbox or header routing."""
    if mode == "mailbox":
        return [mailbox]
    if mode != "headers":
        raise ValueError("IMAP_ROUTE_MODE must be mailbox or headers")
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    addresses = [addr.casefold() for _, addr in getaddresses(
        [str(h) for header in ("To", "Cc", "Delivered-To", "X-Original-To")
         for h in message.get_all(header, [])]
    ) if addr]
    # Header routing is NOT an authenticated envelope! Only use in trusted inbox.
    if not addresses:
        raise ValueError("No recipients in message headers")
    return list(dict.fromkeys(addresses))


class DeliveryStore:
    def __init__(self, filename):
        if not filename:
            raise ValueError("IMAP_STATE_DB must be configured")
        self.filename = filename
        self.lock = threading.Lock()
        with self.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS imap_delivered(
                mailbox TEXT NOT NULL, folder TEXT NOT NULL,
                uidvalidity TEXT NOT NULL, uid INTEGER NOT NULL,
                destination TEXT NOT NULL,
                PRIMARY KEY(mailbox,folder,uidvalidity,uid,destination))""")

    def connection(self):
        return sqlite3.connect(self.filename, timeout=10)

    def exists(self, mailbox, folder, validity, uid, destination):
        with self.lock, self.connection() as conn:
            return conn.execute(
                "SELECT 1 FROM imap_delivered WHERE mailbox=? AND folder=? AND uidvalidity=? AND uid=? AND destination=?",
                (mailbox, folder, validity, uid, destination)
            ).fetchone() is not None

    def add(self, mailbox, folder, validity, uid, destination):
        with self.lock, self.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO imap_delivered VALUES(?,?,?,?,?)",
                (mailbox, folder, validity, uid, destination))


class Collector:
    def __init__(self, messenger, router_loader, mailbox, password, store, settings=None):
        self.messenger = messenger
        self.router_loader = router_loader
        self.mailbox = mailbox
        self.password = password
        self.store = store
        settings = settings or {}
        self.host = settings.get("host", os.getenv("IMAP_HOST", "imap.yandex.ru"))
        self.port = int(settings.get("port", config_int("IMAP_PORT", 993)))
        self.folder = settings.get("folder", os.getenv("IMAP_FOLDER", "INBOX"))
        self.route_mode = os.getenv("IMAP_ROUTE_MODE", "mailbox").lower()
        self.mark_seen = os.getenv("IMAP_MARK_SEEN", "true").lower() == "true"
        self.max_bytes = config_int("IMAP_MAX_BYTES", 1048576)
        self.max_batch = config_int("IMAP_BATCH_SIZE", 50)
        self.max_chars = min(6000, config_int("MESSAGE_MAX_CHARS", 5500))
        self.processed_count = 0
        self.delivery_count = 0
        self.skipped_count = 0
        self.history = DeliveryHistory(os.getenv("DELIVERY_HISTORY_DB",
            os.path.join(os.path.dirname(os.path.abspath(store.filename)), "delivery-history.sqlite")))

    def process(self, server, uid, validity):
        status, data = server.uid("FETCH", str(uid), "(BODY.PEEK[] RFC822.SIZE)")
        if status != "OK":
            raise RuntimeError("IMAP UID FETCH failed")
        raw = next((x[1] for x in data if isinstance(x, tuple) and isinstance(x[1], bytes)), None)
        if raw is None:
            raise RuntimeError("No RFC822 body returned")
        if len(raw) > self.max_bytes:
            audit("oversize", "imap", "Maximum message size exceeded")
            LOG.warning("IMAP message exceeds size limit; UID=%s", uid)
            return False

        addresses = recipient_addresses(raw, self.mailbox, self.route_mode)
        destinations = self.router_loader().destinations(addresses)
        text = format_message(raw, self.mailbox, addresses, self.max_chars)
        failures = []
        for destination in destinations:
            key = json.dumps(destination, ensure_ascii=False, sort_keys=True)
            if self.store.exists(self.mailbox, self.folder, validity, uid, key):
                self.skipped_count += 1
                LOG.info("IMAP UID=%s duplicate skipped for %s", uid, next(iter(destination)))
                self.history.add(self.mailbox, uid, destination, "skipped", "Already delivered")
                continue
            try:
                message_id = self.messenger.send(text, destination)
                self.store.add(self.mailbox, self.folder, validity, uid, key)
                self.history.add(self.mailbox, uid, destination, "delivered", str(message_id))
                self.delivery_count += 1
                audit("delivered", "imap", "uid=" + str(uid) + "; type=" + next(iter(destination)))
                LOG.info("IMAP UID=%s delivered to %s message_id=%s", uid, next(iter(destination)), message_id)
            except MessengerDeliveryError as exc:
                kind = next(iter(destination))
                LOG.error("IMAP UID=%s Messenger delivery to %s failed: %s", uid, kind, exc)
                self.history.add(self.mailbox, uid, destination, "failed", str(exc))
                failures.append(str(exc))
            except Exception as exc:
                LOG.error("IMAP UID=%s delivery to %s failed: %s",
                          uid, next(iter(destination)), type(exc).__name__)
                self.history.add(self.mailbox, uid, destination, "failed", type(exc).__name__)
                failures.append(type(exc).__name__)
        if failures:
            raise ImapStageError("delivery failed for " + str(len(failures)) +
                                 " destination(s); first: " + failures[0])
        if self.mark_seen:
            status, _ = server.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Seen)")
            if status != "OK":
                LOG.warning("Could not mark UID=%s seen; delivery checkpoints retained", uid)
        self.processed_count += 1
        return True

    def poll_once(self):
        self.processed_count = 0
        self.delivery_count = 0
        self.skipped_count = 0
        errors = []
        stage = "connect"
        try:
            with imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(), timeout=30) as server:
                stage = "login"
                server.login(self.mailbox, self.password)
                stage = "select"
                status, _ = server.select(self.folder, readonly=False)
                if status != "OK":
                    raise RuntimeError("Cannot select IMAP folder")
                stage = "uidvalidity"
                validity_response = server.response("UIDVALIDITY")[1]
                if not validity_response or not validity_response[0]:
                    raise RuntimeError("UIDVALIDITY missing; refusing to process")
                validity = validity_response[0].decode("ascii")
                stage = "search"
                status, results = server.uid("SEARCH", None, "UNSEEN")
                if status != "OK":
                    raise RuntimeError("IMAP UID SEARCH failed")
                uids = results[0].split()[:self.max_batch] if results and results[0] else []
                stage = "fetch_or_deliver"
                for raw_uid in uids:
                    uid = int(raw_uid)
                    try:
                        if not self.process(server, uid, validity):
                            continue
                    except Exception as exc:
                        audit("failed", "imap", "uid=" + str(uid) + "; " + type(exc).__name__)
                        LOG.error("IMAP delivery failed UID=%s: %s", uid, describe_failure(exc))
                        errors.append(describe_failure(exc))
                stage = "close"
                server.close()
        except (imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError, RuntimeError) as exc:
            hint = ""
            if stage == "login":
                hint = "; verify IMAP_USER, app password and IMAP access settings"
            elif stage == "select":
                hint = "; check IMAP_FOLDER and mailbox permissions"
            raise ImapStageError("stage=" + stage + "; " + describe_failure(exc) + hint) from None
        if errors:
            raise ImapStageError("stage=fetch_or_deliver; failed messages=" + str(len(errors)) +
                                 "; first error=" + errors[0][:160])
        return self.processed_count, self.delivery_count

    def run(self, stop):
        interval = config_int("IMAP_POLL_SECONDS", 60)
        status_store = StatusStore(os.getenv("IMAP_STATUS_DB", "/data/mailbox-status.sqlite"))
        failures = 0
        while not stop.is_set():
            delay = interval
            try:
                processed, delivered = self.poll_once()
                status_store.record(self.mailbox, success=True, processed=processed, delivered=delivered)
                failures = 0
            except Exception as exc:
                status_store.record(self.mailbox, success=False, processed=self.processed_count,
                                    delivered=self.delivery_count, error=describe_failure(exc))
                audit("imap_error", "imap", type(exc).__name__)
                LOG.error("IMAP polling failed: %s", describe_failure(exc))
                if is_connection_abort(exc):
                    failures += 1
                    delay = retry_delay(interval, failures)
                    LOG.warning("IMAP connection aborted before login; next poll in %s seconds", delay)
                else:
                    failures = 0
            stop.wait(delay)


def start_collector(messenger, router_loader):
    mailbox = os.getenv("IMAP_USER", "").strip()
    password = os.getenv("IMAP_APP_PASSWORD", "")
    if not mailbox or not password:
        raise ValueError("IMAP_USER and IMAP_APP_PASSWORD are required")
    store = DeliveryStore(os.getenv("IMAP_STATE_DB", "/data/imap-state.sqlite"))
    collector = Collector(messenger, router_loader, mailbox, password, store)
    stop = threading.Event()
    worker = threading.Thread(target=collector.run, args=(stop,), name="imap-collector", daemon=True)
    worker.start()
    return stop, worker


def start_multi_collector(messenger, router_loader, filename):
    """Single scheduler reloads account file each cycle without restarting service.

    Sequential polling prevents overlapping polling of the same mailbox.
    Each account has independent UID/UIDVALIDITY checkpoints.
    """
    from accounts import Accounts
    accounts = Accounts(filename)
    store = DeliveryStore(os.getenv("IMAP_STATE_DB", "/data/imap-state.sqlite"))
    status_store = StatusStore(os.getenv("IMAP_STATUS_DB", "/data/mailbox-status.sqlite"))
    stop = threading.Event()

    def run():
        interval = config_int("IMAP_POLL_SECONDS", 60)
        while not stop.is_set():
            try:
                active = [a for a in accounts.load() if a["enabled"]]
                for account in active:
                    if stop.is_set():
                        break
                    worker = None
                    try:
                        worker = Collector(messenger, router_loader, account["email"],
                                           account["password"], store, account)
                        processed, delivered = worker.poll_once()
                        status_store.record(account["email"], success=True,
                                            processed=processed, delivered=delivered)
                    except Exception as exc:
                        status_store.record(account["email"], success=False,
                                            processed=worker.processed_count if worker is not None else 0,
                                            delivered=worker.delivery_count if worker is not None else 0,
                                            error=describe_failure(exc))
                        audit("imap_error", "imap", account["email"] + ": " + type(exc).__name__)
                        LOG.error("IMAP account %s failed: %s", account["email"], describe_failure(exc))
            except Exception as exc:
                LOG.error("IMAP account configuration error: %s", type(exc).__name__)
            stop.wait(interval)

    worker = threading.Thread(target=run, name="imap-multi-collector", daemon=True)
    worker.start()
    return stop, worker
