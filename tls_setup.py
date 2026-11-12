import fcntl
import ipaddress
import os
from pathlib import Path
import plistlib
import re
import shutil
import ssl
import subprocess
import tempfile
import uuid


APP_SUPPORT = Path.home() / "Library" / "Application Support"
STATE_DIR = APP_SUPPORT / "Lazy Remote"
LEGACY_STATE_DIR = APP_SUPPORT / "Couch Remote"
# Preserve the existing HTTPS identity and browser trust when upgrading.
if not STATE_DIR.exists() and LEGACY_STATE_DIR.exists():
    STATE_DIR = LEGACY_STATE_DIR
OPENSSL = "/usr/bin/openssl"
LAN_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def private_ipv4(value):
    address = ipaddress.IPv4Address(value)
    if address == ipaddress.IPv4Address("127.0.0.1") or any(address in network for network in LAN_NETWORKS):
        return str(address)
    raise ValueError("REMOTE_BIND must be a private IPv4 address or 127.0.0.1.")


def local_endpoints():
    bind = os.environ.get("REMOTE_BIND")
    if bind:
        address = private_ipv4(bind)
    else:
        interfaces = subprocess.check_output(["/sbin/ifconfig", "-l"], text=True).split()
        route = subprocess.run(
            ["/sbin/route", "-n", "get", "default"], capture_output=True, text=True
        )
        default = re.search(r"interface:\s*(en\d+)\b", route.stdout)
        preferred = [default[1]] if default else []
        address = "127.0.0.1"
        for interface in dict.fromkeys(preferred + ["en0"] + interfaces):
            if not re.fullmatch(r"en\d+", interface):
                continue
            result = subprocess.run(
                ["/usr/sbin/ipconfig", "getifaddr", interface], capture_output=True, text=True
            )
            try:
                address = private_ipv4(result.stdout.strip())
            except ValueError:
                continue
            break

    result = subprocess.run(
        ["/usr/sbin/scutil", "--get", "LocalHostName"], capture_output=True, text=True
    )
    name = result.stdout.strip().lower()
    hostname = f"{name}.local" if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name) else None
    return address, hostname


def openssl(*args, cwd, data=None):
    result = subprocess.run(
        [OPENSSL, *args], cwd=cwd, input=data, capture_output=True, timeout=20
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "OpenSSL failed.")
    return result.stdout


def make_identity(directory, names):
    root_key = openssl("genrsa", "2048", cwd=directory)
    openssl(
        "req", "-new", "-x509", "-sha256", "-days", "365", "-key", "/dev/stdin",
        "-subj", "/CN=Lazy Remote Local Root", "-out", "root.pem",
        "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash",
        cwd=directory, data=root_key,
    )
    openssl(
        "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
        "-subj", "/CN=Lazy Remote", "-keyout", "server.key", "-out", "server.csr",
        cwd=directory,
    )
    alternatives = ",".join(f"{'DNS' if name.endswith('.local') else 'IP'}:{name}" for name in names)
    extensions = directory / "server.ext"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        f"subjectAltName={alternatives}\n",
        encoding="ascii",
    )
    openssl(
        "x509", "-req", "-sha256", "-days", "365", "-in", "server.csr",
        "-CA", "root.pem", "-CAkey", "/dev/stdin", "-CAcreateserial",
        "-extfile", "server.ext", "-out", "server.pem", cwd=directory, data=root_key,
    )
    del root_key
    for name in ("server.csr", "server.ext", "root.srl"):
        (directory / name).unlink(missing_ok=True)
    (directory / "server.key").chmod(0o600)
    (directory / "names").write_text("\n".join(names), encoding="ascii")
    root_der = ssl.PEM_cert_to_DER_cert((directory / "root.pem").read_text(encoding="ascii"))
    (directory / "Lazy Remote Root.cer").write_bytes(root_der)
    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": "local.couch-remote",
        "PayloadUUID": str(uuid.uuid4()),
        "PayloadDisplayName": "Lazy Remote",
        "PayloadContent": [{
            "PayloadType": "com.apple.security.root",
            "PayloadVersion": 1,
            "PayloadIdentifier": "local.couch-remote.root",
            "PayloadUUID": str(uuid.uuid4()),
            "PayloadDisplayName": "Lazy Remote Local Root",
            "PayloadCertificateFileName": "Lazy Remote Root.cer",
            "PayloadContent": root_der,
        }],
    }
    (directory / "Lazy Remote.mobileconfig").write_bytes(plistlib.dumps(profile))


def private_state_dir(state_dir):
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = state_dir.lstat()
    if state_dir.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"Lazy Remote storage must be a private directory owned by you: {state_dir}")
    return state_dir.resolve()


def tls_identity(address, hostname, renew, state_dir=STATE_DIR):
    state_dir = private_state_dir(state_dir)
    current = state_dir / "certificates"
    with (state_dir / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing_names = (current / "names").read_text(encoding="ascii").splitlines() if current.exists() else []
        names = [address]
        if hostname and address != "127.0.0.1":
            names.append(hostname)
        if not renew and existing_names:
            check = subprocess.run(
                [OPENSSL, "x509", "-checkend", "0", "-noout", "-in", str(current / "server.pem")],
                capture_output=True,
            )
            renew = check.returncode != 0 or not set(names).intersection(existing_names)
        if renew or not existing_names:
            directory = Path(tempfile.mkdtemp(prefix="tls-", dir=state_dir))
            link = state_dir / f".{directory.name}"
            try:
                cert_names = list(dict.fromkeys(names + ["127.0.0.1"] + ([hostname] if hostname else [])))
                make_identity(directory, cert_names)
                previous = current.resolve() if current.is_symlink() else None
                link.symlink_to(directory.name, target_is_directory=True)
                link.replace(current)
            except BaseException:
                link.unlink(missing_ok=True)
                shutil.rmtree(directory)
                raise
            if previous and previous.parent == state_dir and previous.name.startswith("tls-"):
                shutil.rmtree(previous, ignore_errors=True)
            existing_names = names

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(current / "server.pem", current / "server.key")
        fingerprint = openssl(
            "x509", "-noout", "-fingerprint", "-sha256", "-in", "root.pem", cwd=current
        ).decode().strip().split("=", 1)[1]
    return context, current, [name for name in names if name in existing_names], fingerprint
