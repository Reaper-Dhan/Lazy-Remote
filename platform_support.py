"""Operating-system differences for the Lazy Remote host: storage, locks, and the local address."""
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import sys


IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
HOST_NAME = "Mac" if IS_MAC else "PC"

LAN_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def default_state_dir():
    if IS_MAC:
        support = Path.home() / "Library" / "Application Support"
        current, legacy = support / "Lazy Remote", support / "Couch Remote"
        # Preserve the existing HTTPS identity and browser trust when upgrading.
        return legacy if not current.exists() and legacy.exists() else current
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Lazy Remote"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "lazy-remote"


if IS_WINDOWS:
    import msvcrt

    def lock_file(handle, blocking=True):
        """Lock the first byte of an open file. Raises BlockingIOError when busy."""
        handle.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError:
            raise BlockingIOError("The file is locked by another process.") from None
else:
    import fcntl

    def lock_file(handle, blocking=True):
        fcntl.flock(handle, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)


def private_ipv4(value):
    address = ipaddress.IPv4Address(value)
    if address == ipaddress.IPv4Address("127.0.0.1") or any(address in network for network in LAN_NETWORKS):
        return str(address)
    raise ValueError("REMOTE_BIND must be a private IPv4 address or 127.0.0.1.")


def _mac_address():
    interfaces = subprocess.check_output(["/sbin/ifconfig", "-l"], text=True).split()
    route = subprocess.run(["/sbin/route", "-n", "get", "default"], capture_output=True, text=True)
    default = re.search(r"interface:\s*(en\d+)\b", route.stdout)
    preferred = [default[1]] if default else []
    for interface in dict.fromkeys(preferred + ["en0"] + interfaces):
        if not re.fullmatch(r"en\d+", interface):
            continue
        result = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", interface], capture_output=True, text=True)
        try:
            return private_ipv4(result.stdout.strip())
        except ValueError:
            continue
    return "127.0.0.1"


def _routed_address():
    """The private IPv4 address of the interface that carries the default route.

    Connecting a UDP socket sends no packets; it only asks the OS which source
    address it would use.
    """
    for probe in ("192.168.0.1", "10.0.0.1", "172.16.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.connect((probe, 9))
                return private_ipv4(udp.getsockname()[0])
        except (OSError, ValueError):
            continue
    return "127.0.0.1"


def _mac_hostname():
    result = subprocess.run(["/usr/sbin/scutil", "--get", "LocalHostName"], capture_output=True, text=True)
    name = result.stdout.strip().lower()
    return f"{name}.local" if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name) else None


def local_endpoints():
    bind = os.environ.get("REMOTE_BIND")
    if bind:
        address = private_ipv4(bind)
    else:
        address = _mac_address() if IS_MAC else _routed_address()
    # Phones resolve a Mac's Bonjour name reliably. Other hosts are reached by IP
    # only, because .local names are not resolvable on every phone and network.
    hostname = _mac_hostname() if IS_MAC else None
    return address, hostname


def open_folder(path):
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606 - opens Explorer on a local folder.
    elif IS_MAC:
        subprocess.Popen(["/usr/bin/open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
