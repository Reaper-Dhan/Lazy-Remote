import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from collections import deque
from dataclasses import dataclass
from typing import Optional

from platform_support import HOST_NAME


PAIR_LIFETIME = 10 * 60
APPROVAL_LIFETIME = 90
DEVICE_LIFETIME = 30 * 24 * 60 * 60


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class RememberedBrowser:
    def __init__(self, path):
        self.path = path
        self.record = None
        if path is not None:
            try:
                record = json.loads(path.read_text(encoding="ascii"))
            except (FileNotFoundError, ValueError, RecursionError):
                return
            if (isinstance(record, dict) and isinstance(record.get("digest"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", record["digest"])
                    and type(record.get("expires")) is int):
                self.record = record

    def active(self):
        return self.record is not None and self.record["expires"] > time.time()

    def matches(self, token):
        return bool(token and self.active() and hmac.compare_digest(
            hashlib.sha256(token.encode("ascii")).hexdigest(), self.record["digest"]
        ))

    def replace(self, remember):
        token = secrets.token_urlsafe(32) if remember else ""
        try:
            if remember:
                record = {
                    "digest": hashlib.sha256(token.encode("ascii")).hexdigest(),
                    "expires": int(time.time()) + DEVICE_LIFETIME,
                }
                if self.path is not None:
                    with tempfile.TemporaryDirectory(dir=self.path.parent) as directory:
                        pending = Path(directory) / self.path.name
                        with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
                            json.dump(record, file)
                        pending.replace(self.path)
                self.record = record
            else:
                if self.path is not None:
                    self.path.unlink(missing_ok=True)
                self.record = None
        except OSError:
            raise RequestError(503, f"Could not update remembered browser access on the {HOST_NAME}.") from None
        return token


@dataclass
class Approval:
    token: str
    match: str
    until: float
    remember: Optional[bool]  # None means a previously paired browser is requesting a session.
    request_key: Optional[str] = None
    approved: bool = False


class Pairing:
    """Pairing and approval state. The host serializes calls from HTTP and local controls."""

    def __init__(self, browser_path):
        self.browser = RememberedBrowser(browser_path)
        self.code = self.qr = None
        self.until = 0
        self.attempts = deque()
        self.pending = None

    def generate(self, now):
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        self.code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
        self.qr = secrets.token_urlsafe(32)
        self.until = now + PAIR_LIFETIME

    def clear_codes(self):
        self.code = self.qr = None

    def expire(self, now):
        changed = False
        if self.browser.record and not self.browser.active():
            self.browser.record = None
            changed = True
        if self.code and now >= self.until:
            self.clear_codes()
            changed = True
        if self.pending and now >= self.pending.until:
            if self.pending.remember is not None:
                self.generate(now)
            self.pending = None
            changed = True
        return changed

    def pair(self, body, now, request_key=None):
        if self.pending:
            raise RequestError(409, "A request is already waiting for approval on the computer.")
        if not self.code:
            raise RequestError(409, "Get a fresh QR code or pairing code from Lazy Remote on the computer.")

        remember = body.get("remember", False)
        methods = body.keys() - {"remember"}
        if methods == {"code"}:
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 8:
                raise RequestError(429, "Too many pairing attempts. Wait a minute or use the QR code.")
            self.attempts.append(now)
            value = body["code"]
            expected = self.code.replace("-", "")
            valid = isinstance(value, str) and value.isascii() and hmac.compare_digest(
                value.strip().replace("-", "").upper(), expected
            )
        elif methods == {"qr"}:
            value = body["qr"]
            valid = isinstance(value, str) and value.isascii() and hmac.compare_digest(value, self.qr)
        else:
            valid = False
        if not valid or type(remember) is not bool:
            raise RequestError(403, "That pairing code didn't match.")
        self.clear_codes()
        return self.request(remember, now, request_key)

    def resume(self, device, now, request_key=None):
        if not self.browser.matches(device):
            raise RequestError(401, "Pair this browser to control the computer.")
        if self.pending:
            raise RequestError(409, "A request is already waiting for approval on the computer.")
        return self.request(None, now, request_key)

    def request(self, remember, now, request_key):
        self.pending = Approval(
            secrets.token_urlsafe(32), f"{secrets.randbelow(1_000_000):06}", now + APPROVAL_LIFETIME,
            remember, request_key,
        )
        return self.pending

    def request_for(self, token, request_key):
        if not self.pending:
            return None
        if self.pending.request_key is not None:
            if request_key and hmac.compare_digest(request_key, self.pending.request_key):
                return self.pending
        elif token and hmac.compare_digest(token, self.pending.token):
            return self.pending
        return None

    def decide(self, match, approve, now):
        if not self.pending or self.pending.match != match or self.pending.approved:
            raise RequestError(409, "That approval request has expired.")
        if approve:
            self.pending.approved = True
        else:
            if self.pending.remember is not None:
                self.generate(now)
            self.pending = None

    def claim(self, token, request_key):
        pending = self.request_for(token, request_key)
        if not pending:
            raise RequestError(401, "Request access again from your phone.")
        if not pending.approved:
            raise RequestError(409, "Approve the matching request on the computer first.")
        device = self.browser.replace(pending.remember) if pending.remember is not None else None
        self.pending = None
        self.clear_codes()
        return device
