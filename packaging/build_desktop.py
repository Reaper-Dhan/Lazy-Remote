"""Build the one-file Lazy Remote app for Windows or Linux with PyInstaller.

Run on the target OS:  python packaging/build_desktop.py
Output: dist/Lazy-Remote-Windows.zip or dist/Lazy-Remote-Linux-<arch>.tar.gz
"""
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parent.parent
DATA = [
    ("index.html", "."), ("remote.js", "."), ("remote.css", "."),
    ("assets/logo.svg", "assets"), ("assets/touch-icon.png", "assets"),
]


def main():
    windows = os.name == "nt"
    if not windows and not sys.platform.startswith("linux"):
        sys.exit("Build the Mac app with 'Build Launcher.command' instead.")
    name = "Lazy Remote" if windows else "lazy-remote"
    work = ROOT / "build" / "pyinstaller"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", name, "--icon", str(ROOT / "assets" / "logo.png"),
        "--distpath", str(work / "dist"), "--workpath", str(work / "build"), "--specpath", str(work),
        "--hidden-import", "server", "--hidden-import", "input_backends",
        "--paths", str(ROOT),
    ]
    for source, target in DATA:
        command += ["--add-data", f"{ROOT / source}{os.pathsep}{target}"]
    command.append(str(ROOT / "desktop_launcher.py"))
    subprocess.run(command, check=True, cwd=ROOT)

    binary = work / "dist" / (name + (".exe" if windows else ""))
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    if windows:
        archive = output / "Lazy-Remote-Windows.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(binary, binary.name)
    else:
        archive = output / f"Lazy-Remote-Linux-{platform.machine()}.tar.gz"
        binary.chmod(0o755)
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(binary, binary.name)
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    print(f"Packed {archive}")


if __name__ == "__main__":
    main()
