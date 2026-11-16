"""Build the Lazy Remote app for Windows or Linux with PyInstaller.

Run on the target OS:  python packaging/build_desktop.py
Output: dist/Lazy-Remote-Windows.zip or dist/Lazy-Remote-Linux-<arch>.tar.gz

The app is a folder (PyInstaller "onedir"), not one self-extracting file:
antivirus scanners flag one-file PyInstaller apps far more often, and a folder
starts faster because nothing is unpacked on each launch.
"""
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parent.parent
DATA = [
    ("index.html", "."), ("remote.js", "."), ("remote.css", "."),
    ("pwa.js", "."), ("qr-scan.js", "."), ("service-worker.js", "."), ("manifest.webmanifest", "."), ("offline.html", "."),
    ("vendor/jsQR.js", "vendor"), ("vendor/jsQR-LICENSE.txt", "vendor"),
    ("assets/apple-touch-icon.png", "assets"),
    ("assets/app-icon-192.png", "assets"), ("assets/app-icon-512.png", "assets"),
    ("assets/logo.svg", "assets"), ("assets/touch-icon.png", "assets"),
]


def version():
    """The release tag (v1.2.3 -> 1.2.3) when CI builds a tag, else 0.0.0."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", os.environ.get("GITHUB_REF_NAME", ""))
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)


def windows_version_file(path, numbers):
    """Version details Windows shows in the file's Properties, which also helps
    antivirus and SmartScreen tell the app apart from anonymous executables."""
    text = ".".join(map(str, numbers))
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers + (0,)}, prodvers={numbers + (0,)}),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Lazy Remote'),
      StringStruct('FileDescription', 'Lazy Remote: control this PC from your phone'),
      StringStruct('FileVersion', '{text}'),
      StringStruct('InternalName', 'Lazy Remote'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'Lazy Remote.exe'),
      StringStruct('ProductName', 'Lazy Remote'),
      StringStruct('ProductVersion', '{text}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def main():
    windows = os.name == "nt"
    if not windows and not sys.platform.startswith("linux"):
        sys.exit("Build the Mac app with 'Build Launcher.command' instead.")
    name = "Lazy Remote" if windows else "lazy-remote"
    work = ROOT / "build" / "pyinstaller"
    work.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--noupx", "--name", name, "--icon", str(ROOT / "assets" / "logo.png"),
        "--distpath", str(work / "dist"), "--workpath", str(work / "build"), "--specpath", str(work),
        "--hidden-import", "server", "--hidden-import", "input_backends",
        "--hidden-import", "bluetooth_transport",
        "--paths", str(ROOT),
    ]
    if windows:
        version_file = work / "version.txt"
        windows_version_file(version_file, version())
        command += ["--version-file", str(version_file)]
    for source, target in DATA:
        command += ["--add-data", f"{ROOT / source}{os.pathsep}{target}"]
    command.append(str(ROOT / "desktop_launcher.py"))
    subprocess.run(command, check=True, cwd=ROOT)

    folder = work / "dist" / name
    shutil.copy2(ROOT / "LICENSE", folder / "LICENSE.txt")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    if windows:
        archive = output / "Lazy-Remote-Windows.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(folder.rglob("*")):
                bundle.write(path, Path(name) / path.relative_to(folder))
    else:
        archive = output / f"Lazy-Remote-Linux-{platform.machine()}.tar.gz"
        (folder / name).chmod(0o755)
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(folder, name)
    shutil.rmtree(work, ignore_errors=True)
    print(f"Packed {archive}")


if __name__ == "__main__":
    main()
