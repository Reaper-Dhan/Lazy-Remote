#!/usr/bin/env python3
import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
import os
from pathlib import Path
import plistlib
import re
import secrets
import select
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from auth import APPROVAL_LIFETIME, DEVICE_LIFETIME, Pairing, RequestError
from platform_support import HOST_NAME, IS_LINUX, IS_MAC, local_endpoints, lock_file
from tls_setup import STATE_DIR, private_state_dir, tls_identity


# A packaged app (PyInstaller) unpacks its files into sys._MEIPASS.
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if IS_MAC:
    PERMISSION_MESSAGE = "Allow Accessibility access on the Mac to use the controls."
elif IS_LINUX:
    PERMISSION_MESSAGE = ("Unlock the PC. If it is unlocked, allow access to /dev/uinput: "
                          "the Lazy Remote setup guide shows how.")
else:
    PERMISSION_MESSAGE = "The PC refused the input. Try again."
HELPER_BIN = BASE_DIR / "input_helper"
COOKIE = "__Host-couch_remote"
DEVICE_COOKIE = "__Host-couch_device"
APPROVAL_COOKIE = "__Host-couch_approval"
IDLE_LIFETIME = 5 * 60
SERVER_IDLE_TIMEOUT = 30 * 60
SERVER_IDLE_MESSAGE = "Server stopped after 30 minutes without phone activity."
SESSION_LIFETIME = 12 * 60 * 60
DRAG_LIFETIME = 15
MAX_BODY = 4096
CONNECTION_TIMEOUT = 5
MAX_DELTA = 16384
MOTION_POSITION_LIMIT = 2 ** 32
SHARE_LIFETIME = 120  # seconds the phone certificate is offered over plain HTTP
SHARE_DOWNLOADS = 2
DEFAULT_GESTURES = {
    "tap": True, "secondary_tap": True, "scroll": True, "horizontal_scroll": True,
    "natural_scroll": True, "pinch_zoom": False, "three_finger_drag": False, "swipes": {},
}
# A leftward swipe reveals the Space to the right, and vice versa.
SWIPE_SHORTCUTS = {
    "swipe_left": ("81", 124, "Horiz"),
    "swipe_right": ("79", 123, "Horiz"),
    "swipe_up": ("32", 126, "Vert"),
    "swipe_down": ("33", 125, "Vert"),
}
CONTROL_FLAG = 1 << 18
KEYBOARD_FLAGS = sum(1 << bit for bit in (16, 17, 18, 19, 20, 21, 23))
KEYS = set("""
    space enter backspace escape tab up down left right f m c k j l close_tab
    reopen_tab next_tab prev_tab new_tab refresh fullscreen_mac next_video
    prev_video skip_intro cmd_left cmd_right zoom_in zoom_out zoom_reset
    vol_up vol_down vol_mute media_play bright_up bright_down
""".split()) | SWIPE_SHORTCUTS.keys()
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/remote.js": ("remote.js", "text/javascript; charset=utf-8"),
    "/remote.css": ("remote.css", "text/css; charset=utf-8"),
    "/assets/logo.svg": ("assets/logo.svg", "image/svg+xml"),
    "/assets/touch-icon.png": ("assets/touch-icon.png", "image/png"),
}


def mac_preferences(domain):
    try:
        exported = plistlib.loads(subprocess.check_output(
            ["/usr/bin/defaults", "export", domain, "-"], stderr=subprocess.DEVNULL, timeout=1
        ))
        return exported if isinstance(exported, dict) else {}
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return {}


def three_finger_shortcuts(settings, hotkeys):
    if settings.get("TrackpadThreeFingerDrag") or not isinstance(hotkeys, dict):
        return {}
    shortcuts = {}
    for name, (identifier, default_key, axis) in SWIPE_SHORTCUTS.items():
        if settings.get(f"TrackpadThreeFinger{axis}SwipeGesture") != 2:
            continue
        entry = hotkeys.get(identifier, {})
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        value = entry.get("value", {})
        if not isinstance(value, dict) or value.get("type", "standard") != "standard":
            continue
        parameters = value.get("parameters", [65535, default_key, CONTROL_FLAG])
        if not isinstance(parameters, list) or len(parameters) != 3:
            continue
        _, key_code, flags = parameters
        if (type(key_code) is int and 0 <= key_code <= 127 and
                type(flags) is int and flags >= 0 and flags & ~KEYBOARD_FLAGS == 0):
            shortcuts[name] = {"key_code": key_code, "flags": flags}
    return shortcuts


