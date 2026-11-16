"""Tests for the Windows and Linux host code. They run on any OS with fake devices."""
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import input_backends
from input_backends import KEYMAP, InputBackend, LinuxInput, WindowsInput
from server import KEYS, SWIPE_SHORTCUTS, LocalInput
from auth import RequestError
import tls_setup


class Recording(InputBackend):
    def __init__(self, ready=True):
        super().__init__()
        self.events, self.is_ready = [], ready

    def ready(self):
        return self.is_ready

    def move_by(self, dx, dy):
        self.events.append(("move", dx, dy))

    def button(self, name, down):
        self.events.append(("button", name, down))

    def wheel(self, vertical, horizontal):
        self.events.append(("wheel", vertical, horizontal))

    def key(self, name, down):
        self.events.append(("key", name, down))

    def text(self, value):
        self.events.append(("text", value))


class TranslationTests(unittest.TestCase):
    def test_every_remote_key_has_a_mapping(self):
        self.assertEqual(set(KEYMAP), KEYS - SWIPE_SHORTCUTS.keys())

    def test_command_shortcuts_become_ctrl_and_release_in_reverse(self):
        backend = Recording()
        self.assertTrue(backend.send({"action": "key", "name": "reopen_tab"}))
        self.assertEqual(backend.events, [
            ("key", "ctrl", True), ("key", "shift", True), ("key", "t", True),
            ("key", "t", False), ("key", "shift", False), ("key", "ctrl", False),
        ])

    def test_fractional_moves_accumulate(self):
        backend = Recording()
        for _ in range(3):
            backend.send({"action": "move", "dx": 0.5, "dy": -0.4})
        self.assertEqual(backend.events, [("move", 1, 0), ("move", 0, -1)])

    def test_scroll_matches_mac_direction(self):
        backend = Recording()
        backend.send({"action": "scroll", "dx": 2, "dy": 5})
        # Positive dy scrolls up; positive dx scrolls left, so "right" is negative.
        self.assertEqual(backend.events, [("wheel", 15, -6)])

    def test_drag_and_clicks(self):
        backend = Recording()
        backend.send({"action": "drag", "active": True})
        backend.send({"action": "drag", "active": True})
        backend.send({"action": "drag", "active": False})
        backend.send({"action": "click", "button": "left", "count": 2})
        self.assertEqual([event for event in backend.events if event[0] == "button"], [
            ("button", "left", True), ("button", "left", False),
            ("button", "left", True), ("button", "left", False), ("button", "left", True), ("button", "left", False),
        ])

    def test_no_input_without_permission(self):
        backend = Recording(ready=False)
        self.assertFalse(backend.send({"action": "move", "dx": 4, "dy": 4}))
        self.assertFalse(backend.send({"action": "check_perm"}))
        self.assertEqual(backend.events, [])

    def test_mac_only_shortcut_is_refused(self):
        bridge = LocalInput(Recording())
        with self.assertRaises(RequestError) as error:
            bridge.send({"action": "shortcut", "key_code": 1, "flags": 0})
        self.assertEqual(error.exception.status, 503)


class AvailableKeyTests(unittest.TestCase):
    def test_windows_reports_brightness_as_unavailable(self):
        windows = type("FakeWindows", (InputBackend,), {"supported_keys": WindowsInput.supported_keys})()
        self.assertEqual(windows.unavailable_keys(), {"bright_up", "bright_down"})
        self.assertEqual(LocalInput(windows).unavailable_keys, ["bright_down", "bright_up"])

    def test_linux_sends_every_key(self):
        self.assertEqual(Recording().unavailable_keys(), set())


class WindowsKeyTests(unittest.TestCase):
    def test_virtual_keys_cover_the_keymap(self):
        names = {name for modifiers, key in KEYMAP.values() for name in (*modifiers, key)}
        # Windows has no standard brightness key, so those two buttons do nothing.
        self.assertEqual(names - set(WindowsInput.VK), {"bright_up", "bright_down"})

    def test_rejected_windows_input_does_not_mark_drag_active(self):
        class RejectingWindows(WindowsInput):
            def __init__(self):
                InputBackend.__init__(self)

            def _mouse(self, *args):
                raise OSError("Windows refused the input.")

        backend = RejectingWindows()
        with self.assertRaises(RequestError):
            LocalInput(backend).send({"action": "drag", "active": True})
        self.assertFalse(backend.dragging)


class FakeUinput:
    def __init__(self):
        self.read_fd, self.write_fd = os.pipe()
        self.calls = []

    def ioctl(self, fd, request, argument=0):
        self.calls.append((request, argument))

    def events(self):
        os.close(self.write_fd)
        data = b""
        while chunk := os.read(self.read_fd, 4096):
            data += chunk
        os.close(self.read_fd)
        size = struct.calcsize("llHHi")
        return [struct.unpack("llHHi", data[i:i + size])[2:] for i in range(0, len(data), size)]


class LinuxTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeUinput()
        self.locked = False
        self.backend = LinuxInput(opener=lambda: os.dup(self.device.write_fd), ioctl=self.device.ioctl,
                                  locked=lambda: self.locked, typer=[])
        self.backend.ready()

    def events(self):
        self.backend.close(release=False)
        return [event for event in self.device.events() if event[0] != LinuxInput.EV_SYN]

    def test_creates_one_virtual_device(self):
        requests = [request for request, _ in self.device.calls]
        self.assertEqual(requests[-2:], [LinuxInput.UI_DEV_SETUP, LinuxInput.UI_DEV_CREATE])
        setup = self.device.calls[-2][1]
        self.assertEqual(len(setup), 92)  # sizeof(struct uinput_setup)
        self.assertTrue(setup[8:].startswith(b"Lazy Remote\0"))

    def test_unsupported_fallback_text_is_rejected_before_any_keys(self):
        with self.assertRaises(RequestError) as error:
            LocalInput(self.backend).send({"action": "type", "text": "aé"})
        self.assertEqual(error.exception.status, 422)
        self.assertEqual(self.events(), [])

    def test_no_input_while_the_session_is_locked(self):
        self.locked = True
        self.assertFalse(self.backend.send({"action": "key", "name": "space"}))
        self.locked = False
        self.assertTrue(self.backend.send({"action": "key", "name": "space"}))
        self.assertEqual(len(self.events()), 2)  # only the unlocked press: down and up

    def test_missing_device_means_no_permission(self):
        def refuse():
            raise PermissionError("denied")
        backend = LinuxInput(opener=refuse, ioctl=self.device.ioctl, locked=lambda: False, typer=[])
        self.assertFalse(backend.send({"action": "check_perm"}))

    def test_supported_fallback_text_uses_shift(self):
        self.backend.text("A!")
        key = LinuxInput.EV_KEY
        shift, a, one = LinuxInput.KEY["shift"], LinuxInput.KEY["a"], LinuxInput.KEY["1"]
        self.assertEqual(self.events(), [
            (key, shift, 1), (key, a, 1), (key, a, 0), (key, shift, 0),
            (key, shift, 1), (key, one, 1), (key, one, 0), (key, shift, 0),
        ])

    def test_typing_tool_reads_stdin_and_does_not_replay_after_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "typer.py"
            log = script.with_suffix(".log")
            script.write_text("import sys\n"
                              "with open(sys.argv[1], 'ab') as output: output.write(sys.stdin.buffer.read())\n"
                              "sys.exit(int(sys.argv[2]))\n", encoding="utf-8")
            self.backend.typer = [sys.executable, str(script), str(log), "0"]
            self.backend.text("héllo -n")
            self.assertEqual(log.read_text(encoding="utf-8"), "héllo -n")
            self.backend.typer = [sys.executable, str(script), str(log), "1"]
            with self.assertRaises(OSError):
                self.backend.text("a")
            self.assertEqual(log.read_text(encoding="utf-8"), "héllo -na")
            self.assertEqual(self.events(), [])

    def test_lock_destroys_device_and_clears_a_held_button(self):
        self.backend.send({"action": "drag", "active": True})
        self.locked = True
        self.assertFalse(self.backend.send({"action": "check_perm"}))
        self.assertIsNone(self.backend.fd)
        self.assertFalse(self.backend.dragging)
        self.assertEqual(self.device.calls[-1][0], LinuxInput.UI_DEV_DESTROY)
        self.assertEqual(self.events(), [(LinuxInput.EV_KEY, LinuxInput.BTN["left"], 1)])

    def test_wheel_sends_hi_res_and_whole_notches(self):
        self.backend.wheel(90, 0)
        self.backend.wheel(90, -30)
        rel = LinuxInput.EV_REL
        self.assertEqual(self.events(), [
            (rel, LinuxInput.REL_WHEEL_HI_RES, 90),
            (rel, LinuxInput.REL_WHEEL_HI_RES, 90), (rel, LinuxInput.REL_WHEEL, 1),
            (rel, LinuxInput.REL_HWHEEL_HI_RES, -30),
        ])


