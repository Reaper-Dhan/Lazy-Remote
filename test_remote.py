import http.client
import json
import os
from pathlib import Path
import plistlib
import runpy
import ssl
import stat
import tempfile
import threading
import time
import unittest

from server import (
    APPROVAL_COOKIE, COOKIE, DEVICE_COOKIE, IDLE_LIFETIME, SERVER_IDLE_MESSAGE,
    SERVER_IDLE_TIMEOUT, PRESENCE_LIFETIME, REQUEST_HEADER, TAB_HEADER, Remote, RemoteServer, mac_swipe_shortcuts,
)
from tls_setup import tls_identity


class RecordingBridge:
    def __init__(self):
        self.commands = []
        self.ready = True

    def send(self, command):
        self.commands.append(command)
        return self.ready


class RemoteSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.state = Path(temporary.name) / "tls"
        cls.context, cls.certificates, _, _ = tls_identity("127.0.0.1", None, False, cls.state)
        cls.trust = ssl.create_default_context(cafile=str(cls.certificates / "root.pem"))

    def setUp(self):
        self.bridge = RecordingBridge()
        self.tabs = {}
        self.server = RemoteServer(("127.0.0.1", 0))
        self.server.configure_tls(self.context, ["127.0.0.1"])
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.browser_path = Path(storage.name) / "browser.json"
        self.remote = self.server.remote = Remote(self.bridge, self.browser_path)
        self.code = self.remote.new_code()
        self.port = self.server.server_address[1]
        self.origin = f"https://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, *, cookie=None, headers=None, context=None):
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, context=context or self.trust, timeout=10)
        request_headers = {"Origin": self.origin, "Content-Type": "application/json"}
        if cookie:
            request_headers["Cookie"] = cookie
            if cookie in self.tabs:
                request_headers[TAB_HEADER] = self.tabs[cookie]
        request_headers.update(headers or {})
        try:
            if body is None:
                connection.request("GET", path, headers=request_headers)
            else:
                connection.request("POST", path, json.dumps(body), request_headers)
            response = connection.getresponse()
            result = response.read()
            if path == "/api/claim" and response.status == 200:
                self.tabs[response.headers["Set-Cookie"].split(";", 1)[0]] = json.loads(result)["tab"]
            return response.status, response.headers, result
        finally:
            connection.close()

    def begin(self, body=None, device=None):
        status, headers, _ = self.request(
            "/api/resume" if device else "/api/pair",
            {} if device else body or {"code": self.code},
            cookie=device,
        )
        self.assertEqual(status, 202)
        return headers["Set-Cookie"].split(";", 1)[0]

    def pair(self):
        pending = self.begin()
        self.remote.decide(self.remote.snapshot()["match"], True)
        status, headers, _ = self.request("/api/claim", {}, cookie=pending)
        self.assertEqual(status, 200)
        return headers["Set-Cookie"]

    def test_https_and_same_origin(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.request("/", context=ssl.create_default_context())
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"Pair this browser", body)
        self.assertEqual(self.request("/", headers={"Host": "other.invalid"})[0], 421)
        self.assertEqual(self.request("/api/pair", {"code": self.code}, headers={
            "Origin": "https://other.invalid"
        })[0], 403)
        self.assertEqual(self.request("/api/pair", {"code": self.code}, headers={
            "Content-Type": "text/plain"
        })[0], 415)
        self.pair()

    def test_pairing_and_revocation(self):
        command = {"action": "key", "name": "space"}
        self.assertEqual(self.request("/api/status")[0], 401)
        self.assertEqual(self.request("/api/cmd", command)[0], 401)
        self.assertFalse(self.bridge.commands)
        header = self.pair()
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, header)
        cookie = header.split(";", 1)[0]
        self.assertEqual(self.request("/api/pair", {"code": self.code})[0], 409)
        self.assertEqual(self.request("/api/cmd", command, cookie=cookie, headers={
            "Origin": "https://other.invalid"
        })[0], 403)
        self.assertEqual(self.request("/api/cmd", command, cookie=cookie)[0], 200)
        self.assertEqual(self.bridge.commands, [command])
        self.assertEqual(self.request("/api/disconnect", {}, cookie=cookie)[0], 200)
        self.assertEqual(self.request("/api/cmd", command, cookie=cookie)[0], 401)
        self.code = self.remote.new_code()
        new_cookie = self.pair().split(";", 1)[0]
        self.remote.last_command -= IDLE_LIFETIME + 1
        self.assertEqual(self.request("/api/status", cookie=new_cookie)[0], 401)

    def test_phone_activity_controls_server_shutdown(self):
        # Step the recorded time back before each action: Windows' monotonic clock only
        # ticks every ~15 ms, so two quick requests can otherwise share one reading.
        started = self.remote.last_phone_activity = self.remote.last_phone_activity - 10
        pending = self.begin()
        self.assertEqual(self.request("/api/status", cookie=pending)[0], 401)
        self.assertEqual(self.remote.last_phone_activity, started)

        self.remote.decide(self.remote.snapshot()["match"], True)
        _, headers, _ = self.request("/api/claim", {}, cookie=pending)
        session = headers["Set-Cookie"].split(";", 1)[0]
        self.assertGreater(self.remote.last_phone_activity, started)
        claimed = self.remote.last_phone_activity = self.remote.last_phone_activity - 10
        self.assertEqual(self.request("/api/status", cookie=session)[0], 200)
        self.assertEqual(self.remote.last_phone_activity, claimed)

        command = {"action": "key", "name": "space"}
        self.assertEqual(self.request("/api/cmd", command, cookie=session)[0], 200)
        used = self.remote.last_phone_activity
        self.assertGreater(used, claimed)
        self.assertEqual(self.request("/api/disconnect", {}, cookie=session)[0], 200)
        self.assertEqual(self.remote.last_phone_activity, used)

        self.remote.last_phone_activity -= SERVER_IDLE_TIMEOUT
        code = self.remote.snapshot()["code"]
        self.assertEqual(self.request("/api/pair", {"code": code})[0], 503)
        self.assertIsNone(self.remote.snapshot()["code"])
        events = []
        self.remote.on_change = lambda event, **data: events.append((event, data))
        stop = threading.Event()
        self.remote.maintain(stop)
        self.assertTrue(stop.is_set())
        self.assertEqual(events, [("notice", {"message": SERVER_IDLE_MESSAGE})])

    def test_keep_running_skips_the_idle_stop_but_still_locks_the_phone(self):
        self.remote.stop_when_idle = False
        cookie = self.pair().split(";", 1)[0]
        self.remote.last_phone_activity -= SERVER_IDLE_TIMEOUT
        self.remote.last_command -= IDLE_LIFETIME
        self.assertEqual(self.request("/api/cmd", {"action": "key", "name": "space"}, cookie=cookie)[0], 401)
        self.assertIsNotNone(self.remote.snapshot()["code"])  # still serving, with a fresh QR code

    def test_session_cookie_needs_the_approved_tab_and_foreground_lease(self):
        cookie = self.pair().split(";", 1)[0]
        tab = self.tabs.pop(cookie)
        self.assertEqual(self.request("/api/status", cookie=cookie)[0], 401)
        self.assertEqual(self.request("/api/cmd", {"action": "key", "name": "space"}, cookie=cookie)[0], 401)
        self.tabs[cookie] = tab
        self.remote.last_presence -= 30
        self.assertEqual(self.request("/api/status", cookie=cookie)[0], 200)
        self.remote.last_presence -= PRESENCE_LIFETIME
        self.assertEqual(self.request("/api/status", cookie=cookie)[0], 401)

    def test_extended_reconnect_window_still_expires(self):
        self.remote.presence_lifetime = IDLE_LIFETIME
        cookie = self.pair().split(";", 1)[0]
        self.remote.last_presence -= PRESENCE_LIFETIME
        self.assertEqual(self.request("/api/status", cookie=cookie)[0], 200)
        self.remote.last_presence -= IDLE_LIFETIME
        self.assertEqual(self.request("/api/status", cookie=cookie)[0], 401)

    def test_reloaded_remembered_browser_can_request_a_new_session(self):
        pending = self.begin({"code": self.code, "remember": True})
        self.remote.decide(self.remote.snapshot()["match"], True)
        _, headers, _ = self.request("/api/claim", {}, cookie=pending)
        session, device = (value.split(";", 1)[0] for value in headers.get_all("Set-Cookie")
                           if not value.startswith(APPROVAL_COOKIE))
        self.assertEqual(self.request("/api/resume", {}, cookie=device)[0], 409)
        status, headers, _ = self.request("/api/resume", {}, cookie=f"{session}; {device}")
        self.assertEqual(status, 202)
        next_pending = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/api/claim", {}, cookie=next_pending)[0], 409)
        self.assertEqual(self.request("/api/status", cookie=session)[0], 401)
        self.remote.decide(self.remote.snapshot()["match"], True)
        self.assertEqual(self.request("/api/claim", {}, cookie=next_pending)[0], 200)

    def test_damaged_browser_record_still_allows_a_new_pairing(self):
        self.browser_path.write_bytes(b'{"digest":')
        self.remote = self.server.remote = Remote(self.bridge, self.browser_path)
        self.assertFalse(self.remote.snapshot()["remembered"])
        self.code = self.remote.new_code()
        self.pair()

    def test_stopping_host_refuses_input_before_the_listener_closes(self):
        session = self.pair().split(";", 1)[0]
        self.remote.stop_event.set()
        self.assertEqual(self.request("/api/cmd", {"action": "key", "name": "space"}, cookie=session)[0], 503)
        self.assertEqual(self.request("/api/status", cookie=session)[0], 503)
        self.assertFalse(self.bridge.commands)

    def test_same_page_recovers_pairing_and_claim_after_responses_are_lost(self):
        key = {REQUEST_HEADER: "a" * 64}
        wrong_key = {REQUEST_HEADER: "b" * 64}
        pair = {"qr": self.remote.snapshot()["qr"], "remember": True}
        status, headers, _ = self.request("/api/pair", pair, headers=key)
        self.assertEqual(status, 202)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, retry, _ = self.request("/api/pair", pair, headers=key)
        self.assertEqual(status, 202)
        self.assertEqual(headers["Set-Cookie"], retry["Set-Cookie"])
        match = self.remote.snapshot()["match"]
        self.assertEqual(json.loads(self.request("/api/status", headers=key)[2])["match"], match)
        self.assertIsNone(json.loads(self.request("/api/status", cookie=cookie)[2])["match"])
        self.assertEqual(self.request("/api/claim", {}, headers=key)[0], 409)
        self.remote.decide(match, True)
        self.assertEqual(self.request("/api/claim", {}, cookie=cookie, headers=wrong_key)[0], 401)
        status, issued, body = self.request("/api/claim", {}, headers=key)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(self.request("/api/status", headers=key)[2])["approved"])
        status, recovered, recovered_body = self.request("/api/claim", {}, headers=key)
        self.assertEqual(status, 200)
        self.assertEqual(issued.get_all("Set-Cookie"), recovered.get_all("Set-Cookie"))
        self.assertEqual(body, recovered_body)
        session = issued["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/api/status", cookie=session, headers=key)[0], 200)
        self.assertEqual(self.request("/api/claim", {}, headers=key)[0], 401)

    def test_tv_host_requires_approval_and_only_dispatches_tv_buttons(self):
        tv = runpy.run_path(str(Path(__file__).parent / "android-tv/app/src/main/python/android_host.py"))

        class Tv:
            def __init__(self):
                self.actions = []

            def ready(self):
                return True

            def availableKeys(self):
                return ["up", "enter", "tv_back", "tv_home"]

            def press(self, action):
                if action not in self.availableKeys():
                    return False
                self.actions.append(action)
                return True

        platform = Tv()
        self.remote = self.server.remote = tv["TvRemote"](tv["TvInput"](platform), self.browser_path)
        self.code = self.remote.new_code()
        up = {"action": "key", "name": "up"}
        status, _, body = self.request("/api/status")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["host"], "tv")
        self.assertEqual(self.request("/api/cmd", up)[0], 401)
        cookie = self.pair().split(";", 1)[0]
        status, _, body = self.request("/api/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("media_play", json.loads(body)["unavailable"])
        self.assertEqual(self.request("/api/cmd", up, cookie=cookie)[0], 200)
        self.assertEqual(self.request("/api/cmd", {"action": "click", "button": "left", "count": 1}, cookie=cookie)[0], 400)
        self.assertEqual(self.request("/api/cmd", {"action": "key", "name": "media_play"}, cookie=cookie)[0], 422)
        self.assertEqual(platform.actions, ["up"])

    def test_revoke_does_not_wait_for_native_input(self):
        cookie = self.pair().split(";", 1)[0]
        started = threading.Event()
        finish = threading.Event()

        def slow_input(command):
            self.bridge.commands.append(command)
            if command == {"action": "drag", "active": True}:
                started.set()
                finish.wait(5)
            return True

        self.bridge.send = slow_input
        results = []
        thread = threading.Thread(target=lambda: results.append(self.request(
            "/api/cmd", {"action": "drag", "active": True}, cookie=cookie
        )[0]))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(self.request("/api/disconnect", {}, cookie=cookie)[0], 200)
            self.assertTrue(thread.is_alive())
        finally:
            finish.set()
            thread.join(5)
        self.assertEqual(results, [401])
        self.assertEqual(self.bridge.commands[-1], {"action": "drag", "active": False})

    def test_pairing_expiry_and_attempt_limit(self):
        self.remote.pairing.until = time.monotonic() - 1
        self.assertEqual(self.request("/api/pair", {"code": self.code})[0], 409)
        self.code = self.remote.new_code()
        for _ in range(8):
            self.assertEqual(self.request("/api/pair", {"code": "wrong"})[0], 403)
        self.assertEqual(self.request("/api/pair", {"code": self.code})[0], 429)

    def test_input_validation_and_permission(self):
        cookie = self.pair().split(";", 1)[0]
        for body in (
            [],
            {"action": "check_perm"},
            {"action": "request_perm"},
            {"action": "key", "name": []},
            {"action": "click", "button": "left", "count": 0},
            {"action": "scroll", "dx": 0, "dy": 2147483648},
            {"action": "move", "dx": float("nan"), "dy": 0},
            {"action": "move", "dx": True, "dy": 0},
            {"action": "type", "text": "\ud800"},
            {"action": "type", "text": "a" * 129},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.request("/api/cmd", body, cookie=cookie)[0], 400)
        self.assertEqual(self.request("/api/cmd", {"action": "type", "text": "a" * 4096}, cookie=cookie)[0], 413)
        self.assertFalse(self.bridge.commands)
        self.bridge.ready = False
        _, _, body = self.request("/api/status", cookie=cookie)
        self.assertFalse(json.loads(body)["ready"])
        self.assertEqual(self.request("/api/cmd", {"action": "key", "name": "space"}, cookie=cookie)[0], 503)

    def test_motion_keeps_the_latest_sample_without_replaying_older_samples(self):
        cookie = self.pair().split(";", 1)[0]
        packet = {"action": "motion", "seq": 2, "dx": 10.5, "dy": -3}
        self.assertEqual(self.request("/api/cmd", packet)[0], 401)
        self.assertEqual(self.request("/api/cmd", packet, cookie=cookie)[0], 200)
        last_activity = self.remote.last_phone_activity
        self.assertEqual(self.request("/api/cmd", dict(packet, seq=1, dx=6), cookie=cookie)[0], 200)
        self.assertEqual(self.request("/api/cmd", packet, cookie=cookie)[0], 200)
        self.assertEqual(self.remote.last_phone_activity, last_activity)
        self.assertEqual(self.request("/api/cmd", dict(packet, seq=3, dx=8, dy=-1), cookie=cookie)[0], 200)
        self.assertEqual(self.bridge.commands, [
            {"action": "move", "dx": 10.5, "dy": -3},
            {"action": "move", "dx": -2.5, "dy": 2},
        ])
        self.assertEqual(self.request("/api/cmd", dict(packet, seq=4, dx=float("inf")), cookie=cookie)[0], 400)
        self.assertEqual(self.request("/api/disconnect", {}, cookie=cookie)[0], 200)
        self.assertEqual(self.remote.motion, (0, 0, 0))

    def test_swipes_follow_enabled_mac_shortcuts_and_finger_counts(self):
        trackpad = {
            "TrackpadThreeFingerHorizSwipeGesture": 2,
            "TrackpadThreeFingerVertSwipeGesture": 2,
            "TrackpadFourFingerHorizSwipeGesture": 2,
        }
        hotkeys = {
            "32": {"enabled": False},
            "33": {"enabled": True, "value": {"type": "standard", "parameters": [65535, 12, 1 << 20]}},
        }
        shortcuts, fingers = mac_swipe_shortcuts(trackpad, hotkeys)
        self.assertEqual(shortcuts["swipe_left"], {"key_code": 124, "flags": 1 << 18})
        self.assertEqual(shortcuts["swipe_right"], {"key_code": 123, "flags": 1 << 18})
        self.assertNotIn("swipe_up", shortcuts)
        self.assertEqual(shortcuts["swipe_down"], {"key_code": 12, "flags": 1 << 20})
        self.assertEqual(fingers, {"swipe_left": [3, 4], "swipe_right": [3, 4], "swipe_down": [3]})
        _, drag_fingers = mac_swipe_shortcuts(dict(trackpad, TrackpadThreeFingerDrag=1), hotkeys)
        self.assertEqual(drag_fingers, {"swipe_left": [4], "swipe_right": [4]})
        self.remote.gestures = {**self.remote.gestures, "swipes": shortcuts, "swipe_fingers": fingers}
        cookie = self.pair().split(";", 1)[0]
        _, _, body = self.request("/api/status", cookie=cookie)
        self.assertEqual(set(json.loads(body)["gestures"]["swipes"]), set(shortcuts))
        self.assertEqual(json.loads(body)["gestures"]["swipe_fingers"], fingers)
        self.bridge.commands.clear()
        self.assertEqual(self.request("/api/cmd", {"action": "key", "name": "swipe_up"}, cookie=cookie)[0], 400)
        self.assertEqual(self.request("/api/cmd", {
            "action": "shortcut", "key_code": 12, "flags": 1 << 20,
        }, cookie=cookie)[0], 400)
        self.assertEqual(self.request("/api/cmd", {"action": "key", "name": "swipe_down"}, cookie=cookie)[0], 200)
        self.assertEqual(self.bridge.commands, [{"action": "shortcut", "name": "swipe_down", "key_code": 12, "flags": 1 << 20}])

    def test_drag_releases_when_expired_or_revoked(self):
        cookie = self.pair().split(";", 1)[0]
        hold = {"action": "drag", "active": True}
        release = {"action": "drag", "active": False}
        self.assertEqual(self.request("/api/cmd", hold, cookie=cookie)[0], 200)
        with self.remote.lock:
            self.remote.last_presence = self.remote.drag_until - 1
            self.remote.expire(self.remote.drag_until)
        self.remote.flush_drag_release()
        self.assertEqual(self.bridge.commands, [hold, release])
        self.assertEqual(self.request("/api/cmd", hold, cookie=cookie)[0], 200)
        self.remote.new_code()
        self.assertEqual(self.bridge.commands, [hold, release, hold, release])
        self.assertEqual(self.request("/api/cmd", hold, cookie=cookie)[0], 401)

    def test_click_releases_an_active_drag_before_sending_mouse_button(self):
        cookie = self.pair().split(";", 1)[0]
        hold = {"action": "drag", "active": True}
        release = {"action": "drag", "active": False}
        left = {"action": "click", "button": "left", "count": 1}
        right = {"action": "click", "button": "right", "count": 1}
        for click in (left, right):
            self.assertEqual(self.request("/api/cmd", hold, cookie=cookie)[0], 200)
            status, _, body = self.request("/api/cmd", click, cookie=cookie)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)["dragging"])
        self.assertEqual(self.bridge.commands, [hold, release, left, hold, release, right])

    def test_qr_requires_matching_local_approval(self):
        qr = self.remote.snapshot()["qr"]
        pending = self.begin({"qr": qr, "remember": False})
        self.assertTrue(pending.startswith(APPROVAL_COOKIE + "="))
        self.assertEqual(self.request("/api/pair", {"qr": qr})[0], 409)
        self.assertEqual(self.request("/api/claim", {}, cookie=pending)[0], 409)
        command = {"action": "key", "name": "space"}
        self.assertEqual(self.request("/api/cmd", command, cookie=pending)[0], 401)
        self.assertFalse(self.bridge.commands)
        _, _, body = self.request("/api/status", cookie=pending)
        match = json.loads(body)["match"]
        _, _, unrelated = self.request("/api/status")
        self.assertIsNone(json.loads(unrelated)["match"])
        with self.assertRaisesRegex(Exception, "expired"):
            self.remote.decide("wrong", True)
        self.remote.decide(match, True)
        self.assertEqual(self.request("/api/claim", {})[0], 401)
        status, headers, _ = self.request("/api/claim", {}, cookie=pending)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/claim", {}, cookie=pending)[0], 401)
        self.assertFalse(self.browser_path.exists())
        session = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/api/cmd", command, cookie=session)[0], 200)

    def test_denied_and_expired_approval_cannot_be_claimed(self):
        pending = self.begin()
        self.remote.decide(self.remote.snapshot()["match"], False)
        self.assertEqual(self.request("/api/claim", {}, cookie=pending)[0], 401)
        self.code = self.remote.snapshot()["code"]
        pending = self.begin()
        self.remote.decide(self.remote.snapshot()["match"], True)
        self.remote.pairing.pending.until = time.monotonic() - 1
        self.assertEqual(self.request("/api/claim", {}, cookie=pending)[0], 401)

    def test_remembered_browser_needs_approval_after_restart_and_lock(self):
        pending = self.begin({"code": self.code, "remember": True})
        self.remote.decide(self.remote.snapshot()["match"], True)
        _, headers, _ = self.request("/api/claim", {}, cookie=pending)
        device_header = next(value for value in headers.get_all("Set-Cookie") if value.startswith(DEVICE_COOKIE))
        self.assertIn("Secure; HttpOnly; SameSite=Strict", device_header)
        device = device_header.split(";", 1)[0]
        self.assertNotIn(device.split("=", 1)[1], self.browser_path.read_text())
        if os.name != "nt":  # Windows has no POSIX file modes.
            self.assertEqual(stat.S_IMODE(self.browser_path.stat().st_mode), 0o600)

        self.remote = self.server.remote = Remote(self.bridge, self.browser_path)
        self.remote.new_code()
        _, _, body = self.request("/api/status", cookie=device)
        self.assertTrue(json.loads(body)["remembered"])
        self.assertEqual(self.request("/api/resume", {}, cookie=device, headers={
            "Origin": "https://other.invalid"
        })[0], 403)
        pending = self.begin(device=device)
        self.assertEqual(self.request("/api/claim", {}, cookie=pending)[0], 409)
        self.remote.decide(self.remote.snapshot()["match"], True)
        _, headers, _ = self.request("/api/claim", {}, cookie=pending)
        session = headers["Set-Cookie"].split(";", 1)[0]
        self.assertTrue(session.startswith(COOKIE + "="))
        self.assertEqual(self.request("/api/disconnect", {}, cookie=session)[0], 200)
        self.assertEqual(self.request("/api/status", cookie=session)[0], 401)
        self.assertEqual(self.request("/api/resume", {}, cookie=device)[0], 202)
        self.remote.new_code(forget=True)
        self.assertEqual(self.request("/api/resume", {}, cookie=device)[0], 401)
        self.assertFalse(self.browser_path.exists())

    def test_certificate_profile_and_renewal(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "tls"
            _, path, names, fingerprint = tls_identity("192.168.1.2", "couch.local", False, state)
            self.assertEqual(names, ["192.168.1.2", "couch.local"])
            if os.name != "nt":  # Windows has no POSIX file modes.
                self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((path / "server.key").stat().st_mode), 0o600)
            phone = path / "phone"
            profile = plistlib.loads((phone / "Lazy Remote.mobileconfig").read_bytes())
            self.assertEqual(profile["PayloadContent"][0]["PayloadContent"],
                             (phone / "Lazy Remote Root.cer").read_bytes())
            _, current, _, _ = tls_identity("192.168.1.2", "couch.local", False, Path(temporary) / "other")
            other_profile = plistlib.loads((current / "phone" / "Lazy Remote.mobileconfig").read_bytes())
            self.assertNotEqual(profile["PayloadIdentifier"], other_profile["PayloadIdentifier"])
            self.assertEqual(tls_identity("192.168.1.3", "couch.local", False, state)[2:],
                             (["192.168.1.3", "couch.local"], fingerprint))
            self.assertEqual(tls_identity("192.168.1.4", None, False, state)[2:],
                             (["192.168.1.4"], fingerprint))
            previous = path.resolve()
            self.assertNotEqual(tls_identity("192.168.1.3", "couch.local", True, state)[3], fingerprint)
            self.assertFalse(previous.exists())

    def test_certificate_format_upgrade_rotates_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "tls"
            old = tls_identity("192.168.1.2", None, False, state)[3]
            _, path, _, updated = tls_identity("192.168.1.2", None, False, state, identity_version=4)
            self.assertNotEqual(updated, old)
            self.assertEqual((path / "version").read_text(), "4")
            self.assertEqual(tls_identity("192.168.1.3", None, False, state, identity_version=4)[3], updated)


