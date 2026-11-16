"""Run the shared Lazy Remote server inside the Android TV app."""

import json
from pathlib import Path
import threading

from auth import RequestError
from platform_support import private_ipv4
from server import KEYS, Remote, RemoteServer
from tls_setup import make_identity, tls_identity

TV_KEYS = frozenset(("up", "down", "left", "right", "enter", "tv_home", "tv_back",
                     "vol_up", "vol_down", "vol_mute", "media_play"))
PORT = 8765


class TvInput:
    def __init__(self, platform):
        self.platform = platform

    @property
    def unavailable_keys(self):
        return sorted((KEYS | TV_KEYS) - set(self.platform.availableKeys()))

    def send(self, command):
        if command["action"] in ("check_perm", "request_perm"):
            return self.platform.ready()
        if not self.platform.press(command["name"]):
            raise RequestError(422, "That button is unavailable right now on this TV.")
        return True


class TvRemote(Remote):
    @staticmethod
    def accepts_command(command):
        name = command.get("name")
        return (command.keys() == {"action", "name"} and command["action"] == "key"
                and isinstance(name, str) and name in TV_KEYS)

    def status(self, *args):
        status, payload = super().status(*args)
        return status, {**payload, "host": "tv"}


class TvHost:
    def __init__(self, platform):
        self.platform = platform
        self.remote = None
        self.stop = threading.Event()

    def notify(self, event, **data):
        self.platform.event(json.dumps({"kind": event, **data}))

    def run(self, address, directory, port=PORT):
        worker = None
        try:
            address = private_ipv4(address)
            if address == "127.0.0.1" and port != 0:
                raise ValueError("Connect the TV and phone to the same private Wi-Fi or Ethernet network.")
            if self.stop.is_set():
                return
            state = Path(directory) / "lazy-remote"
            with RemoteServer((address, port)) as server:
                context, identity, names, fingerprint = tls_identity(
                    address, None, False, state, build_identity=self.make_identity,
                    is_expired=lambda path, within_days: self.platform.certificateExpires(str(path), within_days),
                )
                if self.stop.is_set():
                    return
                server.configure_tls(context, names)
                remote = self.remote = server.remote = TvRemote(
                    TvInput(self.platform), state / "browser.json", on_change=self.notify,
                )
                remote.new_code(publish=False)
                self.notify("ready", url=f"https://{address}:{server.server_port}/", fingerprint=fingerprint,
                            certificates=str(identity / "phone"), **remote.snapshot())
                worker = threading.Thread(target=remote.maintain, args=(self.stop,), daemon=True)
                worker.start()
                server.timeout = 0.5
                while not self.stop.is_set():
                    server.handle_request()
        except Exception as error:
            self.notify("notice", message=str(error))
        finally:
            if self.remote:
                with self.remote.lock:
                    self.remote.revoke()
            self.stop.set()
            if worker:
                worker.join()
            self.remote = None
            self.notify("stopped")

    def make_identity(self, directory, names, existing=None):
        make_identity(
            directory, names, existing,
            build_issuer=lambda path: self.platform.makeIssuer(str(path)),
            build_leaf=lambda path, hosts: self.platform.makeLeaf(str(path), hosts[0]),
        )

    def close(self):
        self.stop.set()
        remote = self.remote
        if remote:
            with remote.lock:
                remote.pairing.pending = None
                remote.pairing.clear_codes()
                remote.revoke()

    def local(self, action, match=""):
        remote = self.remote
        if not remote or self.stop.is_set():
            return
        try:
            if action in ("approve", "deny"):
                remote.decide(match, action == "approve")
            elif action in ("lock", "forget"):
                remote.new_code(forget=action == "forget")
        except RequestError as error:
            self.notify("notice", message=str(error))
