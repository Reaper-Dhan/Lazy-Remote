import datetime
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid

from platform_support import IS_WINDOWS, default_state_dir, lock_file

try:  # Bundled with the Windows and Linux apps; the Mac uses its built-in OpenSSL instead.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError:
    x509 = None


STATE_DIR = default_state_dir()
OPENSSL = shutil.which("openssl") or "/usr/bin/openssl"
LIFETIME_DAYS = 365
# Version 2 added name constraints; older identities are replaced once.
IDENTITY_VERSION = 2
# The root may only vouch for ".local" names, so it can never impersonate a real
# website. IP addresses are left unconstrained: several TLS libraries (LibreSSL
# among them) reject any certificate whose root limits IP ranges. The rule is
# non-critical so a phone that does not understand it still connects.
PERMITTED_DOMAIN = "local"


def _openssl_constraints():
    return f"nameConstraints=permitted;DNS:{PERMITTED_DOMAIN}"


def openssl(*args, cwd, data=None):
    result = subprocess.run(
        [OPENSSL, *args], cwd=cwd, input=data, capture_output=True, timeout=20
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "OpenSSL failed.")
    return result.stdout


def _openssl_identity(directory, names):
    root_key = openssl("genrsa", "2048", cwd=directory)
    openssl(
        "req", "-new", "-x509", "-sha256", "-days", str(LIFETIME_DAYS), "-key", "/dev/stdin",
        "-subj", "/CN=Lazy Remote Local Root", "-out", "root.pem",
        "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash",
        "-addext", _openssl_constraints(),
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
        "x509", "-req", "-sha256", "-days", str(LIFETIME_DAYS), "-in", "server.csr",
        "-CA", "root.pem", "-CAkey", "/dev/stdin", "-CAcreateserial",
        "-extfile", "server.ext", "-out", "server.pem", cwd=directory, data=root_key,
    )
    del root_key
    for name in ("server.csr", "server.ext", "root.srl"):
        (directory / name).unlink(missing_ok=True)


def _cryptography_identity(directory, names):
    import ipaddress

    now = datetime.datetime.now(datetime.timezone.utc)
    until = now + datetime.timedelta(days=LIFETIME_DAYS)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lazy Remote Local Root")])
    root_ski = x509.SubjectKeyIdentifier.from_public_key(root_key.public_key())
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name).issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(until)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(root_ski, critical=False)
    )
    root = root.add_extension(x509.NameConstraints(
        permitted_subtrees=[x509.DNSName(PERMITTED_DOMAIN)], excluded_subtrees=None,
    ), critical=False)
    root = root.sign(root_key, hashes.SHA256())
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alternatives = [
        x509.DNSName(name) if name.endswith(".local") else x509.IPAddress(ipaddress.ip_address(name))
        for name in names
    ]
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lazy Remote")]))
        .issuer_name(root_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(until)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(root_ski), critical=False)
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    # Only the root certificate is kept; its signing key is discarded here.
    del root_key
    (directory / "root.pem").write_bytes(root.public_bytes(serialization.Encoding.PEM))
    (directory / "server.pem").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key = server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    with os.fdopen(os.open(directory / "server.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(key)


def make_identity(directory, names):
    if x509 is not None:
        _cryptography_identity(directory, names)
    else:
        _openssl_identity(directory, names)
    (directory / "server.key").chmod(0o600)
    (directory / "names").write_text("\n".join(names), encoding="ascii")
    (directory / "version").write_text(str(IDENTITY_VERSION), encoding="ascii")
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


def certificate_expired(path):
    if x509 is not None:
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
        expiry = getattr(certificate, "not_valid_after_utc", None)
        if expiry is None:  # cryptography before 42
            expiry = certificate.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        return expiry <= datetime.datetime.now(datetime.timezone.utc)
    check = subprocess.run([OPENSSL, "x509", "-checkend", "0", "-noout", "-in", str(path)], capture_output=True)
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
        return
    domain = os.environ.get("USERDOMAIN")
    account = f"{domain}\\{user}" if domain else user
    subprocess.run(
        ["icacls", str(path), "/inheritance:r",
         "/grant:r", f"{account}:(OI)(CI)F", "/grant:r", "*S-1-5-18:(OI)(CI)F"],
        capture_output=True, timeout=10,
    )


def private_state_dir(state_dir):
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if state_dir.is_symlink():
        raise RuntimeError(f"Lazy Remote storage must be a private directory owned by you: {state_dir}")
    if IS_WINDOWS:
        try:
            _restrict_windows_folder(state_dir)
        except (OSError, subprocess.SubprocessError):
            pass  # The folder still has the user profile's private permissions.
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
        existing_names = (current / "names").read_text(encoding="ascii").splitlines() if current.exists() else []
        names = [address]
        if hostname and address != "127.0.0.1":
            names.append(hostname)
        if not renew and existing_names:
            renew = (certificate_expired(current / "server.pem")
                     or not set(names).intersection(existing_names)
                     or _identity_version(current) < IDENTITY_VERSION)
        if renew or not existing_names:
            directory = Path(tempfile.mkdtemp(prefix="tls-", dir=state_dir))
            try:
                cert_names = list(dict.fromkeys(names + ["127.0.0.1"] + ([hostname] if hostname else [])))
                make_identity(directory, cert_names)
                previous = current.resolve() if current.exists() else None
                _point_current_at(state_dir, state_dir / "certificates", directory)
            except BaseException:
                shutil.rmtree(directory)
                raise
            if previous and previous.parent == state_dir and previous.name.startswith("tls-"):
                _remove_tree(previous)
            current = _current_dir(state_dir)
            existing_names = names

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(current / "server.pem", current / "server.key")
        root_fingerprint = fingerprint(current / "root.pem")
    return context, current, [name for name in names if name in existing_names], root_fingerprint
