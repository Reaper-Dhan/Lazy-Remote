"""Exercise the packaged server and the launcher protocol over loopback HTTPS."""

import http.client
import base64
import json
import os
from pathlib import Path
import plistlib
from queue import Queue
import secrets
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit


def next_event(events, kind):
    while True:
        event = events.get(timeout=30)
        if event is None:
            raise RuntimeError(f"Packaged server exited while waiting for {kind}.")
        if event["event"] == "notice":
            raise RuntimeError(event["message"])
        if event["event"] == kind:
            return event


def read_events(stream, events):
    try:
        for line in stream:
            events.put(json.loads(line))
    finally:
        events.put(None)


def smoke(process):
    events = Queue()
    threading.Thread(target=read_events, args=(process.stdout, events), daemon=True).start()
    ready = next_event(events, "ready")
    url = urlsplit(ready["urls"][0])
    assert url.scheme == "https" and url.hostname == "127.0.0.1"
    directory = Path(ready["certificates"])
    assert {file.suffix for file in directory.iterdir()} == {".cer", ".mobileconfig"}
    root = (directory / "Lazy Remote Root.cer").read_bytes()
    profile = plistlib.loads((directory / "Lazy Remote.mobileconfig").read_bytes())
    assert profile["PayloadContent"][0]["PayloadContent"] == root
    trust = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(root))

    def request(path, body=None, *, cookie=None, tab=None, page=None, expected=200, method=None):
        connection = http.client.HTTPSConnection(url.hostname, url.port, timeout=10, context=trust)
        headers = {"Origin": f"https://{url.netloc}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if tab:
            headers["X-Lazy-Remote-Tab"] = tab
        if page:
            headers["X-Lazy-Remote-Request"] = page
        try:
            connection.request(method or ("GET" if body is None else "POST"), path,
                               None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            payload = response.read()
            assert response.status == expected, (path, response.status, payload)
            return response.headers, payload
        finally:
            connection.close()

    assert b"Pair this browser" in request("/")[1]
    manifest = json.loads(request("/manifest.webmanifest")[1])
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    icons = tuple(icon["src"] for icon in manifest["icons"])
    for path in ("/remote.js", "/remote.css", "/pwa.js", "/qr-scan.js", "/vendor/jsQR.js", "/service-worker.js", "/offline.html",
                 "/assets/logo.svg", "/assets/touch-icon.png", *icons):
        assert request(path)[1]
    headers, icon = request("/apple-touch-icon.png")
    assert headers["Content-Type"] == "image/png"
    assert icon.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", icon[16:24]) == (180, 180) and icon[25] == 2
    metadata, head_body = request("/apple-touch-icon.png", method="HEAD")
    assert not head_body and int(metadata["Content-Length"]) == len(icon)
    status = json.loads(request("/api/status", expected=401)[1])
    assert f"https://{url.netloc}" in status["pairing_origins"]
    page = secrets.token_hex(32)
    headers, _ = request("/api/pair", {"qr": ready["qr"]}, page=page, expected=202)
    pending = headers["Set-Cookie"].split(";", 1)[0]
    match = next_event(events, "pairing")["match"]
    assert json.loads(request("/api/status", page=page, expected=401)[1])["match"] == match
    request("/api/claim", {}, cookie=pending, expected=401)
    request("/api/claim", {}, page=page, expected=409)
    process.stdin.write(f"approve {match}\n")
    process.stdin.flush()
    assert next_event(events, "pairing")["approved"]
    _, first_claim = request("/api/claim", {}, page=page)
    assert json.loads(request("/api/status", page=page, expected=401)[1])["approved"]
    headers, body = request("/api/claim", {}, page=page)
    assert json.loads(body)["tab"] == json.loads(first_claim)["tab"]
    session = headers["Set-Cookie"].split(";", 1)[0]
    tab = json.loads(body)["tab"]
    assert "ready" in json.loads(request("/api/status", cookie=session, tab=tab, page=page)[1])
    request("/api/claim", {}, page=page, expected=401)
    request("/api/status", cookie=session, expected=401)
    request("/api/disconnect", {}, cookie=session, tab=tab)
    request("/api/status", cookie=session, tab=tab, expected=401)

    # Exercise the exact local pipe used by the native Bluetooth host too.
    page = secrets.token_hex(32)

    def bluetooth(operation, **fields):
        identifier = secrets.token_hex(16)
        message = {"id": identifier, "op": operation, "request_key": page, **fields}
        encoded = base64.b64encode(json.dumps(message).encode()).decode()
        process.stdin.write(f"bluetooth {encoded}\n")
        process.stdin.flush()
        result = json.loads(base64.b64decode(next_event(events, "bluetooth")["response"]))
        assert result["id"] == identifier
        return result

    assert bluetooth("cmd", body={"action": "key", "name": "space"})["status"] == 401
    # The preceding response is a pipe barrier: earlier approval events have
    # been consumed, so the next pairing event belongs to this new code.
    process.stdin.write("new_code\n")
    process.stdin.flush()
    fresh = next_event(events, "pairing")
    assert bluetooth("pair", body={"qr": fresh["qr"]})["status"] == 202
    pending = bluetooth("status")
    assert pending["match"] and not pending["approved"]
    assert bluetooth("claim")["status"] == 409
    process.stdin.write(f"approve {pending['match']}\n")
    process.stdin.flush()
    session = bluetooth("claim")
    assert session["status"] == 200
    credentials = {field: session[field] for field in ("token", "tab")}
    assert bluetooth("status", **credentials)["status"] == 200
    assert bluetooth("cancel", **credentials)["status"] == 200
    assert bluetooth("status", **credentials)["status"] == 401


def main(command):
    with tempfile.TemporaryDirectory() as directory:
        environment = {**os.environ, "REMOTE_BIND": "0.0.0.0", "REMOTE_PORT": "0"}
        with subprocess.Popen([*command, "--launcher", "--bind", "127.0.0.1", "--state-dir", str(Path(directory) / "state")],
                              env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, encoding="utf-8") as process:
            try:
                smoke(process)
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    raise
            assert process.returncode == 0, process.returncode
    print("Packaged HTTPS, phone profile, native Bluetooth dispatch, approval, and revocation passed.")


if __name__ == "__main__":
    main(sys.argv[1:])
