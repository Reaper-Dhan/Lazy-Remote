"""Native Bluetooth requests use the same approved Remote session as HTTPS.

The Mac launcher carries these messages over encrypted Core Bluetooth
characteristics. The local launcher pipe is the only entry point here.
"""

import json
import re

from auth import RequestError


def dispatch(remote, message):
    identifier = message.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", identifier):
        raise ValueError("Invalid Bluetooth request identifier.")
    try:
        key = message.get("request_key")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise RequestError(400, "Invalid pairing request.")
        values = {}
        for field in ("token", "tab", "device", "pending"):
            value = message.get(field)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value)):
                raise RequestError(400, "Invalid Bluetooth session.")
            values[field] = value
        operation = message.get("op")
        if operation == "status":
            status, result = remote.status(values["token"], values["tab"], values["device"], values["pending"], key)
        elif operation == "pair":
            body = message.get("body")
            if not isinstance(body, dict):
                raise RequestError(400, "Enter the pairing code or scan the host QR.")
            pending = remote.request_access(body=body, previous_session=values["token"],
                                            previous_tab=values["tab"], request_key=key)
            status, result = 202, {"pending": pending}
        elif operation == "claim":
            token, tab, _ = remote.claim(values["pending"], key)
            status, result = 200, {"token": token, "tab": tab}
        elif operation == "cmd":
            body = message.get("body")
            if not isinstance(body, dict):
                raise RequestError(400, "Invalid control command.")
            status, result = 200, remote.command(values["token"], values["tab"], body)
        elif operation == "disconnect":
            remote.disconnect(values["token"], values["tab"])
            status, result = 200, {"ok": True}
        elif operation == "cancel":
            if values["token"] and values["tab"]:
                try:
                    remote.disconnect(values["token"], values["tab"])
                except RequestError as error:
                    if error.status != 401:
                        raise
            remote.cancel_request(key)
            status, result = 200, {"ok": True}
        else:
            raise RequestError(404, "Unknown Bluetooth operation.")
    except RequestError as error:
        status, result = error.status, {"error": str(error)}
    return {"id": identifier, "status": status, **result}


def local_request(remote, encoded):
    import base64
    if len(encoded) > 12000:
        raise ValueError("Bluetooth request is too large.")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 8192:
        raise ValueError("Bluetooth request is too large.")
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError("Invalid Bluetooth request.")
    return base64.b64encode(json.dumps(dispatch(remote, message)).encode("utf-8")).decode("ascii")


def start_requests(remote, stop, emit):
    """Keep slow native input off the host approval/Lock controls thread."""
    import queue
    import threading
    requests = queue.Queue(maxsize=1)

    def run():
        while not stop.is_set():
            try:
                encoded = requests.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                emit("bluetooth", response=local_request(remote, encoded))
            except (ValueError, RecursionError):
                emit("notice", message="Invalid Bluetooth request.")

    threading.Thread(target=run, name="bluetooth-requests", daemon=True).start()
    return requests
