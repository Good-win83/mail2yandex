"""SMTP-to-Yandex Messenger notification gateway."""
import asyncio
import html
import json
import logging
import os
import re
import signal
import sqlite3
import threading
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

import requests
from aiosmtpd.controller import Controller

# The Docker entrypoint executes this file as __main__, while the IMAP worker
# imports 'mail2yandex'. Alias both names to preserve exception/class identity
# and avoid loading two copies of MessengerDeliveryError.
if __name__ == "__main__":
    import sys
    sys.modules["mail2yandex"] = sys.modules["__main__"]

API_URL = "https://botapi.messenger.yandex.net/bot/v1/messages/sendText/"
LOG = logging.getLogger("mail2yandex")
DB_LOCK = threading.Lock()


def audit(status, channel, detail=""):
    """Log metadata only, no email bodies or tokens."""
    db_path = os.getenv("AUDIT_DB", "")
    if not db_path:
        return
    try:
        with DB_LOCK, sqlite3.connect(db_path, timeout=5) as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, status TEXT NOT NULL, channel TEXT NOT NULL, detail TEXT NOT NULL)")
            db.execute("INSERT INTO events(at,status,channel,detail) VALUES(?,?,?,?)", (datetime.now(timezone.utc).isoformat(), status, channel[:30], detail[:200]))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")
    except (OSError, sqlite3.Error):
        LOG.exception("Unable to write audit event")


class PlainHTML(HTMLParser):
    """Extract visible text, ignoring CSS, scripts and document metadata."""
    IGNORED = {"style", "script", "head", "template", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORED:
            self.ignored.append(tag)
            return
        if self.ignored:
            return
        if tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in self.IGNORED:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)



def html_to_text(source):
    parser = PlainHTML()
    parser.feed(source)
    return html.unescape("".join(parser.parts))


