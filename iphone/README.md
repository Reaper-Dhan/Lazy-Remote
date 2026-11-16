# Native iPhone / iPad Bluetooth preview

Connect to the Mac host without Wi-Fi or tethering. This is an early native client, separate from the browser PWA. Windows, Linux, Android phones, and TV do not have a direct BLE transport yet.

## Install on your iPhone

1. Open `Lazy Remote.xcodeproj` in Xcode 16 or later, with an iOS 17+ SDK. Select the **Lazy Remote** target.
2. In **Signing & Capabilities**, select your own development team. If necessary, change `org.lazyremote.phone` to a unique bundle identifier. No signing credentials are included in the repository.
3. Connect your iPhone, enable Developer Mode if requested by iOS, choose it as the run destination, and run the app. A personal development team can require periodic reinstalling.
4. Build and open the Mac app from the same checkout. Select **Direct Bluetooth**, then start the server. Allow Bluetooth access on both devices and Accessibility on the Mac.
5. Tap **Find hosts** on iPhone, select the host, and accept any system pairing prompt. The name alone does not identify a trusted host. Enter the Mac’s one-use code or scan its QR inside the native app. Compare the six-digit number on both devices before approving on the Mac.

The app icon uses the project’s existing logo. Scanning requires camera access; code entry works without it. No certificate profile is required for the native connection.

## Current controls and sessions

The preview includes pointer movement, tap/left/right click, scroll buttons, text, Enter, volume and playback, and buttons for Mac shortcuts enabled in host settings. Unavailable media keys are disabled. Multi-finger gestures, a speed setting, and full parity with the browser remote remain future work.

Controls are serialized so status checks cannot discard a button press. Disconnects drop unconfirmed control commands instead of replaying clicks or typing. An approved session reconnects while its host timeout permits; no access is remembered across process restarts. Backgrounding pauses controls. The same host inactivity limits apply as the browser remote.

**Lock** cancels a pending request or revokes an approved session. If the connection drops before the host confirms the lock, use **Lock phone** on the Mac. Do not assume an offline phone can revoke access immediately.

## Transport and verification

The Mac advertises service `6C617A79-5265-4D6F-7465-000000000001`. Writes and notifications require an encrypted Core Bluetooth link. Newline-framed JSON messages are bounded and fragmented to the negotiated ATT size. Replies are tied to their subscribing central; only one request is accepted at a time. The native host forwards messages through its private launcher pipe, not an unauthenticated HTTP endpoint.

OS Bluetooth pairing is separate from Lazy Remote authorization. The same one-use QR/code, explicit host approval, per-session credentials, request-key claim recovery, command validation, rate limits, revocation, and timeouts apply. The preview does not implement WebAuthn/passkeys or add an independent application-level encrypted handshake; it relies on system Bluetooth link security. Use it only with devices you trust and verify system pairing prompts. Do not describe it as equivalent to an independently verified HTTPS identity or as hackproof.

Compile for a simulator:

```bash
xcodebuild -project "iphone/Lazy Remote.xcodeproj" -scheme "Lazy Remote" \
  -sdk iphonesimulator -configuration Debug -derivedDataPath .cache/iphone-build \
  CODE_SIGNING_ALLOWED=NO build
```

CI also compiles for physical iOS hardware without signing. An unsigned build is not an installable IPA. A simulator compile cannot verify Bluetooth radio discovery, encryption prompts, physical input, or reconnection. Those checks require an iPhone and Mac and are still outstanding. Before releasing, test code and QR pairing, denied/expired approval, host and phone lock, Bluetooth off/on, app switching within and beyond the timeout, and a lost connection during typing.

Run the native session regression harness on macOS (also run by CI):

```bash
swiftc -swift-version 5 -D DEBUG -parse-as-library iphone/RemoteConnection.swift \
  test_iphone.swift -o .cache/test-iphone
.cache/test-iphone
```

It exercises the real client’s response handling with a simulated transport: transient status failures retain credentials, controls wait behind status checks, backgrounding discards queued commands, and late approval replies cannot restore a locked session. Release builds exclude the simulated transport hooks.
