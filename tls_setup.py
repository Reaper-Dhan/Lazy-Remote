import datetime
import hashlib
import ipaddress
import os
from pathlib import Path
import plistlib
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid

from platform_support import IS_MAC, IS_WINDOWS, LAN_NETWORKS, default_state_dir, lock_file

try:  # Bundled with the Windows and Linux apps; the Mac uses its built-in OpenSSL instead.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError:
    x509 = None


STATE_DIR = default_state_dir()
OPENSSL = "/usr/bin/openssl" if IS_MAC else shutil.which("openssl") or "/usr/bin/openssl"
LEAF_DAYS = 90
CA_DAYS = 5 * 365
IDENTITY_VERSION = 3
PERMITTED_DOMAIN = "local"
PERMITTED_IPS = (*LAN_NETWORKS, ipaddress.ip_network("127.0.0.1/32"))
CA_USAGE = "keyUsage=critical,keyCertSign,cRLSign"
ISSUER_FILES = ("root.pem", "issuer.pem", "issuer.key")


def openssl(*args, cwd, data=None):
    result = subprocess.run(
        [OPENSSL, *args], cwd=cwd, input=data, capture_output=True, timeout=20
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "OpenSSL failed.")
    return result.stdout


def _openssl_sign(directory, name, subject, ca, key, days, extensions, data=None):
    openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", subject,
            "-keyout", f"{name}.key", "-out", f"{name}.csr", cwd=directory)
    (directory / f"{name}.key").chmod(0o600)
    (directory / f"{name}.ext").write_text("\n".join(extensions) + "\n", encoding="ascii")
    openssl("x509", "-req", "-sha256", "-days", str(days), "-in", f"{name}.csr",
            "-CA", ca, "-CAkey", key, "-set_serial", str(uuid.uuid4().int),
            "-extfile", f"{name}.ext", "-out", f"{name}.pem", cwd=directory, data=data)
    for extension in ("csr", "ext"):
        (directory / f"{name}.{extension}").unlink()


def _openssl_issuer(directory):
    root_key = openssl("genrsa", "2048", cwd=directory)
    openssl(
        "req", "-new", "-x509", "-sha256", "-days", str(CA_DAYS), "-key", "/dev/stdin",
        "-subj", "/CN=Lazy Remote Local Root", "-out", "root.pem",
        "-addext", "basicConstraints=critical,CA:TRUE,pathlen:1",
        "-addext", CA_USAGE, "-addext", "subjectKeyIdentifier=hash",
        cwd=directory, data=root_key,
    )
    constraints = [f"permitted;DNS:{PERMITTED_DOMAIN}"]
    constraints.extend(f"permitted;IP:{network.network_address}/{network.netmask}" for network in PERMITTED_IPS)
    _openssl_sign(directory, "issuer", "/CN=Lazy Remote LAN Issuer", "root.pem", "/dev/stdin", CA_DAYS, [
        "basicConstraints=critical,CA:TRUE,pathlen:0", CA_USAGE, "subjectKeyIdentifier=hash",
        "authorityKeyIdentifier=keyid,issuer", "extendedKeyUsage=serverAuth",
        "nameConstraints=critical," + ",".join(constraints),
    ], data=root_key)


def _openssl_leaf(directory, names):
    alternatives = ",".join(f"{'DNS' if name.endswith('.local') else 'IP'}:{name}" for name in names)
    _openssl_sign(directory, "server", "/CN=Lazy Remote", "issuer.pem", "issuer.key", LEAF_DAYS, [
        "basicConstraints=critical,CA:FALSE", "keyUsage=critical,digitalSignature,keyEncipherment",
        "extendedKeyUsage=serverAuth", "subjectKeyIdentifier=hash", "authorityKeyIdentifier=keyid,issuer",
        f"subjectAltName={alternatives}",
    ])


def _key_usage(is_ca):
    return x509.KeyUsage(
        digital_signature=not is_ca, content_commitment=False, key_encipherment=not is_ca,
        data_encipherment=False, key_agreement=False, key_cert_sign=is_ca, crl_sign=is_ca,
        encipher_only=False, decipher_only=False,
    )


def _certificate(name, key, days, path_length=None, issuer=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    is_ca = path_length is not None
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer.subject if issuer else subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=path_length), critical=True)
        .add_extension(_key_usage(is_ca), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if issuer:
        return certificate.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.public_key()), critical=False
        ).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return certificate