def extract_body(message):
    plain, html_part = [], []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_maintype() != "text":
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            continue
        if not isinstance(value, str):
            continue
        if part.get_content_type() == "text/plain":
            plain.append(value)
        elif part.get_content_type() == "text/html":
            html_part.append(value)
    result = "\n".join(plain) if plain else html_to_text("\n".join(html_part))
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def clean_notification_body(body):
    """Remove only known boilerplate phrases, even inside HTML-derived lines.

    Automated email templates may contain NBSP, zero-width characters and
    line wraps between words. Match the phrase rather than requiring a whole
    line to match, so text joined by HTML rendering is also cleaned.
    """
    import unicodedata

    body = unicodedata.normalize("NFKC", body)
    # Some mail generators put CSS inside text/plain or inline with visible text,
    # not inside HTML <style>. Remove complete selector/declaration blocks
    # without consuming adjacent words or the actual comment text.
    css_rule = re.compile(
        r"(?<![\w.-])(?:[.#]?[A-Za-z][\w-]*"
        r"(?:\s*[>+~]\s*[.#]?[A-Za-z][\w-]*)*"
        r"(?:\s*,\s*[.#]?[A-Za-z][\w-]*(?:\s*[>+~]\s*[.#]?[A-Za-z][\w-]*)*)*"
        r"|\*)\s*\{\s*([-A-Za-z][\w-]*)\s*:\s*[^{}]*\}",
        re.IGNORECASE,
    )
    css_properties = {
        "margin", "margin-top", "margin-bottom", "padding", "padding-top",
        "padding-bottom", "line-height", "white-space", "border", "display",
        "color", "background", "font", "font-size", "font-family", "width",
        "height", "text-align", "text-decoration", "list-style", "opacity",
    }
    def remove_css_rule(match):
        declaration = match.group(0)
        property_name = match.group(1).lower()
        return "" if property_name in css_properties or ";" in declaration else declaration

    body = css_rule.sub(remove_css_rule, body)
    # Strip zero-width formatting characters often inserted by HTML templates.
    body = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", body)
    gap = r"[\s\u00a0]*"
    phrases = (
        ("При", "ответе", "на", "данное", "письмо", "пожалуйста",
         "не", "изменяйте", "тему", "письма"),
        ("Письмо", "сгенерировано", "автоматически"),
    )
    for words in phrases:
        # Comma after 'письмо' and 'пожалуйста' may vary by mail template.
        parts = [re.escape(word) for word in words]
        if len(words) > 3:
            parts[4] += r"[,]?"
            parts[5] += r"[,]?"
        pattern = gap.join(parts) + r"[!.]?"
        body = re.sub(pattern, "", body, flags=re.IGNORECASE)
    # Remove whitespace-only spacer lines and collapse excess line breaks.
    body = re.sub(r"(?m)^[^\S\n\r]*$", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()



def format_message(raw, mail_from, recipients, max_chars):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(message.get("Subject", "(без темы)"))[:500]
    body = clean_notification_body(extract_body(message)) or "(текст отсутствует; возможно, письмо содержит только вложения)"
    result = f"📧 Новое письмо\nТема: {subject}\n\n{body}"
    if len(result) > max_chars:
        result = result[:max_chars - len("\n\n[Сообщение сокращено]")] + "\n\n[Сообщение сокращено]"
    return result


class MessengerDeliveryError(RuntimeError):
    """Safe Bot API diagnostics: no tokens, request bodies or response echo."""

    def __init__(self, status, reason):
        self.status = status
        self.reason = reason
        super().__init__("Bot API HTTP " + str(status) + "; " + reason)


def messenger_failure_reason(status, description=""):
    """Use a bounded allowlist of known errors; avoid leaking API response text."""
    message = str(description).lower()
    if status in (401, 403):
        return "authentication or access denied; check bot OAuth token and permissions"
    if status == 429:
        return "rate limit exceeded; retry later"
    if status >= 500:
        return "Messenger server error"
    if "not a member" in message:
        return "bot is not a member of target chat"
    if "privacy" in message or "forbid" in message:
        return "recipient privacy policy prevents direct messages"
    if "not found" in message or "unknown user" in message:
        return "recipient or chat not found"
    if status == 400:
        return "bad request; check recipient login, user_id or chat_id"
    return "request rejected; verify recipient and bot access"


class Messenger:
    def __init__(self, token, recipient, timeout):
        self.token = token
        self.recipient = recipient
        self.timeout = timeout

    def check_token(self):
        """Read-only Bot API self/get; validates OAuth without sending a message."""
        url = "https://botapi.messenger.yandex.net/bot/v1/self/get"
        try:
            response = requests.get(
                url, headers={"Authorization": "OAuth " + self.token},
                timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise MessengerDeliveryError("network", type(exc).__name__) from None
        try:
            data = response.json()
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not response.ok or data.get("ok") is not True:
            raise MessengerDeliveryError(
                response.status_code,
                messenger_failure_reason(response.status_code, data.get("description", "")))
        return {"login": str(data.get("login", ""))[:200],
                "organizations": data.get("organizations", [])}

    def send(self, text, recipient=None):
        destination = self.recipient if recipient is None else recipient
        data = dict(destination, text=text, disable_web_page_preview=True)
        try:
            response = requests.post(
                API_URL,
                headers={"Authorization": "OAuth " + self.token},
                json=data,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise MessengerDeliveryError("network", type(exc).__name__) from None

        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if not response.ok or payload.get("ok") is not True:
            raise MessengerDeliveryError(
                response.status_code,
                messenger_failure_reason(response.status_code, payload.get("description", "")),
            )
        return payload.get("message_id")


ALLOWED_KINDS = {"chat_id", "login", "user_id"}


def validate_destination(value):
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("A destination must have exactly one address field")
    kind, target = next(iter(value.items()))
    if kind not in ALLOWED_KINDS or not isinstance(target, str) or not target.strip():
        raise ValueError("Destination must contain chat_id, login or user_id")
    return {kind: target.strip()}


def validate_destinations(value):
    """Old single-recipient dictionaries and new arrays are supported."""
    values = value if isinstance(value, list) else [value]
    if not values or len(values) > 30:
        raise ValueError("Use 1 to 30 recipients per mailbox")
    result, seen = [], set()
    for item in values:
        dest = validate_destination(item)
        key = tuple(dest.items())
        if key not in seen:
            result.append(dest)
            seen.add(key)
    return result


class Router:
    def __init__(self, routes=None, default=None):
        self.routes = {}
        for address, destination in (routes or {}).items():
            if not isinstance(address, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", address):
                raise ValueError(f"Invalid email address in routes: {address!r}")
            key = address.casefold()
            if key in self.routes:
                raise ValueError("Duplicate SMTP recipient address")
            self.routes[key] = validate_destinations(destination)
        self.default = validate_destinations(default) if default is not None else None
        if not self.routes and self.default is None:
            raise ValueError("At least one route or default is required")

    @classmethod
    def from_file(cls, path, default=None):
        if not path:
            return cls(default=default)
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) - {"routes", "default"}:
            raise ValueError("Routes file must contain only routes and optional default")
        routes = document.get("routes", {})
        if not isinstance(routes, dict):
            raise ValueError("routes must be a JSON object")
        return cls(routes, document.get("default", default))

    def resolve(self, address):
        return self.routes.get(address.strip().casefold(), self.default)

    def destinations(self, addresses):
        result = []
        seen = set()
        for address in addresses:
            destinations = self.resolve(address)
            if destinations is None:
                raise ValueError("Unrouted SMTP recipient")
            for destination in destinations:
                key = tuple(destination.items())
                if key not in seen:
                    result.append(destination)
                    seen.add(key)
        return result


class Handler:
    def __init__(self, messenger, max_chars, router=None, router_loader=None):
        self.messenger = messenger
        self.max_chars = max_chars
        self.router = router or Router(default=messenger.recipient)
        self.router_loader = router_loader

    def current_router(self):
        return self.router_loader() if self.router_loader else self.router

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if self.current_router().resolve(address) is None:
            LOG.warning("Rejected unrouted SMTP recipient")
            return "550 5.1.1 Recipient not configured"
        envelope.rcpt_tos.append(address)
        return "250 2.1.5 Recipient OK"

    async def handle_DATA(self, server, session, envelope):
        try:
            destinations = self.current_router().destinations(envelope.rcpt_tos)
            text = format_message(
                envelope.content, envelope.mail_from, envelope.rcpt_tos,
                self.max_chars,
            )
            for destination in destinations:
                message_id = await asyncio.to_thread(self.messenger.send, text, destination)
                LOG.info("Delivered notification to %s; message_id=%s",
                         next(iter(destination)), message_id)
                audit("delivered", next(iter(destination)), str(message_id))
            return "250 2.0.0 Notification delivered"
        except ValueError:
            LOG.error("Notification rejected due to unrouted recipient")
            audit("rejected", "smtp", "Unrouted recipient")
            return "550 5.1.1 Recipient not configured"
        except Exception as exc:
            # Partial delivery may occur if a later destination fails.
            LOG.error("Notification delivery failed: %s", type(exc).__name__)
            audit("failed", "smtp", type(exc).__name__)
            return "451 4.3.0 Temporary notification delivery failure"


def positive_int(name, default):
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(name + " must be positive")
    return value


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.getenv("YANDEX_BOT_TOKEN", "").strip()
    recipients = {
        name: os.getenv(env, "").strip()
        for name, env in (
            ("chat_id", "YANDEX_CHAT_ID"),
            ("login", "YANDEX_LOGIN"),
            ("user_id", "YANDEX_USER_ID"),
        )
        if os.getenv(env, "").strip()
    }
    routes_file = os.getenv("ROUTES_FILE", "").strip()
    mode = os.getenv("INPUT_MODE", "imap").lower()
    if mode not in ("imap", "smtp", "both"):
        raise SystemExit("INPUT_MODE must be imap, smtp, or both")
    if not token or len(recipients) > 1 or (not routes_file and not recipients):
        raise SystemExit("Set YANDEX_BOT_TOKEN, plus ROUTES_FILE or exactly one "
                         "legacy recipient variable")
    router = Router.from_file(routes_file, default=recipients or None)

    host = os.getenv("SMTP_HOST", "127.0.0.1")
    port = positive_int("SMTP_PORT", 8025)
    max_bytes = positive_int("SMTP_MAX_BYTES", 1048576)
    max_chars = min(6000, positive_int("MESSAGE_MAX_CHARS", 5500))
    timeout = positive_int("HTTP_TIMEOUT", 15)
    messenger = Messenger(token, recipients, timeout)
    # Startup preflight is non-fatal: an outage must not prevent retries.
    try:
        bot_identity = messenger.check_token()
        LOG.info("Bot API token valid; bot_login=%s", bot_identity["login"])
        audit("bot_token_ok", "messenger", "startup verification")
    except MessengerDeliveryError as exc:
        LOG.warning("Bot API token verification failed at startup: %s", exc)
        audit("bot_token_failed", "messenger", str(exc))
    handler = Handler(messenger, max_chars, router,
                      router_loader=(lambda: Router.from_file(routes_file, default=recipients or None)) if routes_file else None)
    admin_server = None
    if os.getenv('ADMIN_ENABLED', 'false').lower() == 'true':
        from web_admin import start_admin
        admin_server = start_admin(messenger, routes_file)
    controller = None
    worker_stop = None
    worker = None
    try:
        if mode in ("smtp", "both"):
            controller = Controller(
                handler, hostname=host, port=port, data_size_limit=max_bytes
            )
            controller.start()
            LOG.info("SMTP listening on %s:%s", host, port)
        if mode in ("imap", "both"):
            from imap_collector import start_collector, start_multi_collector
            loader = (lambda: Router.from_file(routes_file, default=recipients or None)) if routes_file else (lambda: router)
            accounts_file = os.getenv("IMAP_ACCOUNTS_FILE", "").strip()
            if accounts_file:
                worker_stop, worker = start_multi_collector(messenger, loader, accounts_file)
                LOG.info("Multi-account IMAP collection started")
            else:
                worker_stop, worker = start_collector(messenger, loader)
                LOG.info("IMAP collection started; account=%s", os.getenv("IMAP_USER"))
        while True:
            import time
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if worker_stop:
            worker_stop.set()
            worker.join(timeout=10)
        if controller is not None:
            controller.stop()
        if admin_server is not None:
            admin_server.shutdown()


if __name__ == "__main__":
    main()
