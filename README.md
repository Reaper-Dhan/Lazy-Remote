<img src="assets/logo.svg" alt="" width="80" height="80">

# Lazy Remote

**A phone trackpad, keyboard, and media remote for your computer.** Open it in a mobile browser, pair over your local network, and approve control on the computer.

[Quick start](#quick-start) · [Install on your phone](#install-on-your-phone) · [Controls](#controls) · [Security](#security) · [Troubleshooting](#troubleshooting) · [Development](#development)

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

Click **Set up phone** to start the server, set up input access, and pair your phone.

While the server runs, closing the window keeps the remote going: on a Mac, Lazy Remote stays in the menu bar; on Windows and Linux, it asks whether to minimize. When a phone asks for control, the window comes back so you can approve it.

Current Mac, Windows, and Linux builds include Python. You don’t need to install it separately or install Xcode Command Line Tools to run the app. Older Mac releases may still require an installed Python.

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

Open **Set up phone** in Lazy Remote. On a Mac, use the share menu to AirDrop the file to your own phone. On Windows and Linux, open the certificate folder and transfer the file to your phone over USB or through a file transfer you trust. The guide shows the SHA-256 fingerprint of the certificate. After installing it, scan the separate pairing QR code to open the remote over HTTPS.

| Phone | File | Where to install it |
| --- | --- | --- |
| iPhone or iPad | `Lazy Remote.mobileconfig` | Open **Settings → Profile Downloaded** after transferring the profile. Then enable **Lazy Remote Local Root** in **Settings → General → About → Certificate Trust Settings**. [Apple’s setup guide](https://support.apple.com/en-us/102390) |
| Android | `Lazy Remote Root.cer` | Install it as a **CA certificate** in your phone’s credential settings. The menu varies by device. |

If you already installed an earlier Lazy Remote profile, replace it once when upgrading to the new certificate format. Remove the old profile from your phone after installing the new one.

## Install on your phone

1. Start Lazy Remote on the computer. Install its trust profile or certificate on the phone and open the HTTPS address shown in the launcher.
2. **iPhone or iPad:** In Safari, tap **Share → Add to Home Screen**. Turn on **Open as Web App** if it appears, then tap **Add**. [Apple’s installation guide](https://support.apple.com/en-az/guide/iphone/iphea86e5236/26/ios/26)
3. **Android:** Tap **Install** in Lazy Remote, or choose **Install app** or **Add to Home screen** in your browser’s menu.
4. Open the new icon. Tap **Scan pairing QR**, point the camera at the QR in the desktop launcher, and tap **Request access**. Approve the matching request on the computer. You can also enter the pairing code manually. Camera frames and QR photos are decoded on the phone.

The app uses the address you installed it from. If the numeric address changes, install from the new address. If the Mac’s `.local` address works on your network, you can install from that address instead. The computer must be running Lazy Remote for the controls to work.

If an older iPhone shortcut shows a letter instead of the Lazy Remote logo, remove that Home Screen icon and add the app again from Safari while the updated server is running.

## Controls

Use **Trackpad** for pointing and typing, **Media** for playback and volume, and **Shortcuts** for browser and system actions.

| Input | What it does |
| --- | --- |
| One finger | Moves the pointer. Tap for a left click or tap twice for a double-click. |
| Two fingers | Scrolls using the Mac’s direction settings. Tap with both fingers on the main pad to right-click. |
| Pinch | Sends zoom shortcuts to supported apps when pinch zoom is enabled. |
| Three- or four-finger swipe | Swipe left or right to switch Spaces, up for Mission Control, and down for App Exposé. The phone follows the Mac’s enabled finger counts and keyboard shortcuts. The same actions appear under **Shortcuts**. |
| **Mac swipes** | Turn this on above the trackpad and swipe with one finger. Tap **Mac swipes** again to return one finger to pointer control. Two-finger scroll and right-click still work. |
| Three-finger drag or **Hold** | Drags items. The Mac’s **Three Finger Drag** setting takes priority over three-finger swipes. |
| **Speed** | Adjusts pointer movement and scrolling. |
| Keyboard and media buttons | Type into the focused app, control playback, and run common browser shortcuts. |

iOS uses three-finger swipes for editing. If the page does not receive the gesture, use **Mac swipes** above the trackpad or tap the action under **Shortcuts**. If neither appears, enable the relevant gesture in the Mac’s **Trackpad** settings and the matching shortcut under **Keyboard → Keyboard Shortcuts → Mission Control**.

On macOS 27, switching Spaces briefly opens Mission Control and selects the neighboring desktop on the display under the pointer. At the end of the row, Lazy Remote reports that there is no adjacent Space in that direction.

**On Windows and Linux**, three-finger swipes are off and Mac shortcuts become their usual equivalents: Cmd becomes Ctrl, browser Back and Forward use Alt+arrow, and fullscreen uses F11. The phone hides brightness buttons on Windows. On Linux, the US layout and limited character support apply when the app falls back to `/dev/uinput` for typing.

## Security

- The server listens on one private IPv4 address. Use Lazy Remote on a network you trust and keep its port off the public internet.
- Pairing codes are single-use and expire after **10 minutes**. An access request expires after **90 seconds** and requires approval on the computer.
- **Remember this browser** skips the QR scan for **30 days** on the same browser and hostname. Each new session still requires approval on the computer.
- Approval applies to a single open tab. By default, hiding the remote page locks it. The server also locks it after **15 seconds** without a check-in if it misses the browser's lock request. Enable **Keep phone access when switching apps** before starting the server if you want the same tab to resume without approval. After a reload, a remembered browser can request fresh approval right away.
- The session still locks after **5 minutes** without control input. The server stops after **30 minutes** without an approved connection or successful control command. Enable **Keep running when the phone is idle** to skip the server stop. Use **Lock phone** in the desktop app to end the session and keep your remembered browser.
- An active session can type and click in the focused app. Treat the approved phone as a keyboard and trackpad with access to your computer.
- The computer generates its own HTTPS identity. It discards the root signing key and retains a restricted intermediate key in private application storage. The intermediate permits HTTPS certificates for `.local` names, private IPv4 addresses, and loopback. The phone folder contains only the public profile and certificate.
- Restart the server after a network address change. It issues a new server certificate under the same trusted root. Open the new address and pair there if the browser doesn't remember it. A root replacement, such as a format upgrade or explicit certificate reset, requires installing a new profile or certificate.
- On Windows, the remote can’t control apps running as administrator. On Linux, the virtual input device could reach the lock screen. Lazy Remote requires an active, unlocked logind session. On desktops other than GNOME and KDE, it also requires a readable desktop screen-lock status. Unsupported or unavailable lock detection disables Linux input.

If you discover a security issue, use GitHub’s private vulnerability reporting if it’s available. For ordinary bug reports, include your operating system version, browser, and steps to reproduce. Keep pairing codes, certificate files, and device addresses out of public reports.

## Troubleshooting

| Symptom | Try this |
| --- | --- |
| The phone can’t reach the `.local` address | Make sure both devices are on the same Wi-Fi network. Select the numeric address in the launcher if it’s available. Guest Wi-Fi may block communication between devices. |
| The address changed after switching Wi-Fi | Restart the server and open the new numeric address. If the browser asks, scan the new pairing QR code. |
| The browser shows a certificate warning | Confirm you opened the exact address displayed by Lazy Remote and installed the current certificate from your computer. On iOS, check **Certificate Trust Settings** as well. |
| The page connects but can’t move the cursor or type | Mac: use **Open Accessibility Settings** in the launcher and allow the app or helper named by macOS. Linux: unlock the active desktop, confirm it reports lock state, then check `/dev/uinput` access in the setup guide. |
| The phone can’t reach a Windows PC | Allow Lazy Remote on **Private networks** in **Windows Security → Firewall & network protection → Allow an app through firewall**, and make sure the Wi-Fi network profile is set to **Private**. |
| A three-finger swipe has no effect | On the phone, tap **Mac swipes** above the trackpad and swipe with one finger. If the button is missing, check the Mac’s gesture and keyboard shortcut settings. |

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

The unit tests use temporary certificates and substitute input devices. Release checks also start the packaged server on loopback to verify the phone profile, HTTPS, pairing, approval, and revocation.

The phone scanner bundles [jsQR 1.4.0](https://github.com/cozmo/jsQR) under the [Apache 2.0 license](vendor/jsQR-LICENSE.txt).

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
