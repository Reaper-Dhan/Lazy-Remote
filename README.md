<img src="assets/logo.svg" alt="" width="80" height="80">

# Lazy Remote

**A phone trackpad, keyboard, and media remote for your Mac.** Open it in a mobile browser, pair over your local network, and approve control from the Mac.

[Quick start](#quick-start) · [Controls](#controls) · [Security](#security) · [Troubleshooting](#troubleshooting) · [Development](#development)

| Host | Controller | Connection |
| --- | --- | --- |
| macOS 13+ | Touchscreen browser or a browser with a mouse or trackpad | HTTPS on the local network |

## Quick start

You’ll need a Mac, Python 3.9+, and the Xcode Command Line Tools with Swift 5.9 or later. Connect your Mac and phone to the same private Wi-Fi network.

```bash
git clone https://github.com/Reaper-Dhan/Lazy-Remote.git
cd Lazy-Remote
./"Build Launcher.command"
open "Lazy Remote.app"
```

1. In **Lazy Remote**, click **Start server**. If macOS asks for input permission, grant it in **System Settings → Privacy & Security → Accessibility** or **Device Control and Data Access**, depending on the macOS version.
2. Set up HTTPS on the phone once using the instructions below.
3. Scan the QR code on the Mac with your phone camera and tap **Request access**. To pair manually, open the displayed HTTPS address and reveal the code under **Enter a code instead** on the Mac.
4. Compare the six-digit number shown on both screens and click **Approve** on the Mac.

### Trust this Mac on your phone

Click **Show phone certificates** in Lazy Remote. Transfer the public file for your phone directly from your own Mac.

| Phone | File | Where to install it |
| --- | --- | --- |
| iPhone or iPad | `Lazy Remote.mobileconfig` | Open **Settings → Profile Downloaded** after transferring the profile. Then enable **Lazy Remote Local Root** in **Settings → General → About → Certificate Trust Settings**. [Apple’s setup guide](https://support.apple.com/en-us/102390) |
| Android | `Lazy Remote Root.cer` | Install it as a **CA certificate** in your phone’s credential settings. The menu varies by device. |

If you upgraded from Couch Remote, Lazy Remote reuses your existing certificate and remembered browser. The certificate files may still have the old name.

## Controls

| Input | What it does |
| --- | --- |
| One finger | Moves the pointer. Tap and double-tap work when **Tap to click** is enabled on the Mac. |
| Two fingers | Scrolls using the Mac’s direction settings. Tap to right-click if the Mac uses two-finger secondary click. |
| Pinch | Sends zoom shortcuts to supported apps when pinch zoom is enabled. |
| Three-finger swipe | Swipe left or right to switch Spaces, up for Mission Control, and down for App Exposé. Enable the matching gestures in **Trackpad** settings and shortcuts under **Keyboard → Keyboard Shortcuts → Mission Control**. The same actions appear under **More**. |
| Three-finger drag or **Hold** | Drags items. The Mac’s **Three Finger Drag** setting takes priority over three-finger swipes. |
| **Speed** | Adjusts pointer movement and scrolling. |
| Keyboard and media buttons | Type into the focused app, control playback, and run common browser shortcuts. |

Safari may intercept some touches before they reach the page. Use **More** if a three-finger swipe is intercepted. Pressure-sensitive input such as Force Click is outside the scope of the browser remote.

## Security

- The server listens on one private IPv4 address. Use Lazy Remote on a network you trust and keep its port off the public internet.
- Pairing codes are single-use and expire after **10 minutes**. An access request expires after **90 seconds** and requires approval on the Mac.
- **Remember this browser** skips the QR scan for **30 days** on the same browser and hostname. Each new session still requires approval on the Mac.
- Checking notifications or switching tabs pauses input. The session locks after **5 minutes** without control input, and the server shuts down after **30 minutes** without an approved connection or successful control command. Use **Lock remote** or **Stop server** to end access immediately.
- An active session can type and click in the current Mac app. Treat the approved phone as a keyboard and trackpad with access to your Mac.
- The Mac generates its own HTTPS identity. Share only the public profile or certificate shown in the launcher. The root signing key is discarded after generation, and the server key stays in the Mac user’s private application data.

If you discover a security issue, use GitHub’s private vulnerability reporting if it’s available. For ordinary bug reports, include your macOS version, browser, and steps to reproduce. Keep pairing codes, certificate files, and device addresses out of public reports.

## Troubleshooting

| Symptom | Try this |
| --- | --- |
| The phone can’t reach the `.local` address | Make sure both devices are on the same Wi-Fi network. Select the numeric address in the launcher if it’s available. Guest Wi-Fi may block communication between devices. |
| The numeric address is missing after a Wi-Fi change | Stop the server and run `./"Start Remote.command" --renew-certificate`. Replace the old phone profile with the new one. |
| The browser shows a certificate warning | Confirm you opened the exact address displayed by Lazy Remote and installed the current certificate from your Mac. On iOS, check **Certificate Trust Settings** as well. |
| The page connects but can’t move the cursor or type | Use **Open Accessibility Settings** in the launcher and grant the requested input permission. |
| A three-finger swipe has no effect | Check the corresponding Mac gesture and keyboard shortcut. Try the same action under **More** to check whether Safari is intercepting the gesture. |

## Development

```bash
python3 -B -m unittest -v test_remote
./"Build Launcher.command"
```

The tests use temporary certificates and a substitute input helper, so they can run without moving your cursor or typing on your Mac.

| Component | Role |
| --- | --- |
| [Launcher.swift](Launcher.swift) | SwiftUI launcher, QR code, and local approval screen |
| [auth.py](auth.py) and [server.py](server.py) | Pairing, session limits, HTTPS endpoints, and command validation |
| [tls_setup.py](tls_setup.py) | Local certificates and phone trust profiles |
| [input_helper.swift](input_helper.swift) | macOS mouse, keyboard, and media events |
| [index.html](index.html), [remote.js](remote.js), [remote.css](remote.css) | Mobile browser interface |

**Terminal mode.** Run `./"Start Remote.command"` to start the server in Terminal. Use the printed `approve` or `deny` command and matching number. Press **Enter** on its own to forget the browser and generate a new code, or **Ctrl+C** to stop. Set `REMOTE_BIND` to choose a private IPv4 interface or `REMOTE_PORT` to override port 8765.

**Portability.** The browser interface and pairing code can be reused for other hosts. The shipped launcher, certificate provisioning, and input helper target macOS. Linux and Windows need their own host integrations.

## License

[MIT](LICENSE)