def _write_certificate(directory, name, certificate, key=None):
    (directory / f"{name}.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    if key is not None:
        encoded = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
        with os.fdopen(os.open(directory / f"{name}.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            file.write(encoded)


def _cryptography_issuer(directory):
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root = _certificate("Lazy Remote Local Root", root_key, CA_DAYS, path_length=1).sign(root_key, hashes.SHA256())
    issuer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    permitted = [x509.DNSName(PERMITTED_DOMAIN), *(x509.IPAddress(network) for network in PERMITTED_IPS)]
    issuer = _certificate("Lazy Remote LAN Issuer", issuer_key, CA_DAYS, path_length=0, issuer=root)
    issuer = issuer.add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None),
                                  critical=True).sign(root_key, hashes.SHA256())
    _write_certificate(directory, "root", root)
    _write_certificate(directory, "issuer", issuer, issuer_key)


def _cryptography_leaf(directory, names):
    issuer = x509.load_pem_x509_certificate((directory / "issuer.pem").read_bytes())
    issuer_key = serialization.load_pem_private_key((directory / "issuer.key").read_bytes(), password=None)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alternatives = [
        x509.DNSName(name) if name.endswith(".local") else x509.IPAddress(ipaddress.ip_address(name))
        for name in names
    ]
    server = _certificate("Lazy Remote", server_key, LEAF_DAYS, issuer=issuer)
    server = server.add_extension(x509.SubjectAlternativeName(alternatives), critical=False).sign(issuer_key, hashes.SHA256())
    _write_certificate(directory, "server", server, server_key)


def make_identity(directory, names, existing=None):
    if existing is None:
        (_cryptography_issuer if x509 is not None else _openssl_issuer)(directory)
        write_phone_files(directory)
    else:
        for name in ISSUER_FILES:
            shutil.copy2(existing / name, directory / name)
        shutil.copytree(existing / "phone", directory / "phone")
    (_cryptography_leaf if x509 is not None else _openssl_leaf)(directory, names)
    with (directory / "server.pem").open("ab") as chain:
        chain.write((directory / "issuer.pem").read_bytes())
    (directory / "names").write_text("\n".join(names), encoding="ascii")
    (directory / "version").write_text(str(IDENTITY_VERSION), encoding="ascii")


def write_phone_files(directory):
    phone = directory / "phone"
    phone.mkdir()
    root_der = ssl.PEM_cert_to_DER_cert((directory / "root.pem").read_text(encoding="ascii"))
    (phone / "Lazy Remote Root.cer").write_bytes(root_der)
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
    (phone / "Lazy Remote.mobileconfig").write_bytes(plistlib.dumps(profile))


def certificate_expired(path, within_days=0):
    if x509 is not None:
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
        expiry = getattr(certificate, "not_valid_after_utc", None)
        if expiry is None:  # cryptography before 42
            expiry = certificate.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        return expiry <= datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=within_days)
    check = subprocess.run([OPENSSL, "x509", "-checkend", str(within_days * 86400), "-noout", "-in", str(path)], capture_output=True)
    return check.returncode != 0


def fingerprint(path):
    der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))


def _identity_version(directory):
    try:
        return int((directory / "version").read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        return 1


def _remove_tree(path):
    """Delete an old certificate folder. Windows antivirus can hold new files open briefly."""
    for attempt in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.1 * (attempt + 1))


def _restrict_windows_folder(path):
    """Give only this user (and SYSTEM) access, like chmod 700 on macOS and Linux."""
    user = os.environ.get("USERNAME")
    if not user:
        raise RuntimeError("Could not identify the Windows user for private storage.")
    domain = os.environ.get("USERDOMAIN")
    account = f"{domain}\\{user}" if domain else user
    subprocess.run(
        ["icacls", str(path), "/inheritance:r",
         "/grant:r", f"{account}:(OI)(CI)F", "/grant:r", "*S-1-5-18:(OI)(CI)F"],
        capture_output=True, timeout=10, check=True,
    )


def private_state_dir(state_dir):
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if state_dir.is_symlink():
        raise RuntimeError(f"Lazy Remote storage must be a private directory owned by you: {state_dir}")
    if IS_WINDOWS:
        try:
            _restrict_windows_folder(state_dir)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Could not restrict access to Lazy Remote storage.") from error
    else:
        info = state_dir.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError(f"Lazy Remote storage must be a private directory owned by you: {state_dir}")
    return state_dir.resolve()


def _point_current_at(state_dir, current, directory):
    """Switch the active certificate folder in one step."""
    if IS_WINDOWS:
        # Symbolic links need extra rights on Windows, so a pointer file names the folder.
        pending = state_dir / ".current.tmp"
        pending.write_text(directory.name, encoding="ascii")
        pending.replace(state_dir / "current")
        return
    link = state_dir / f".{directory.name}"
    try:
        link.symlink_to(directory.name, target_is_directory=True)
        link.replace(current)
    except BaseException:
        link.unlink(missing_ok=True)
        raise


def _current_dir(state_dir):
    if IS_WINDOWS:
        try:
            name = (state_dir / "current").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return state_dir / "certificates"
        return state_dir / name
    return state_dir / "certificates"


def tls_identity(address, hostname, renew, state_dir=STATE_DIR):
    state_dir = private_state_dir(state_dir)
    with (state_dir / "lock").open("a") as lock:
        lock_file(lock)
        current = _current_dir(state_dir)
        names = [address]
        if hostname and address != "127.0.0.1":
            names.append(hostname)
        existing_names = (current / "names").read_text(encoding="ascii").splitlines() if current.exists() else []
        replace_root = (renew or not existing_names or _identity_version(current) != IDENTITY_VERSION
                        or any(not (current / name).is_file() for name in ISSUER_FILES)
                        or certificate_expired(current / "root.pem", within_days=7)
                        or certificate_expired(current / "issuer.pem", within_days=7))
        if (replace_root or not set(names).issubset(existing_names)
                or certificate_expired(current / "server.pem", within_days=7)):
            directory = Path(tempfile.mkdtemp(prefix="tls-", dir=state_dir))
            try:
                make_identity(directory, names, existing=None if replace_root else current)
                context = _server_context(directory)
                previous = current.resolve() if current.exists() else None
                _point_current_at(state_dir, state_dir / "certificates", directory)
            except BaseException:
                _remove_tree(directory)
                raise
            if previous and previous.parent == state_dir and previous.name.startswith("tls-"):
                _remove_tree(previous)
            current = _current_dir(state_dir)
        else:
            context = _server_context(current)
        root_fingerprint = fingerprint(current / "root.pem")
    return context, current, names, root_fingerprint


def _server_context(directory):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(directory / "server.pem", directory / "server.key")
    return context