def mac_gesture_settings():
    settings = mac_preferences("com.apple.AppleMultitouchTrackpad") or mac_preferences(
        "com.apple.driver.AppleBluetoothMultitouch.trackpad"
    )
    hotkeys = mac_preferences("com.apple.symbolichotkeys").get("AppleSymbolicHotKeys", {})
    try:
        direction = subprocess.check_output(
            ["/usr/bin/defaults", "read", "-g", "com.apple.swipescrolldirection"],
            stderr=subprocess.DEVNULL, text=True, timeout=1,
        ).strip().lower()
    except (OSError, subprocess.SubprocessError):
        direction = "1"
    return {
        "tap": bool(settings.get("Clicking", DEFAULT_GESTURES["tap"])),
        "secondary_tap": bool(settings.get("TrackpadRightClick", DEFAULT_GESTURES["secondary_tap"]))
                         and settings.get("TrackpadCornerSecondaryClick", 0) == 0,
        "scroll": bool(settings.get("TrackpadScroll", DEFAULT_GESTURES["scroll"])),
        "horizontal_scroll": bool(settings.get("TrackpadHorizScroll", DEFAULT_GESTURES["horizontal_scroll"])),
        "natural_scroll": direction not in ("0", "false", "no"),
        "pinch_zoom": bool(settings.get("TrackpadPinch", DEFAULT_GESTURES["pinch_zoom"])),
        "three_finger_drag": bool(settings.get("TrackpadThreeFingerDrag", DEFAULT_GESTURES["three_finger_drag"])),
        "swipes": three_finger_shortcuts(settings, hotkeys),
    }


def valid_command(command):
    action = command.get("action")
    if action in ("move", "scroll", "motion"):
        limit = MOTION_POSITION_LIMIT if action == "motion" else MAX_DELTA
        for axis in ("dx", "dy"):
            value = command.get(axis)
            if action == "scroll" and type(value) is not int:
                return False
            if type(value) not in (int, float) or not -limit <= value <= limit:
                return False
        if action == "motion":
            stream, seq = command.get("stream"), command.get("seq")
            return (command.keys() == {"action", "dx", "dy", "stream", "seq"} and
                    isinstance(stream, str) and re.fullmatch(r"[0-9a-f]{32}", stream) is not None and
                    type(seq) is int and 1 <= seq <= 2 ** 32)
        return command.keys() == {"action", "dx", "dy"}
    if action == "click":
        count = command.get("count")
        button = command.get("button")
        if type(count) is not int or (button, count) not in (("left", 1), ("left", 2), ("right", 1)):
            return False
        if "continuation" in command:
            return (command.keys() == {"action", "button", "count", "continuation"} and
                    (button, count) == ("left", 1) and command["continuation"] is True)
        return command.keys() == {"action", "button", "count"}
    if action == "drag":
        return command.keys() == {"action", "active"} and type(command.get("active")) is bool
    if action == "key":
        name = command.get("name")
        return command.keys() == {"action", "name"} and isinstance(name, str) and name in KEYS
    if action == "type":
        value = command.get("text")
        return (command.keys() == {"action", "text"} and isinstance(value, str) and
                1 <= len(value) <= 128 and
                all(not 0xD800 <= ord(char) <= 0xDFFF and (ord(char) >= 32 or char in "\n\r\t")
                    and ord(char) != 127 for char in value))
    return False


