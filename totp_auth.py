"""Minimal RFC 6238 TOTP and in-memory authenticated sessions, no dependencies."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

LOCK = threading.RLock()


def read_secret(path):
    """Never create a default secret on first request: provisioning is explicit."""
    raw = Path(path).read_text(encoding="ascii").strip().replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z2-7]{32,}", raw):
        raise ValueError("TOTP secret must be base32 and at least 160 bits")
    secret = base64.b32decode(raw + "=" * (-len(raw) % 8), casefold=True)
    if len(secret) < 20:
        raise ValueError("TOTP secret too short")
    return secret


def totp(secret, step):
    digest = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset+4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


class LoginManager:
    def __init__(self, user, secret, ttl=3600):
        if not user or not isinstance(user, str):
            raise ValueError("ADMIN_USER is required")
        if not isinstance(secret, bytes) or len(secret) < 20:
            raise ValueError("Valid TOTP secret is required")
        self.user = user
        self.secret = secret
        self.ttl = max(300, min(int(ttl), 43200))
        self.sessions = {}
        self.attempts = {}
        self.last_otp_step = -1

    def authenticate(self, user, otp, client, now=None):
        now = time.time() if now is None else now
        with LOCK:
            recent = [t for t in self.attempts.get(client, []) if now - t < 300]
            self.attempts[client] = recent
            if len(recent) >= 5:
                return None
            step = int(now // 30)
            valid = (isinstance(otp, str) and re.fullmatch(r"\d{6}", otp)
                     and any(step + offset > self.last_otp_step
                             and hmac.compare_digest(totp(self.secret, step + offset), otp)
                             for offset in (-1, 0, 1) if step + offset >= 0))
            login_matches = (isinstance(user, str) and hmac.compare_digest(user, self.user))
            if not (login_matches and valid):
                recent.append(now)
                self.attempts[client] = recent
                return None
            matched = next(step+offset for offset in (-1,0,1)
                           if step+offset > self.last_otp_step
                           and hmac.compare_digest(totp(self.secret, step+offset), otp))
            self.last_otp_step = matched
            self.attempts.pop(client, None)
            sid = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            self.sessions[sid] = (now + self.ttl, csrf)
            # Limit unbounded memory growth and expire sessions.
            self.sessions = {k:v for k,v in self.sessions.items() if v[0] > now}
            return sid, csrf

    def session(self, cookie_header, now=None):
        now = time.time() if now is None else now
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header or "")
            sid = cookie["mail2yandex_session"].value
        except (KeyError, ValueError):
            return None
        with LOCK:
            session = self.sessions.get(sid)
            if not session or session[0] <= now:
                self.sessions.pop(sid, None)
                return None
            return sid, session[1]

    def logout(self, sid):
        with LOCK:
            self.sessions.pop(sid, None)
