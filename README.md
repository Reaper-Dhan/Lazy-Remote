<img src="assets/logo.svg" alt="" width="80" height="80">

# Lazy Remote

**A phone trackpad, keyboard, and media remote for your computer.** Open it in a mobile browser, pair over your local network, and approve control on the computer.

[Quick start](#quick-start) · [Controls](#controls) · [Security](#security) · [Troubleshooting](#troubleshooting) · [Development](#development)

| Host | Controller | Connection |
| --- | --- | --- |
| macOS 13+, Windows 10/11, Linux (X11 or Wayland) | Touchscreen browser or a browser with a mouse or trackpad | HTTPS on the local network |

## Quick start

You only need the app. No repo, no Terminal. Get it from the [Releases page](https://github.com/Reaper-Dhan/Lazy-Remote/releases).

| Computer | Download | First open |
| --- | --- | --- |
| Mac | `Lazy-Remote-macOS.zip` | Unzip and drag **Lazy Remote** into **Applications**. Control-click it, choose **Open**, then **Open** again. On newer macOS, use **System Settings → Privacy & Security → Open Anyway**. |
| Windows | `Lazy-Remote-Windows.zip` | Unzip, open the **Lazy Remote** folder and run **Lazy Remote.exe**. If SmartScreen appears, click **More info → Run anyway**. When the firewall asks, allow **Private networks** only. |
| Linux (Ubuntu 22.04+, Debian 12+, RHEL 9+) | `Lazy-Remote-Linux-x86_64.tar.gz` | Extract and run `lazy-remote/lazy-remote`. The setup guide gives a one-time command that lets it use `/dev/uinput`. |

The apps aren’t signed by Apple or Microsoft, so the system warns once. Each release includes `SHA256SUMS.txt`; check your download against it before you open the app (`shasum -a 256 -c SHA256SUMS.txt --ignore-missing`, or `Get-FileHash` in PowerShell). Ignore the **Source code** files that GitHub adds to every release. To prove a file was built by this project’s workflow, run `gh attestation verify <file> --repo Reaper-Dhan/Lazy-Remote`.

Click **Start server**. The **Setup guide** opens on first launch and walks you through the rest: input access, trusting this computer on your phone, and pairing.

Everything ships inside the app. The Mac app uses the Python 3 built into macOS. If the Command Line Tools aren’t installed, it uses Homebrew’s Python, or asks you to run `xcode-select --install`. The Windows and Linux apps include their own Python.

Connect your computer and phone to the same private Wi-Fi network.

### Linux input access

Linux apps can’t move the pointer on Wayland, so Lazy Remote creates a virtual mouse and keyboard through `/dev/uinput`. Run this once, then log out and back in:

```bash
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/70-lazy-remote-uinput.rules
sudo modprobe uinput && sudo udevadm control --reload-rules && sudo udevadm trigger
```

The `uaccess` tag gives the device only to the person signed in at the screen. Avoid adding your account to the `input` group instead: that also lets programs read every keyboard.

This access isn’t limited to Lazy Remote: any app you run can then create a virtual keyboard and mouse, which Wayland normally prevents. Remove the rule when you stop using the remote:

```bash
sudo rm /etc/udev/rules.d/70-lazy-remote-uinput.rules && sudo udevadm control --reload-rules && sudo udevadm trigger
```

For typing, Lazy Remote uses `xdotool` (X11) or `wtype` (Wayland, except GNOME) when they’re installed, so any keyboard layout and accented letters work. Without them it types through `/dev/uinput` with a US layout.

### Trust this computer on your phone

Open **Set up phone** in Lazy Remote. On a Mac it sends the right file with AirDrop or the share menu. On every system it can also **send it over Wi-Fi with a QR code**: scan the code with the phone camera and download the file. It also shows the steps and the certificate fingerprint.

The Wi-Fi link is plain HTTP, because the phone can’t use HTTPS before it trusts the computer. Someone else on the same network could swap the file, so compare the fingerprint on the phone with the one in Lazy Remote before you install it. The link uses a random address and stops after 2 minutes or two downloads.

| Phone | File | Where to install it |
| --- | --- | --- |
| iPhone or iPad | `Lazy Remote.mobileconfig` | Open **Settings → Profile Downloaded** after transferring the profile. Then enable **Lazy Remote Local Root** in **Settings → General → About → Certificate Trust Settings**. [Apple’s setup guide](https://support.apple.com/en-us/102390) |
| Android | `Lazy Remote Root.cer` | Install it as a **CA certificate** in your phone’s credential settings. The menu varies by device. |

If you upgraded from Couch Remote, Lazy Remote reuses your existing certificate and remembered browser. The certificate files may still have the old name.

## Controls

| Input | What it does |
| --- | --- |
| One finger | Moves the pointer. Tap for a left click or tap twice for a double-click. |
| Two fingers | Scrolls using the Mac’s direction settings. Tap with both fingers on the main pad to right-click. |
| Pinch | Sends zoom shortcuts to supported apps when pinch zoom is enabled. |
| Three-finger swipe | Swipe left or right to switch Spaces, up for Mission Control, and down for App Exposé. Enable the matching gestures in **Trackpad** settings and shortcuts under **Keyboard → Keyboard Shortcuts → Mission Control**. The same actions appear under **More**. |
| Three-finger drag or **Hold** | Drags items. The Mac’s **Three Finger Drag** setting takes priority over three-finger swipes. |
| **Speed** | Adjusts pointer movement and scrolling. |
| Keyboard and media buttons | Type into the focused app, control playback, and run common browser shortcuts. |

Safari may intercept three-finger swipes for iOS editing. Turn on **More → Two-finger Mac swipes** to use two fingers for those shortcuts, or use the individual shortcut buttons. In that mode, two-finger tap still right-clicks and the **Scroll** strip handles scrolling.

**On Windows and Linux**, three-finger swipes are off and Mac shortcuts become their usual equivalents: Cmd becomes Ctrl, browser Back and Forward use Alt+arrow, and fullscreen uses F11. Brightness buttons do nothing on Windows. On Linux, typing uses a US keyboard layout and skips characters outside it, such as accented letters.

## Security

- The server listens on one private IPv4 address. Use Lazy Remote on a network you trust and keep its port off the public internet.
- Pairing codes are single-use and expire after **10 minutes**. An access request expires after **90 seconds** and requires approval on the Mac.
- **Remember this browser** skips the QR scan for **30 days** on the same browser and hostname. Each new session still requires approval on the Mac.
- Checking notifications or switching tabs pauses input. The session locks after **5 minutes** without control input, and the server shuts down after **30 minutes** without an approved connection or successful control command. Turn on **Keep running when the phone is idle** in the launcher to skip the 30-minute stop; the 5-minute lock still applies. Use **Lock remote** or **Stop server** to end access immediately.
- An active session can type and click in the current Mac app. Treat the approved phone as a keyboard and trackpad with access to your Mac.
- The computer generates its own HTTPS identity. Share only the public profile or certificate shown in the launcher. The root signing key is discarded after generation, and the root may only vouch for `.local` names, so it can’t be used to impersonate a real website. The server key stays in the user’s private application data (`~/Library/Application Support/Lazy Remote`, `%LOCALAPPDATA%\Lazy Remote`, or `~/.local/share/lazy-remote`), which is limited to your account on every system.
- **Upgrading from an earlier version** creates a new certificate once, to add the `.local` limit. Install the new profile or certificate on your phone and remove the old one.
- On Windows, the remote can’t control apps running as administrator. On Linux, the virtual input device could reach the lock screen, so Lazy Remote sends nothing while the desktop session is locked.

If you discover a security issue, use GitHub’s private vulnerability reporting if it’s available. For ordinary bug reports, include your macOS version, browser, and steps to reproduce. Keep pairing codes, certificate files, and device addresses out of public reports.

## Troubleshooting

| Symptom | Try this |
| --- | --- |
| The phone can’t reach the `.local` address | Make sure both devices are on the same Wi-Fi network. Select the numeric address in the launcher if it’s available. Guest Wi-Fi may block communication between devices. |
| The numeric address is missing after a Wi-Fi change | Stop the server and run `./"Start Remote.command" --renew-certificate`. Replace the old phone profile with the new one. |
| The browser shows a certificate warning | Confirm you opened the exact address displayed by Lazy Remote and installed the current certificate from your Mac. On iOS, check **Certificate Trust Settings** as well. |
| The page connects but can’t move the cursor or type | Mac: use **Open Accessibility Settings** in the launcher and allow the app or helper named by macOS. Linux: run the `/dev/uinput` command from the setup guide, log out and in, then click **Check again**. |
| The phone can’t reach a Windows PC | Allow Lazy Remote on **Private networks** in **Windows Security → Firewall & network protection → Allow an app through firewall**, and make sure the Wi-Fi network profile is set to **Private**. |
| A three-finger swipe has no effect | Check the corresponding Mac gesture and keyboard shortcut, then try **More → Two-finger Mac swipes**. |

## Development

```bash
python3 -B -m unittest -v test_platform test_remote
node --test test_remote_js.cjs
```

**Mac app.** You need the Xcode Command Line Tools with Swift 5.9 or later. `./"Build Launcher.command"` makes a universal app (Apple Silicon and Intel) with the server inside it and packs `dist/Lazy-Remote-macOS.zip`.

**Windows and Linux apps.** Build on the target system, because PyInstaller can’t cross-compile:

```bash
python -m pip install -r packaging/requirements.txt
python packaging/build_desktop.py
```

**Releases.** [.github/workflows/build.yml](.github/workflows/build.yml) runs the tests on all three systems and builds all three apps on every push. Push a tag such as `v1.2.0` and it publishes a release with the three downloads, `SHA256SUMS.txt`, and the notes in [packaging/release-notes.md](packaging/release-notes.md). Actions are pinned to commit hashes and bundled packages to exact versions in [packaging/requirements.txt](packaging/requirements.txt).

The tests use temporary certificates and substitute input devices, so they can run without moving your cursor or typing.

| Component | Role |
| --- | --- |
| [Launcher.swift](Launcher.swift) | Mac launcher: SwiftUI window, QR code, approval, and setup guide |
| [desktop_launcher.py](desktop_launcher.py) | Windows and Linux launcher: the same window in Tk |
| [auth.py](auth.py) and [server.py](server.py) | Pairing, session limits, HTTPS endpoints, and command validation |
| [tls_setup.py](tls_setup.py) | Local certificates and phone trust profiles |
| [platform_support.py](platform_support.py) | Data folder, file lock, and network address for each system |
| [input_helper.swift](input_helper.swift) | macOS mouse, keyboard, and media events |
| [input_backends.py](input_backends.py) | Windows (`SendInput`) and Linux (`/dev/uinput`) mouse, keyboard, and media events |
| [index.html](index.html), [remote.js](remote.js), [remote.css](remote.css) | Mobile browser interface |

**Terminal mode.** Run `./"Start Remote.command"` on a Mac, or `python3 server.py` on any system, to start the server in a terminal. Use the printed `approve` or `deny` command and matching number. Press **Enter** on its own to forget the browser and generate a new code, or **Ctrl+C** to stop. Set `REMOTE_BIND` to choose a private IPv4 interface or `REMOTE_PORT` to override port 8765. Windows and Linux need `pip install cryptography` for terminal mode unless `openssl` is installed.

## License

[MIT](LICENSE)