class InputBridge:
    def __init__(self, path=HELPER_BIN, request_access=True):
        self.path = path
        self.proc = None
        source = path.with_suffix(".swift")
        # The app bundle ships a prebuilt, signed helper and no source; only a
        # source checkout rebuilds it.
        if source.exists() and (not path.exists() or source.stat().st_mtime_ns > path.stat().st_mtime_ns):
            with tempfile.TemporaryDirectory(prefix=".build-", dir=path.parent) as directory:
                output = Path(directory) / path.name
                subprocess.run(["/usr/bin/swiftc", "-O", str(source), "-o", str(output)], check=True)
                output.replace(path)
        self.start(request_access)

    def start(self, request_access=False):
        args = [str(self.path)]
        if request_access:
            args.append("--request-access")
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True
        )

    def send(self, command):
        try:
            if self.proc is None or self.proc.poll() is not None:
                self.close()
                self.start()
            self.proc.stdin.write(json.dumps(command, ensure_ascii=False).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
            ready, _, _ = select.select([self.proc.stdout], [], [], 4)
            reply = self.proc.stdout.readline(32).strip() if ready else b""
            if reply in (b"ok", b"permission"):
                return reply == b"ok"
        except OSError:
            pass
        self.close()
        raise RequestError(503, "The Mac input helper is unavailable. Try again.")

    def close(self):
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()


class Remote:
    def __init__(self, bridge, browser_path=None, on_change=None, gesture_reader=None, stop_when_idle=True):
        self.bridge = bridge
        self.pairing = Pairing(browser_path)
        self.on_change = on_change
        self.gesture_reader = gesture_reader
        self.stop_when_idle = stop_when_idle
        self.gestures = gesture_reader() if gesture_reader else DEFAULT_GESTURES
        self.motion_streams = {}
        self.lock = threading.Lock()
        self.token = None
        self.session_until = self.last_command = 0
        self.last_phone_activity = time.monotonic()
        self.drag_until = 0
        self.command_budget = 120
        self.budget_updated = 0

    def release_drag(self):
        if self.drag_until:
            self.drag_until = 0
            try:
                self.bridge.send({"action": "drag", "active": False})
            except RequestError:
                pass

    def revoke(self):
        self.token = None
        self.motion_streams.clear()
        self.release_drag()

    def snapshot(self):
        pending = self.pairing.pending
        return {
            "code": self.pairing.code,
            "qr": self.pairing.qr,
            "paired": bool(self.token),
            "remembered": self.pairing.browser.active(),
            "match": pending.match if pending else None,
            "approved": bool(pending and pending.approved),
        }

    def publish(self):
        if self.on_change:
            self.on_change("pairing", **self.snapshot())

    def new_code(self, forget=False, publish=True):
        with self.lock:
            now = time.monotonic()
            self.expire(now)
            if forget:
                self.pairing.browser.replace(False)
            self.pairing.pending = None
            self.revoke()
            self.pairing.generate(now)
            if publish:
                self.publish()
            return self.pairing.code

    def start_session(self, now):
        self.revoke()
        self.token = secrets.token_urlsafe(32)
        self.session_until = now + SESSION_LIFETIME
        self.last_command = self.last_phone_activity = now
        self.command_budget = 120
        self.budget_updated = now
        self.publish()
        return self.token

    def request_access(self, body=None, device=None):
        with self.lock:
            now = time.monotonic()
            self.expire(now)
            if self.token:
                raise RequestError(409, "A browser is already connected. Lock it before requesting access.")
            pending = self.pairing.pair(body, now) if body is not None else self.pairing.resume(device, now)
            self.publish()
            return pending.token

    def decide(self, match, approve):
        with self.lock:
            now = time.monotonic()
            self.expire(now)
            self.pairing.decide(match, approve, now)
            self.publish()

    def claim(self, pending):
        with self.lock:
            now = time.monotonic()
            self.expire(now)
            device = self.pairing.claim(pending)
            return self.start_session(now), device

    def expire(self, now):
        if self.stop_when_idle and now - self.last_phone_activity >= SERVER_IDLE_TIMEOUT:
            self.pairing.pending = None
            self.pairing.clear_codes()
            self.revoke()
            raise RequestError(503, SERVER_IDLE_MESSAGE)
        changed = self.pairing.expire(now)
        if self.token and (now >= self.session_until or now - self.last_command >= IDLE_LIFETIME):
            self.revoke()
            if not self.pairing.browser.active():
                self.pairing.generate(now)
            changed = True
        if changed:
            self.publish()
        if self.drag_until and now >= self.drag_until:
            self.release_drag()

    def require_session(self, token, now):
        self.expire(now)
        if not token or self.token is None or not hmac.compare_digest(token, self.token):
            raise RequestError(401, "Request access to control the computer.")

    def status(self, token, device, pending_token):
        with self.lock:
            try:
                self.require_session(token, time.monotonic())
            except RequestError as error:
                if error.status != 401:
                    raise
                pending = self.pairing.request_for(pending_token)
                return 401, {
                    "remembered": self.pairing.browser.matches(device),
                    "match": pending.match if pending else None,
                    "approved": bool(pending and pending.approved),
                }
            ready = self.bridge.send({"action": "check_perm"})
            gestures = {**self.gestures, "swipes": list(self.gestures["swipes"])}
            return 200, {
                "ready": ready, "dragging": bool(self.drag_until), "gestures": gestures,
                "unavailable": list(getattr(self.bridge, "unavailable_keys", ())),
            }

    def input_permission(self, request=False):
        with self.lock:
            try:
                return self.bridge.send({"action": "request_perm" if request else "check_perm"})
            except RequestError:
                return None

    def disconnect(self, token):
        with self.lock:
            now = time.monotonic()
            self.require_session(token, now)
            self.revoke()
            if not self.pairing.browser.active():
                self.pairing.generate(now)
            self.publish()

    def command(self, token, command):
        with self.lock:
            now = time.monotonic()
            self.require_session(token, now)
            if not valid_command(command):
                raise RequestError(400, "Invalid remote command.")
            self.command_budget = min(120, self.command_budget + 90 * (now - self.budget_updated))
            self.budget_updated = now
            cost = math.ceil(len(command["text"]) / 8) if command["action"] == "type" else 1
            if self.command_budget < cost:
                raise RequestError(429, "The remote is busy. Try again.")
            self.command_budget -= cost
            stream = None
            if command["action"] == "motion":
                stream = command["stream"]
                if stream not in self.motion_streams and len(self.motion_streams) >= 32:
                    raise RequestError(409, "Lock the remote and reconnect to start a new trackpad stream.")
                seq, x, y = command["seq"], command["dx"], command["dy"]
                previous_seq, previous_x, previous_y = self.motion_streams.get(stream, (0, 0, 0))
                if seq <= previous_seq:
                    return {"ok": True, "dragging": bool(self.drag_until)}
                command = {
                    "action": "move",
                    "dx": max(-MAX_DELTA, min(MAX_DELTA, x - previous_x)),
                    "dy": max(-MAX_DELTA, min(MAX_DELTA, y - previous_y)),
                }
            elif command["action"] == "key" and command["name"] in SWIPE_SHORTCUTS:
                shortcut = self.gestures["swipes"].get(command["name"])
                if shortcut is None:
                    raise RequestError(400, "That three-finger swipe is disabled on the Mac.")
                command = {"action": "shortcut", **shortcut}
            if command["action"] == "click":
                self.release_drag()
            if not self.bridge.send(command):
                self.release_drag()
                raise RequestError(503, PERMISSION_MESSAGE)
            if stream is not None:
                self.motion_streams[stream] = (seq, x, y)
            self.last_command = self.last_phone_activity = time.monotonic()
            if command["action"] == "drag":
                self.drag_until = self.last_command + DRAG_LIFETIME if command["active"] else 0
            elif command["action"] == "move" and self.drag_until:
                self.drag_until = self.last_command + DRAG_LIFETIME
            return {"ok": True, "dragging": bool(self.drag_until)}

    def maintain(self, stop):
        next_gesture_check = time.monotonic() + 10
        while not stop.wait(0.5):
            now = time.monotonic()
            with self.lock:
                try:
                    self.expire(now)
                except RequestError as error:
                    stop.set()
                    if self.on_change:
                        self.on_change("notice", message=str(error))
                    return
            if self.gesture_reader and now >= next_gesture_check:
                gestures = self.gesture_reader()
                with self.lock:
                    self.gestures = gestures
                next_gesture_check = time.monotonic() + 10


class CertificateShare:
    """Serves the public phone certificate over plain HTTP for a short time.

    The phone cannot use HTTPS before it trusts this computer, so the files go
    out unencrypted: someone else on the network could swap them. The page and
    the launcher both show the root fingerprint so the user can compare them,
    and the share ends after SHARE_LIFETIME seconds or SHARE_DOWNLOADS files.
    """

    FILES = {
        "Lazy-Remote.mobileconfig": ("mobileconfig", "application/x-apple-aspen-config"),
        "Lazy-Remote-Root.cer": ("cer", "application/x-x509-ca-cert"),
    }

    def __init__(self, address, directory, fingerprint, on_change=None):
        self.address, self.directory, self.fingerprint = address, Path(directory), fingerprint
        self.on_change = on_change
        self.server = None
        self.lock = threading.Lock()

    def file(self, extension):
        return next((path for path in self.directory.iterdir() if path.suffix == "." + extension), None)

    def start(self):
        with self.lock:
            self._stop_locked(publish=False)
            token = secrets.token_urlsafe(16)
            share = self

            class Handler(BaseHTTPRequestHandler):
                server_version = "LazyRemote"
                sys_version = ""

                def log_message(self, *_):
                    pass

                def do_GET(self):
                    prefix = f"/{token}/"
                    if not self.path.startswith(prefix):
                        self.send_error(404)
                        return
                    name = self.path[len(prefix):]
                    if name == "":
                        body, kind = share.page(), "text/html; charset=utf-8"
                    elif name in share.FILES:
                        extension, kind = share.FILES[name]
                        path = share.file(extension)
                        if path is None:
                            self.send_error(404)
                            return
                        body = path.read_bytes()
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
                    if name:
                        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.end_headers()
                    self.wfile.write(body)
                    if name:
                        share.downloaded()

            server = ThreadingHTTPServer((self.address, 0), Handler)
            server.timeout = 0.5
            server.until = time.monotonic() + SHARE_LIFETIME
            server.downloads = 0
            self.server = server
            threading.Thread(target=self._serve, args=(server,), daemon=True).start()
            url = f"http://{self.address}:{server.server_address[1]}/{token}/"
        self._publish("certificate_share", url=url, seconds=SHARE_LIFETIME, fingerprint=self.fingerprint)
        return url

    def page(self):
        return (
            "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Lazy Remote certificate</title>"
            "<body style='font:17px -apple-system,system-ui,sans-serif;margin:24px;line-height:1.45'>"
            "<h1 style='font-size:22px'>Trust this computer</h1>"
            "<p><a href='Lazy-Remote.mobileconfig'>iPhone or iPad profile</a><br>"
            "<a href='Lazy-Remote-Root.cer'>Android certificate</a></p>"
            "<p>Check that this fingerprint matches the one on the computer before you install:</p>"
            f"<p style='font:13px ui-monospace,monospace;word-break:break-all'>{self.fingerprint}</p>"
            "<p>iPhone: then open Settings → Profile Downloaded → Install, and turn on "
            "Lazy Remote Local Root in Settings → General → About → Certificate Trust Settings.</p>"
            "<p>Android: open Settings, search for “CA certificate”, and install the downloaded file.</p>"
        ).encode("utf-8")

    def downloaded(self):
        with self.lock:
            if self.server is None:
                return
            self.server.downloads += 1
            if self.server.downloads >= SHARE_DOWNLOADS:
                self.server.until = 0

    def _serve(self, server):
        while time.monotonic() < server.until and self.server is server:
            server.handle_request()
        with self.lock:
            if self.server is server:
                self.server = None
                ended = True
            else:
                ended = False
        server.server_close()
        if ended:
            self._publish("certificate_share_ended")

    def stop(self):
        with self.lock:
            self._stop_locked(publish=True)

    def _stop_locked(self, publish):
        if self.server is not None:
            self.server.until = 0
            self.server = None
            if publish:
                threading.Thread(target=self._publish, args=("certificate_share_ended",), daemon=True).start()

    def _publish(self, event, **data):
        if self.on_change:
            self.on_change(event, **data)


class RemoteServer(ThreadingHTTPServer):
    def __init__(self, address):
        self.slots = threading.BoundedSemaphore(32)
        self.remote = None
        self.assets = {
            route: ((BASE_DIR / filename).read_bytes(), content_type)
            for route, (filename, content_type) in ASSETS.items()
        }
        super().__init__(address, Handler)

    def configure_tls(self, context, names):
        self.context = context
        port = self.server_address[1]
        self.hosts = {name if port == 443 else f"{name}:{port}" for name in names}

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(CONNECTION_TIMEOUT)
            with self.context.wrap_socket(request, server_side=True) as secure:
                try:
                    self.finish_request(secure, client_address)
                finally:
                    self.shutdown_request(secure)
        except (OSError, ssl.SSLError):
            request.close()
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LazyRemote"
    sys_version = ""
    disable_nagle_algorithm = True

    def log_message(self, *_):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def reply(self, status, body, content_type="application/json", cookies=()):
        self.send_response(status)
        if status >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        for name, value, lifetime in cookies:
            max_age = "; Max-Age=0" if not value else f"; Max-Age={lifetime}" if lifetime else ""
            self.send_header(
                "Set-Cookie", f"{name}={value}; Path=/{max_age}; "
                "Secure; HttpOnly; SameSite=Strict"
            )
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json_reply(self, status, body, cookies=()):
        self.reply(status, json.dumps(body).encode("utf-8"), cookies=cookies)

    def host(self):
        values = self.headers.get_all("Host", [])
        if len(values) != 1 or values[0].lower() not in self.server.hosts:
            raise RequestError(421, f"Open the HTTPS address shown on the {HOST_NAME}.")
        return values[0].lower()

    def token(self, name=COOKIE):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except (ValueError, TypeError):
            return None
        value = cookie.get(name)
        if value is not None and re.fullmatch(r"[A-Za-z0-9_-]{43}", value.value):
            return value.value
        return None

    def json_body(self):
        if self.headers.get_all("Transfer-Encoding"):
            raise RequestError(400, "Unsupported request framing.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise RequestError(411, "A valid Content-Length is required.")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise RequestError(413, "Request is too large.")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].lower() != "application/json":
            raise RequestError(415, "Expected application/json.")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            raise RequestError(400, "Invalid JSON.") from None
        if not isinstance(body, dict):
            raise RequestError(400, "Expected a JSON object.")
        return body

    def do_GET(self):
        try:
            self.host()
            path = "/" if self.path == "/index.html" else self.path
            if path in self.server.assets:
                content, content_type = self.server.assets[path]
                self.reply(200, content, content_type)
            elif path == "/api/status":
                self.json_reply(*self.server.remote.status(
                    self.token(), self.token(DEVICE_COOKIE), self.token(APPROVAL_COOKIE)
                ))
            else:
                raise RequestError(404, "Page not found.")
        except RequestError as error:
            self.json_reply(error.status, {"error": str(error)})

    def do_POST(self):
        try:
            origin = self.headers.get_all("Origin", [])
            if origin != [f"https://{self.host()}"]:
                raise RequestError(403, "Open the remote page to send commands.")
            if self.path not in ("/api/pair", "/api/resume", "/api/claim", "/api/disconnect", "/api/cmd"):
                raise RequestError(404, "Page not found.")
            remote = self.server.remote
            body = self.json_body()
            if self.path == "/api/cmd":
                self.json_reply(200, remote.command(self.token(), body))
                return
            if self.path != "/api/pair" and body:
                raise RequestError(400, "Expected an empty JSON object.")
            if self.path in ("/api/pair", "/api/resume"):
                pending = (remote.request_access(body) if self.path == "/api/pair"
                           else remote.request_access(device=self.token(DEVICE_COOKIE)))
                self.json_reply(202, {"ok": True}, cookies=((APPROVAL_COOKIE, pending, APPROVAL_LIFETIME),))
            elif self.path == "/api/claim":
                token, device = remote.claim(self.token(APPROVAL_COOKIE))
                cookies = [(COOKIE, token, None), (APPROVAL_COOKIE, "", APPROVAL_LIFETIME)]
                if device is not None:
                    cookies.append((DEVICE_COOKIE, device, DEVICE_LIFETIME))
                self.json_reply(200, {"ok": True}, cookies=cookies)
            else:
                remote.disconnect(self.token())
                self.json_reply(200, {"ok": True})
        except RequestError as error:
            self.json_reply(error.status, {"error": str(error)})


def launcher_event(event, **data):
    try:
        print(json.dumps({"event": event, **data}), flush=True)
    except BrokenPipeError:
        pass


def terminal_event(event, **data):
    if event == "notice":
        print(data["message"], flush=True)
    elif event == "certificate_share":
        print(f"Open {data['url']} on your phone within {data['seconds']} seconds to get the certificate.\n"
              f"Check that it shows this fingerprint: {data['fingerprint']}", flush=True)
    elif event == "certificate_share_ended":
        print("Stopped offering the certificate.", flush=True)
    elif data["match"] and not data["approved"]:
        print(f"Phone requests access. Compare {data['match']} on both screens, "
              f"then type approve {data['match']} or deny {data['match']}.", flush=True)


def local_controls(remote, stop, launcher, share=None):
    for line in sys.stdin:
        if stop.is_set():
            return
        command = line.strip()
        try:
            decision = re.fullmatch(r"(approve|deny) ([0-9]{6})", command)
            if decision:
                remote.decide(decision[2], decision[1] == "approve")
            elif launcher and command in ("check_access", "request_input_access"):
                launcher_event("permission", input_ready=remote.input_permission(request=command == "request_input_access"))
            elif share is not None and command == "share_certificate":
                share.start()
            elif share is not None and command == "stop_certificate_share":
                share.stop()
            elif command in ("new_code", "forget") or (not launcher and not command):
                code = remote.new_code(forget=command != "new_code")
                if not launcher:
                    print(f"Pairing code (one use, 10 minutes)  {code}", flush=True)
        except RequestError as error:
            if launcher:
                launcher_event("notice", message=str(error))
            else:
                print(error, file=sys.stderr, flush=True)
    if launcher:
        stop.set()


def open_input(launcher):
    if IS_MAC:
        return InputBridge(request_access=not launcher)
    import input_backends
    return LocalInput(input_backends.create())


class LocalInput:
    """In-process mouse and keyboard output for Windows and Linux."""

    def __init__(self, backend):
        self.backend = backend
        # Remote keys this computer cannot send, so the phone can hide their buttons.
        self.unavailable_keys = sorted(backend.unavailable_keys())

    def send(self, command):
        try:
            return self.backend.send(command)
        except (OSError, ValueError):
            raise RequestError(503, f"The {HOST_NAME} input helper is unavailable. Try again.") from None

    def close(self):
        self.backend.close()


def main():
    parser = argparse.ArgumentParser(description="Control this computer from one paired browser over HTTPS.")
    parser.add_argument("--renew-certificate", action="store_true", help="Replace this computer's HTTPS identity.")
    parser.add_argument("--keep-running", action="store_true",
                        help="Do not stop after 30 minutes without phone activity. The phone still locks after 5.")
    parser.add_argument("--launcher", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.exit("Start Lazy Remote from your user account without sudo.")
    server = bridge = worker = instance = share = None
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        port = int(os.environ.get("REMOTE_PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError("REMOTE_PORT must be between 1 and 65535.")
        address, hostname = local_endpoints()
        server = RemoteServer((address, port))
        state = private_state_dir(STATE_DIR)
        instance = (state / "instance.lock").open("a")
        try:
            lock_file(instance, blocking=False)
        except BlockingIOError:
            raise RuntimeError("Lazy Remote is already running. Stop the other instance first.") from None
        context, directory, names, fingerprint = tls_identity(address, hostname, args.renew_certificate, state)
        server.configure_tls(context, names)
        share = CertificateShare(address, directory, fingerprint,
                                 on_change=launcher_event if args.launcher else terminal_event)
        bridge = open_input(args.launcher)
        remote = server.remote = Remote(
            bridge, state / "browser.json", on_change=launcher_event if args.launcher else terminal_event,
            gesture_reader=mac_gesture_settings if IS_MAC else None, stop_when_idle=not args.keep_running,
        )
        code = remote.new_code(publish=False)
        urls = [f"https://{host}/" for host in sorted(server.hosts, key=lambda host: ".local" not in host)]
        loopback = address == "127.0.0.1"
        if args.launcher:
            launcher_event(
                "ready", urls=urls, certificates=str(directory), fingerprint=fingerprint, loopback=loopback,
                input_ready=remote.input_permission(), **remote.snapshot()
            )
        else:
            print("Lazy Remote is running. Open one of these HTTPS addresses on your phone:")
            for url in urls:
                print(f"  {url}")
            if loopback:
                print(f"No private network address found. This launch is available on the {HOST_NAME} only.")
            print(f"Transfer the trust profile to your phone from this folder:\n  {directory}")
            print(f"Local root SHA-256 fingerprint: {fingerprint}")
            print(f"Pairing code (one use, 10 minutes)  {code}", flush=True)
            if sys.stdin.isatty():
                print("Press Enter to forget the browser and get a new code. Type share_certificate to offer\n"
                      "the phone certificate over Wi-Fi for 2 minutes. Press Ctrl+C to stop.", flush=True)
        worker = threading.Thread(target=remote.maintain, args=(stop,), daemon=True)
        worker.start()
        if args.launcher or sys.stdin.isatty():
            threading.Thread(target=local_controls, args=(remote, stop, args.launcher, share), daemon=True).start()
        server.timeout = 0.5
        while not stop.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if args.launcher:
            launcher_event("notice", message=str(error))
        sys.exit(f"Lazy Remote could not start: {error}")
    finally:
        stop.set()
        if server:
            server.server_close()
        if worker:
            worker.join()
        if server and server.remote:
            with server.remote.lock:
                server.remote.pairing.clear_codes()
                server.remote.revoke()
        if share:
            share.stop()
        if bridge:
            bridge.close()
        if instance:
            instance.close()


if __name__ == "__main__":
    main()