class LockCheckTests(unittest.TestCase):
    def check(self, answers, desktop="GNOME"):
        def run(command):
            stdout, code = answers[command[0]]
            return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}):
            return input_backends._LockCheck(run)()

    def test_requires_active_and_unlocked(self):
        for output, code in (("Active=yes\nLockedHint=yes", 0), ("Active=no\nLockedHint=no", 0),
                             ("Active=yes", 0), ("", 1)):
            with self.subTest(output=output, code=code):
                self.assertTrue(self.check({"loginctl": (output, code)}))
        self.assertFalse(self.check({"loginctl": ("Active=yes\nLockedHint=no", 0)}))

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX only")
    def test_unknown_screen_state_is_blocked(self):
        answers = {"loginctl": ("Active=yes\nLockedHint=no", 0), "pgrep": ("", 1), "gdbus": ("", 1)}
        self.assertTrue(self.check(answers, "sway"))
        self.assertFalse(self.check({**answers, "gdbus": ("(false,)", 0)}, "sway"))
        self.assertTrue(self.check({**answers, "pgrep": ("1234", 0)}, "sway"))


class CertificateTests(unittest.TestCase):
    @unittest.skipIf(shutil.which("openssl") is None, "openssl is not installed")
    def test_restricted_intermediate_and_public_phone_files(self):
        # Covers whichever backend this Python uses (cryptography or the openssl binary).
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tls_setup.make_identity(directory, ["10.1.2.3", "pc.local", "127.0.0.1"])
            root = subprocess.run(["openssl", "x509", "-noout", "-text", "-in", "issuer.pem"],
                                  cwd=directory, capture_output=True, text=True).stdout
            self.assertIn("Name Constraints: critical", root)
            self.assertIn("DNS:local", root)
            self.assertIn("IP:192.168.0.0/255.255.0.0", root)
            self.assertFalse((directory / "root.key").exists())
            self.assertEqual({p.suffix for p in (directory / "phone").iterdir()}, {".cer", ".mobileconfig"})
            verify = subprocess.run(
                ["openssl", "verify", "-CAfile", "root.pem", "-untrusted", "issuer.pem", "-purpose", "sslserver", "server.pem"],
                cwd=directory, capture_output=True, text=True,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertEqual((directory / "version").read_text(), str(tls_setup.IDENTITY_VERSION))

    @unittest.skipUnless(os.name == "nt", "Windows folder permissions")
    def test_windows_state_folder_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = tls_setup.private_state_dir(Path(temporary) / "state")
            listing = subprocess.run(["icacls", str(state)], capture_output=True, text=True).stdout
            self.assertIn(os.environ["USERNAME"], listing)
            for broad in ("BUILTIN\\Users", "Everyone", "Authenticated Users"):
                self.assertNotIn(broad, listing)

    def test_identities_without_constraints_are_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "tls"
            _, path, _, first = tls_setup.tls_identity("127.0.0.1", None, False, state)
            (path / "version").unlink()  # Looks like an identity made before name constraints.
            self.assertNotEqual(tls_setup.tls_identity("127.0.0.1", None, False, state)[3], first)

    @unittest.skipIf(tls_setup.x509 is None, "cryptography is not installed")
    @unittest.skipIf(shutil.which("openssl") is None, "openssl is not installed")
    def test_cryptography_identity_verifies_with_openssl(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tls_setup.make_identity(directory, ["192.168.1.2", "pc.local", "127.0.0.1"])
            result = subprocess.run(
                ["openssl", "verify", "-CAfile", "root.pem", "-untrusted", "issuer.pem", "-purpose", "sslserver", "server.pem"],
                cwd=directory, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            text = subprocess.run(["openssl", "x509", "-noout", "-text", "-in", "server.pem"],
                                  cwd=directory, capture_output=True, text=True).stdout
            self.assertIn("IP Address:192.168.1.2", text)
            self.assertIn("DNS:pc.local", text)
            root = subprocess.run(["openssl", "x509", "-noout", "-text", "-in", "issuer.pem"],
                                  cwd=directory, capture_output=True, text=True).stdout
            self.assertIn("DNS:local", root)
            expected = subprocess.run(
                ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", "root.pem"],
                cwd=directory, capture_output=True, text=True,
            ).stdout.strip().split("=", 1)[1]
            self.assertEqual(tls_setup.fingerprint(directory / "root.pem"), expected)
            self.assertFalse(tls_setup.certificate_expired(directory / "server.pem"))


class FactoryTests(unittest.TestCase):
    def test_launcher_runs_the_server_script_from_source(self):
        import desktop_launcher
        command = desktop_launcher.server_command()
        self.assertEqual(Path(command[-2]).name, "server.py")
        self.assertEqual(command[-1], "--launcher")

    def test_linux_setup_rule_uses_seat_access(self):
        self.assertIn('TAG+="uaccess"', input_backends.LINUX_SETUP)
        self.assertNotIn("chmod", input_backends.LINUX_SETUP)


if __name__ == "__main__":
    unittest.main()