class BluetoothSecurityTests(unittest.TestCase):
    def setUp(self):
        self.bridge = RecordingBridge()
        self.remote = Remote(self.bridge)
        self.code = self.remote.new_code()
        self.key = "a" * 64

    def request(self, operation, *, key=None, **fields):
        from bluetooth_transport import local_request
        import base64
        message = {"id": "test-1", "op": operation, "request_key": key or self.key, **fields}
        reply = local_request(self.remote, base64.b64encode(json.dumps(message).encode()).decode())
        return json.loads(base64.b64decode(reply))

    def pair(self):
        pending = self.request("pair", body={"code": self.code})
        self.assertEqual(pending["status"], 202)
        self.assertEqual(self.request("claim")["status"], 409)
        self.remote.decide(self.remote.snapshot()["match"], True)
        session = self.request("claim")
        self.assertEqual(session["status"], 200)
        return {field: session[field] for field in ("token", "tab")}

    def test_controls_require_code_host_approval_and_both_session_credentials(self):
        command = {"action": "click", "button": "left", "count": 1}
        self.assertEqual(self.request("cmd", body=command)["status"], 401)
        self.assertEqual(self.request("pair", body={"code": "wrong"})["status"], 403)
        session = self.pair()
        self.assertEqual(self.request("cmd", token=session["token"], body=command)["status"], 401)
        self.assertEqual(self.request("cmd", **session, body=command)["status"], 200)
        self.assertEqual(self.bridge.commands, [command])
        self.assertEqual(self.request("disconnect", **session)["status"], 200)
        self.assertEqual(self.request("cmd", **session, body=command)["status"], 401)

    def test_lost_claim_reply_is_recoverable_only_by_its_request_key(self):
        session = self.pair()
        self.assertEqual(self.request("claim", key="b" * 64)["status"], 401)
        self.assertEqual(self.request("claim")["token"], session["token"])
        self.assertEqual(self.request("status", **session)["status"], 200)
        self.assertEqual(self.request("claim")["status"], 401)

    def test_lock_cancels_pending_approval_or_unconfirmed_claim(self):
        self.request("pair", body={"code": self.code})
        self.request("cancel", key="b" * 64)
        self.assertIsNotNone(self.remote.pairing.pending)
        self.request("cancel")
        self.assertIsNone(self.remote.pairing.pending)
        self.assertEqual(self.request("claim")["status"], 401)
        self.code = self.remote.pairing.code
        session = self.pair()
        self.request("cancel")
        self.assertEqual(self.request("status", **session)["status"], 401)

    def test_lock_revokes_a_confirmed_session(self):
        session = self.pair()
        self.request("status", **session)
        self.request("cancel", **session)
        self.assertIsNone(self.remote.token)

    def test_invalid_or_oversized_frames_never_reach_controls(self):
        from bluetooth_transport import local_request
        import base64
        for message in ([], {"id": "invalid id"}, {"id": "a" * 65}):
            with self.assertRaises(ValueError):
                local_request(self.remote, base64.b64encode(json.dumps(message).encode()).decode())
        for encoded in ("!", "a" * 12001, base64.b64encode(b" " * 8193).decode()):
            with self.assertRaises(ValueError):
                local_request(self.remote, encoded)
        self.assertEqual(self.request("status", key="bad")["status"], 400)
        self.assertEqual(self.request("cmd", token="bad", body={})["status"], 400)
        self.assertFalse(self.bridge.commands)

    def test_host_lock_does_not_wait_for_a_slow_bluetooth_control(self):
        import base64
        from unittest.mock import patch
        from server import local_controls
        started, release, replied = threading.Event(), threading.Event(), threading.Event()
        stop = threading.Event()
        session = self.pair()
        message = {"id": "slow-input", "op": "cmd", "request_key": self.key, **session,
                   "body": {"action": "key", "name": "space"}}
        encoded = base64.b64encode(json.dumps(message).encode()).decode()

        def send(command):
            started.set()
            self.assertTrue(release.wait(timeout=3))
            return True

        def lines():
            yield "bluetooth " + encoded + "\n"
            self.assertTrue(started.wait(timeout=3))
            yield "new_code\n"
            self.assertIsNone(self.remote.token, "Host Lock must revoke while input is still blocked")
            release.set()
            self.assertTrue(replied.wait(timeout=3))

        def emit(event, **fields):
            if event == "bluetooth":
                result = json.loads(base64.b64decode(fields["response"]))
                self.assertEqual(result["status"], 401)
                replied.set()

        try:
            with patch.object(self.bridge, "send", send), patch("server.sys.stdin", lines()), patch("server.launcher_event", emit):
                local_controls(self.remote, stop, True)
        finally:
            release.set()
            stop.set()


if __name__ == "__main__":
    unittest.main()
