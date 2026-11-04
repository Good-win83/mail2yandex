#!/usr/bin/env python3
"""One-time *local* enrollment QR for Google Authenticator. Safe to delete afterward.

Requires an isolated host environment with: pip install 'qrcode[pil]'
The running Mail2Yandex service does not depend on this script or the QR PNG.
"""
import argparse
import base64
import os
import stat
from pathlib import Path
from urllib.parse import quote

def main():
    parser = argparse.ArgumentParser(description="Create local TOTP QR for Mail2Yandex")
    parser.add_argument("--file", default="data/admin-totp.secret")
    parser.add_argument("--qr", default="admin-totp-setup.png")
    parser.add_argument("--user", default="admin")
    args = parser.parse_args()
    try:
        import qrcode
    except ImportError:
        parser.error("Install QR dependency into a host venv: pip install 'qrcode[pil]'")
    target = Path(args.file)
    qr_path = Path(args.qr)
    if qr_path.exists():
        parser.error("QR file already exists; delete it after use or choose a new --qr path")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Re-generate QR for an existing secret, without resetting user's TOTP.
        from_secret = target.read_text(encoding="ascii").strip()
        raw = from_secret.replace(" ", "").upper()
        try:
            key = base64.b32decode(raw + "=" * (-len(raw) % 8), casefold=True)
        except (ValueError, base64.binascii.Error):
            parser.error("Invalid existing TOTP secret")
        if len(key) < 20:
            parser.error("Existing TOTP secret too short")
        secret = raw
        print("Existing TOTP secret reused; no MFA reset.")
    else:
        secret = base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(secret + "\n")
    uri = ("otpauth://totp/" + quote("Mail2Yandex:" + args.user, safe="") +
           "?secret=" + secret + "&issuer=Mail2Yandex&algorithm=SHA1&digits=6&period=30")
    image = qrcode.make(uri)
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    # QR contains the secret: create new file with private permissions.
    fd = os.open(str(qr_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        image.save(output, format="PNG")
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    os.chmod(qr_path, stat.S_IRUSR | stat.S_IWUSR)
    print("QR image (private):", qr_path)
    print("Google Authenticator -> Add a code -> Scan a QR code")
    print("After scanning and confirming successful login, DELETE the QR image.")
    print("The secret file must remain in place for future logins.")
    print("Keep the script only if you want to; it is not used by the service.")

if __name__ == "__main__":
    main()
