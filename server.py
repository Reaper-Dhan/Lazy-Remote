#!/usr/bin/env python3
import argparse
import errno
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
PRESENCE_LIFETIME = 60
TAB_HEADER = "X-Lazy-Remote-Tab"
REQUEST_HEADER = "X-Lazy-Remote-Request"
DRAG_LIFETIME = 15
MAX_BODY = 4096
CONNECTION_TIMEOUT = 5
MAX_DELTA = 16384
MOTION_POSITION_LIMIT = 2 ** 32
DEFAULT_GESTURES = {
    "tap": True, "secondary_tap": True, "scroll": True, "horizontal_scroll": True,
    "natural_scroll": True, "pinch_zoom": False, "three_finger_drag": False,
    "swipes": {}, "swipe_fingers": {},
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
    "/pwa.js": ("pwa.js", "text/javascript; charset=utf-8"),
    "/qr-scan.js": ("qr-scan.js", "text/javascript; charset=utf-8"),
    "/vendor/jsQR.js": ("vendor/jsQR.js", "text/javascript; charset=utf-8"),
    "/service-worker.js": ("service-worker.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/offline.html": ("offline.html", "text/html; charset=utf-8"),
    "/assets/app-icon-192.png": ("assets/app-icon-192.png", "image/png"),
    "/assets/app-icon-512.png": ("assets/app-icon-512.png", "image/png"),
    "/remote.css": ("remote.css", "text/css; charset=utf-8"),
    "/assets/logo.svg": ("assets/logo.svg", "image/svg+xml"),
    "/assets/touch-icon.png": ("assets/touch-icon.png", "image/png"),
    "/apple-touch-icon.png": ("assets/apple-touch-icon.png", "image/png"),
    "/apple-touch-icon-precomposed.png": ("assets/apple-touch-icon.png", "image/png"),
}
PUBLIC_ICONS = frozenset(route for route, (_, mime) in ASSETS.items() if mime.startswith("image/"))


def mac_preferences(domain):
    try:
        exported = plistlib.loads(subprocess.check_output(
            ["/usr/bin/defaults", "export", domain, "-"], stderr=subprocess.DEVNULL, timeout=1
        ))
        return exported if isinstance(exported, dict) else {}
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return {}


def mac_swipe_shortcuts(settings, hotkeys):
    if not isinstance(hotkeys, dict):
        return {}, {}
    shortcuts = {}
    swipe_fingers = {}
    for name, (identifier, default_key, axis) in SWIPE_SHORTCUTS.items():
        fingers = []
        if not settings.get("TrackpadThreeFingerDrag") and settings.get(f"TrackpadThreeFinger{axis}SwipeGesture") == 2:
            fingers.append(3)
        if settings.get(f"TrackpadFourFinger{axis}SwipeGesture") == 2:
            fingers.append(4)
        if not fingers:
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
            swipe_fingers[name] = fingers
    return shortcuts, swipe_fingers


def mac_gesture_settings():
    settings = mac_preferences("com.apple.AppleMultitouchTrackpad") or mac_preferences(
        "com.apple.driver.AppleBluetoothMultitouch.trackpad"
    )
    hotkeys = mac_preferences("com.apple.symbolichotkeys").get("AppleSymbolicHotKeys", {})
    shortcuts, swipe_fingers = mac_swipe_shortcuts(settings, hotkeys)
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
        "swipes": shortcuts,
        "swipe_fingers": swipe_fingers,
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
            seq = command.get("seq")
            return (command.keys() == {"action", "dx", "dy", "seq"} and
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
            space_errors = {
                b"space_edge": "There is no adjacent Space in that direction.",
                b"space_permission": "Allow Accessibility access on the Mac to switch Spaces.",
                b"space_unavailable": "Could not select a Space in Mission Control. Try again after it closes.",
            }
            if reply in space_errors:
                raise RequestError(422, space_errors[reply])
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
    accepts_command = staticmethod(valid_command)

    def __init__(self, bridge, browser_path=None, on_change=None, gesture_reader=None,
                 stop_when_idle=True, presence_lifetime=PRESENCE_LIFETIME, stop_event=None):
        self.bridge = bridge
        self.pairing = Pairing(browser_path)
        self.on_change = on_change
        self.gesture_reader = gesture_reader
        self.stop_when_idle = stop_when_idle
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.gestures = gesture_reader() if gesture_reader else DEFAULT_GESTURES
        self.presence_lifetime = presence_lifetime
        self.motion = (0, 0, 0)
        self.lock = threading.Lock()
        self.input_lock = threading.Lock()
        self.input_ready = None
        self.token = self.tab = None
        self.claim_receipt = None
        self.cancelled_requests = []
        self.last_presence = 0
        self.drag_release_pending = False
        self.session_until = self.last_command = 0
        self.last_phone_activity = time.monotonic()
        self.drag_until = 0
        self.command_budget = 120
        self.budget_updated = 0

    def release_drag(self):
        if self.drag_until:
            self.drag_until = 0
            self.drag_release_pending = True

    def flush_drag_release(self):
        # Revoking a session must not wait for a slow or blocked native input call.
        if self.input_lock.acquire(blocking=False):
            try:
                self._flush_drag_release()
            finally:
                self.input_lock.release()

    def _flush_drag_release(self):
        with self.lock:
            pending, self.drag_release_pending = self.drag_release_pending, False
        if not pending:
            return
        try:
            released = self.bridge.send({"action": "drag", "active": False})
        except RequestError:
            released = False
        if not released:
            with self.lock:
                self.drag_release_pending = True

    def revoke(self):
        self.token = self.tab = None
        self.claim_receipt = None
        self.motion = (0, 0, 0)
        self.release_drag()

    def shutdown(self):
        self.stop_event.set()
        with self.lock:
            self.pairing.pending = None
            self.pairing.clear_codes()
            self.revoke()

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
            code = self.pairing.code
        self.flush_drag_release()
        return code

    def start_session(self, now):
        self.revoke()
        self.token = secrets.token_urlsafe(32)
        self.tab = secrets.token_urlsafe(32)
        self.last_presence = now
        self.session_until = now + SESSION_LIFETIME
        self.last_command = self.last_phone_activity = now
        self.command_budget = 120
        self.budget_updated = now
        self.publish()
        return self.token, self.tab

    def request_access(self, body=None, device=None, previous_session=None, previous_tab=None, request_key=None):
        with self.lock:
            now = time.monotonic()
            self.expire(now)
            pending = self.pairing.request_for(None, request_key)
            if pending:
                return pending.token
            replace = (self.token is not None and previous_session is not None and previous_tab is None
                       and hmac.compare_digest(previous_session, self.token))
            if self.token and not replace:
                raise RequestError(409, "A browser is already connected. Lock it before requesting access.")
            pending = (self.pairing.pair(body, now, request_key) if body is not None
                       else self.pairing.resume(device, now, request_key))
            if replace:
                self.revoke()
            self.publish()
        if replace:
            self.flush_drag_release()
        return pending.token

    def decide(self, match, approve):
        with self.lock:
            now = time.monotonic()
            self.expire(now)
            self.pairing.decide(match, approve, now)
            self.publish()

    def can_recover_claim(self, request_key):
        return bool(request_key and self.claim_receipt
                    and hmac.compare_digest(request_key, self.claim_receipt[0]))

    def claim(self, pending, request_key=None):
        with self.lock:
            now = time.monotonic()
            self.expire(now)
            if self.can_recover_claim(request_key):
                return self.token, self.tab, self.claim_receipt[1]
            device = self.pairing.claim(pending, request_key)
            token, tab = self.start_session(now)
            if request_key:
                # Keep a receipt until this page demonstrates that it received the session.
                self.claim_receipt = (request_key, device)
            return token, tab, device

    def expire(self, now):
        if self.stop_event.is_set():
            raise RequestError(503, "Lazy Remote is stopping.")
        if self.stop_when_idle and now - self.last_phone_activity >= SERVER_IDLE_TIMEOUT:
            self.pairing.pending = None
            self.pairing.clear_codes()
            self.revoke()
            raise RequestError(503, SERVER_IDLE_MESSAGE)
        changed = self.pairing.expire(now)
        if self.token and (now >= self.session_until or now - self.last_command >= IDLE_LIFETIME
                           or now - self.last_presence >= self.presence_lifetime):
            self.revoke()
            if not self.pairing.browser.active():
                self.pairing.generate(now)
            changed = True
        if changed:
            self.publish()
        if self.drag_until and now >= self.drag_until:
            self.release_drag()

    def require_session(self, token, tab, now):
        self.expire(now)
        if (not token or not tab or self.token is None
                or not hmac.compare_digest(token, self.token) or not hmac.compare_digest(tab, self.tab)):
            raise RequestError(401, "Request access to control the computer.")

    def status(self, token, tab, device, pending_token, request_key=None):
        with self.lock:
            now = time.monotonic()
            try:
                self.require_session(token, tab, now)
            except RequestError as error:
                if error.status != 401:
                    raise
                pending = self.pairing.request_for(pending_token, request_key)
                return 401, {
                    "remembered": self.pairing.browser.matches(device),
                    "match": pending.match if pending else None,
                    "approved": bool(pending and pending.approved) or self.can_recover_claim(request_key),
                    "new_pairing_required": bool(self.token and token and not tab
                                                 and hmac.compare_digest(token, self.token)
                                                 and not self.pairing.browser.matches(device)),
                }
            self.claim_receipt = None
            self.last_presence = now
        ready = self.input_permission(wait=False)
        with self.lock:
            self.require_session(token, tab, time.monotonic())
            gestures = {**self.gestures, "swipes": list(self.gestures["swipes"])}
            return 200, {
                "ready": ready, "dragging": bool(self.drag_until), "gestures": gestures,
                # Older open pages use this flag to decide whether to disconnect on hide.
                "protect_background": False,
                "unavailable": list(getattr(self.bridge, "unavailable_keys", ())),
            }

    def input_permission(self, request=False, wait=True):
        if not self.input_lock.acquire(blocking=wait):
            return self.input_ready
        try:
            self._flush_drag_release()
            self.input_ready = self.bridge.send({"action": "request_perm" if request else "check_perm"})
        except RequestError:
            self.input_ready = None
        finally:
            self.input_lock.release()
        return self.input_ready

    def disconnect(self, token, tab):
        with self.lock:
            now = time.monotonic()
            self.require_session(token, tab, now)
            self.revoke()
            if not self.pairing.browser.active():
                self.pairing.generate(now)
            self.publish()
        self.flush_drag_release()

    def cancel_request(self, request_key, token=None, tab=None):
        """Revoke this client's access and acknowledge retries of that same Lock."""
        with self.lock:
            now = time.monotonic()
            self.expire(now)
            pending = self.pairing.request_for(None, request_key)
            receipt = self.can_recover_claim(request_key)
            cancelled = any(hmac.compare_digest(request_key, key)
                            for key, until in self.cancelled_requests if until > now)
            credentials = bool(token and tab)
            if not (pending or receipt or cancelled or credentials):
                raise RequestError(409, "Could not confirm which request to lock. Use Lock phone on the host.")
            matches = (credentials and self.token is not None
                       and hmac.compare_digest(token, self.token) and hmac.compare_digest(tab, self.tab))
            changed = False
            if pending:
                self.pairing.pending = None
                changed = True
            if receipt or matches:
                self.revoke()
                changed = True
            if changed:
                self.pairing.generate(now)
                self.publish()
            # Credentials that don't match the active session prove that the
            # supplied session is no longer active; never revoke another one.
            self.cancelled_requests = [(key, until) for key, until in self.cancelled_requests
                                       if until > now and not hmac.compare_digest(key, request_key)][-15:]
            self.cancelled_requests.append((request_key, now + IDLE_LIFETIME))
        self.flush_drag_release()

    def command(self, token, tab, command):
        # Reject unauthenticated requests before they can wait on the native input queue.
        with self.lock:
            self.require_session(token, tab, time.monotonic())
        with self.input_lock:
            try:
                return self._command(token, tab, command)
            finally:
                self._flush_drag_release()

    def _command(self, token, tab, command):
        with self.lock:
            now = time.monotonic()
            self.require_session(token, tab, now)
            if not self.accepts_command(command):
                raise RequestError(400, "Invalid remote command.")
            self.command_budget = min(120, self.command_budget + 90 * (now - self.budget_updated))
            self.budget_updated = now
            cost = math.ceil(len(command["text"]) / 8) if command["action"] == "type" else 1
            if self.command_budget < cost:
                raise RequestError(429, "The remote is busy. Try again.")
            self.command_budget -= cost
            motion = None
            if command["action"] == "motion":
                seq, x, y = command["seq"], command["dx"], command["dy"]
                previous_seq, previous_x, previous_y = self.motion
                if seq <= previous_seq:
                    return {"ok": True, "dragging": bool(self.drag_until)}
                motion = (seq, x, y)
                command = {
                    "action": "move",
                    "dx": max(-MAX_DELTA, min(MAX_DELTA, x - previous_x)),
                    "dy": max(-MAX_DELTA, min(MAX_DELTA, y - previous_y)),
                }
            elif command["action"] == "key" and command["name"] in SWIPE_SHORTCUTS:
                shortcut = self.gestures["swipes"].get(command["name"])
                if shortcut is None:
                    raise RequestError(400, "That swipe or its keyboard shortcut is disabled on the Mac.")
                command = {"action": "shortcut", "name": command["name"], **shortcut}
            if command["action"] == "click":
                self.release_drag()

        self._flush_drag_release()
        with self.lock:
            self.require_session(token, tab, time.monotonic())
        try:
            delivered = self.bridge.send(command)
        except RequestError:
            with self.lock:
                self.release_drag()
            raise
        with self.lock:
            if token != self.token or self.stop_event.is_set():
                if delivered and command == {"action": "drag", "active": True}:
                    self.drag_release_pending = True
                raise RequestError(401, "Access was locked while the command was in progress.")
            if not delivered:
                self.release_drag()
                raise RequestError(503, PERMISSION_MESSAGE)
            if motion is not None:
                self.motion = motion
            self.last_command = self.last_phone_activity = self.last_presence = time.monotonic()
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
                if stop.is_set():
                    return
                try:
                    self.expire(now)
                except RequestError as error:
                    stop.set()
                    if self.on_change:
                        self.on_change("notice", message=str(error))
                    return
                monitor_drag = self.drag_until and getattr(self.bridge, "monitor_drag", False)
            self.flush_drag_release()
            if monitor_drag and self.input_permission(wait=False) is False:
                with self.lock:
                    self.release_drag()
                self.flush_drag_release()
            if self.gesture_reader and now >= next_gesture_check:
                gestures = self.gesture_reader()
                with self.lock:
                    self.gestures = gestures
                next_gesture_check = time.monotonic() + 10


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
        public_icon = self.path in PUBLIC_ICONS
        self.send_header("Cache-Control", "public, max-age=86400" if public_icon else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin" if public_icon else "same-origin")
        self.send_header("Permissions-Policy", "camera=(self), microphone=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
            "media-src 'self' blob:; connect-src 'self'; "
            "manifest-src 'self'; worker-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
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
        if self.command != "HEAD":
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

    def tab(self):
        values = self.headers.get_all(TAB_HEADER, [])
        return values[0] if len(values) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{43}", values[0]) else None

    def request_key(self):
        values = self.headers.get_all(REQUEST_HEADER, [])
        if not values:
            return None
        if len(values) != 1 or not re.fullmatch(r"[0-9a-f]{64}", values[0]):
            raise RequestError(400, "Invalid pairing request identifier.")
        return values[0]

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
                status, body = self.server.remote.status(
                    self.token(), self.tab(), self.token(DEVICE_COOKIE), self.token(APPROVAL_COOKIE), self.request_key()
                )
                if status == 401:
                    body["pairing_origins"] = [f"https://{host}" for host in sorted(self.server.hosts)]
                self.json_reply(status, body)
            else:
                raise RequestError(404, "Page not found.")
        except RequestError as error:
            self.json_reply(error.status, {"error": str(error)})

    def do_HEAD(self):
        if self.path in self.server.assets or self.path == "/index.html":
            self.do_GET()
        else:
            self.json_reply(404, {"error": "Page not found."})

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
                self.json_reply(200, remote.command(self.token(), self.tab(), body))
                return
            if self.path != "/api/pair" and body:
                raise RequestError(400, "Expected an empty JSON object.")
            if self.path in ("/api/pair", "/api/resume"):
                pending = remote.request_access(
                    body=body if self.path == "/api/pair" else None,
                    device=self.token(DEVICE_COOKIE),
                    previous_session=self.token(), previous_tab=self.tab(),
                    request_key=self.request_key(),
                )
                self.json_reply(202, {"ok": True}, cookies=((APPROVAL_COOKIE, pending, APPROVAL_LIFETIME),))
            elif self.path == "/api/claim":
                token, tab, device = remote.claim(self.token(APPROVAL_COOKIE), self.request_key())
                cookies = [(COOKIE, token, None), (APPROVAL_COOKIE, "", APPROVAL_LIFETIME)]
                if device is not None:
                    cookies.append((DEVICE_COOKIE, device, DEVICE_LIFETIME))
                self.json_reply(200, {"ok": True, "tab": tab, "protect_background": False},
                                cookies=cookies)
            else:
                remote.disconnect(self.token(), self.tab())
                self.json_reply(200, {"ok": True})
        except RequestError as error:
            self.json_reply(error.status, {"error": str(error)})


LAUNCHER_OUTPUT_LOCK = threading.Lock()


def launcher_event(event, **data):
    try:
        with LAUNCHER_OUTPUT_LOCK:
            print(json.dumps({"event": event, **data}), flush=True)
    except BrokenPipeError:
        pass


def terminal_event(event, **data):
    if event == "notice":
        print(data["message"], flush=True)
    elif data["match"] and not data["approved"]:
        print(f"Phone requests access. Compare {data['match']} on both screens, "
              f"then type approve {data['match']} or deny {data['match']}.", flush=True)


def local_controls(remote, stop, launcher):
    bluetooth_requests = None
    for line in sys.stdin:
        if stop.is_set():
            return
        command = line.strip()
        try:
            if launcher and command.startswith("bluetooth "):
                import queue
                from bluetooth_transport import start_requests
                if bluetooth_requests is None:
                    bluetooth_requests = start_requests(remote, stop, launcher_event)
                try:
                    bluetooth_requests.put_nowait(command.removeprefix("bluetooth "))
                except queue.Full:
                    launcher_event("notice", message="Bluetooth requests are arriving too quickly.")
                continue
            decision = re.fullmatch(r"(approve|deny) ([0-9]{6})", command)
            if decision:
                remote.decide(decision[2], decision[1] == "approve")
            elif launcher and command in ("check_access", "request_input_access"):
                launcher_event("permission", input_ready=remote.input_permission(
                    request=command == "request_input_access", wait=False
                ))
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
        self.monitor_drag = getattr(backend, "monitor_drag", False)
        # Remote keys this computer cannot send, so the phone can hide their buttons.
        self.unavailable_keys = sorted(backend.unavailable_keys())

    def send(self, command):
        try:
            return self.backend.send(command)
        except OSError:
            if self.monitor_drag:
                self.backend.close(release=False)
            raise RequestError(503, PERMISSION_MESSAGE) from None
        except ValueError:
            raise RequestError(503, f"The {HOST_NAME} input helper is unavailable. Try again.") from None

    def close(self):
        self.backend.close()


def main():
    parser = argparse.ArgumentParser(description="Control this computer from one paired browser over HTTPS.")
    parser.add_argument("--bind", metavar="IPV4", help="Listen on this host's private IPv4 address.")
    parser.add_argument("--renew-certificate", action="store_true", help="Replace this computer's HTTPS identity.")
    parser.add_argument("--keep-running", action="store_true",
                        help="Do not stop after 30 minutes without phone activity. The phone still locks after 5.")
    parser.add_argument("--allow-background-resume", action="store_true",
                        help="Allow up to five minutes between check-ins instead of one minute.")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--launcher", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.exit("Start Lazy Remote from your user account without sudo.")
    server = bridge = worker = instance = None
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        port = int(os.environ.get("REMOTE_PORT", "8765"))
        if not 0 <= port <= 65535:
            raise ValueError("REMOTE_PORT must be between 0 and 65535.")
        address, hostname = local_endpoints(args.bind)
        try:
            server = RemoteServer((address, port))
        except OSError as error:
            if error.errno == errno.EADDRNOTAVAIL or getattr(error, "winerror", None) == 10049:
                raise ValueError(f"{address} is not assigned to this {HOST_NAME}. "
                                 "Connect to the network first, then check the host IPv4 address.") from None
            raise
        port = server.server_address[1]
        state = private_state_dir(args.state_dir)
        instance = (state / "instance.lock").open("a")
        try:
            lock_file(instance, blocking=False)
        except BlockingIOError:
            raise RuntimeError("Lazy Remote is already running. Stop the other instance first.") from None
        context, directory, names, fingerprint = tls_identity(address, hostname, args.renew_certificate, state)
        server.configure_tls(context, names)
        bridge = open_input(args.launcher)
        remote = server.remote = Remote(
            bridge, state / "browser.json", on_change=launcher_event if args.launcher else terminal_event,
            gesture_reader=mac_gesture_settings if IS_MAC else None, stop_when_idle=not args.keep_running,
            presence_lifetime=IDLE_LIFETIME if args.allow_background_resume else PRESENCE_LIFETIME,
            stop_event=stop,
        )
        code = remote.new_code(publish=False)
        urls = [f"https://{name}{f':{port}' if port != 443 else ''}/" for name in names]
        phone_files = directory / "phone"
        loopback = address == "127.0.0.1"
        if args.launcher:
            launcher_event(
                "ready", urls=urls, certificates=str(phone_files), fingerprint=fingerprint, loopback=loopback,
                input_ready=remote.input_permission(), **remote.snapshot()
            )
        else:
            print("Lazy Remote is running. Open one of these HTTPS addresses on your phone:")
            for url in urls:
                print(f"  {url}")
            if loopback:
                print(f"No private network address found. This launch is available on the {HOST_NAME} only.")
            print(f"Transfer the trust profile to your phone from this folder:\n  {phone_files}")
            print(f"Local root SHA-256 fingerprint: {fingerprint}")
            print(f"Pairing code (one use, 10 minutes)  {code}", flush=True)
            if sys.stdin.isatty():
                print("Press Enter to forget the browser and get a new code, or Ctrl+C to stop.", flush=True)
        worker = threading.Thread(target=remote.maintain, args=(stop,), daemon=True)
        worker.start()
        if args.launcher or sys.stdin.isatty():
            threading.Thread(target=local_controls, args=(remote, stop, args.launcher), daemon=True).start()
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
        if server and server.remote:
            server.remote.shutdown()
        if server:
            server.server_close()
        if worker:
            worker.join()
        if bridge:
            if server and server.remote:
                with server.remote.input_lock:
                    server.remote._flush_drag_release()
                    bridge.close()
            else:
                bridge.close()
        if instance:
            instance.close()


if __name__ == "__main__":
    main()
