import SwiftUI

@main
struct LazyRemotePhoneApp: App {
    @StateObject private var remote = RemoteConnection()
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            RemoteView(remote: remote)
                .onChange(of: phase) { _, value in remote.setActive(value == .active) }
        }
    }
}

private struct RemoteView: View {
    @ObservedObject var remote: RemoteConnection
    @State private var position = CGSize.zero
    @State private var text = ""
    @State private var scanning = false
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 20) {
                    Text(remote.status).font(.subheadline).foregroundStyle(.secondary)
                    if !remote.error.isEmpty {
                        Text(remote.error).foregroundStyle(.red).font(.callout)
                    }
                    if remote.ready {
                        RoundedRectangle(cornerRadius: 24)
                            .fill(Color.secondary.opacity(0.14))
                            .frame(height: 230)
                            .overlay(Label("Trackpad", systemImage: "cursorarrow").allowsHitTesting(false))
                            .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                                remote.move(CGSize(width: value.translation.width - position.width,
                                                   height: value.translation.height - position.height))
                                position = value.translation
                            }.onEnded { value in
                                if hypot(value.translation.width, value.translation.height) < 5 {
                                    remote.command(["action": "click", "button": "left", "count": 1])
                                }
                                position = .zero
                            })
                            .accessibilityLabel("Trackpad. Use the buttons below for clicks.")
                        HStack {
                            Button("Left click") { remote.command(["action": "click", "button": "left", "count": 1]) }
                            Button("Right click") { remote.command(["action": "click", "button": "right", "count": 1]) }
                        }
                        HStack {
                            key("Scroll up", symbol: "arrow.up", body: ["action": "scroll", "dx": 0, "dy": 120])
                            key("Scroll down", symbol: "arrow.down", body: ["action": "scroll", "dx": 0, "dy": -120])
                        }
                        HStack {
                            key("Volume down", symbol: "speaker.minus", body: ["action": "key", "name": "vol_down"])
                            key("Play or pause", symbol: "playpause", body: ["action": "key", "name": "media_play"])
                            key("Volume up", symbol: "speaker.plus", body: ["action": "key", "name": "vol_up"])
                        }
                        if !remote.swipes.isEmpty {
                            LazyVGrid(columns: [GridItem(.adaptive(minimum: 140))], spacing: 12) {
                                ForEach(remote.swipes, id: \.self) { name in
                                    Button(shortcutLabel(name)) { remote.command(["action": "key", "name": name]) }
                                }
                            }
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Text to send").font(.caption)
                            TextField("Type here", text: $text).textFieldStyle(.roundedBorder)
                        }
                        Button("Send text") {
                            guard !text.isEmpty, text.count <= 128 else { return }
                            remote.command(["action": "type", "text": text])
                        }.disabled(text.isEmpty || text.unicodeScalars.count > 128)
                        Button("Enter") { remote.command(["action": "key", "name": "enter"]) }
                    } else if remote.connected {
                        if !remote.match.isEmpty {
                            Text(remote.match).font(.system(size: 38, weight: .semibold, design: .monospaced))
                            Text("Compare this number on the host, then approve the connection there.")
                        } else if !remote.approved && !remote.locking {
                            Text("Pairing code").font(.caption)
                            TextField("Pairing code from the host", text: $remote.pairingCode)
                                .textInputAutocapitalization(.characters)
                                .autocorrectionDisabled()
                                .textFieldStyle(.roundedBorder)
                            Button("Request access") { remote.pair() }.disabled(remote.pairingCode.isEmpty)
                            Button("Scan pairing QR") { scanning = true }
                        }
                    } else {
                        if remote.connecting {
                            ProgressView("Connecting to host…")
                            Button("Cancel connection", action: remote.cancelConnection)
                        }
                        Button("Find hosts", action: remote.scan).disabled(remote.connecting)
                        ForEach(remote.devices) { device in
                            Button(device.name) { remote.connect(device) }.disabled(remote.connecting)
                        }
                        Text("Start the host in Direct Bluetooth mode. Accept the system Bluetooth pairing prompt, then approve access in Lazy Remote.")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                }
                .padding(20).frame(maxWidth: 600)
                .frame(maxWidth: .infinity)
                .buttonStyle(.bordered)
                .controlSize(.large)
            }
            .navigationTitle("Lazy Remote")
            .toolbar {
                if remote.locking {
                    Button("Retry Lock", action: remote.lock).disabled(!remote.connected)
                } else if remote.connected { Button("Lock", action: remote.lock) }
            }
            .sheet(isPresented: $scanning) {
                PairingScanner { value in
                    scanning = false
                    guard let url = URLComponents(string: value), url.scheme == "https",
                          let fragment = url.fragment,
                          let qr = URLComponents(string: "https://remote/?" + fragment)?.queryItems?.first(where: { $0.name == "pair" })?.value,
                          qr.range(of: "^[A-Za-z0-9_-]{43}$", options: .regularExpression) != nil else {
                        remote.error = "Scan a Lazy Remote pairing QR."; return
                    }
                    remote.pair(qr: qr)
                }
            }
        }
    }

    private func key(_ label: String, symbol: String, body: [String: Any]) -> some View {
        Button { remote.command(body) } label: { Image(systemName: symbol).frame(minWidth: 44, minHeight: 44) }
            .accessibilityLabel(label)
            .disabled(remote.unavailable.contains(body["name"] as? String ?? ""))
    }

    private func shortcutLabel(_ name: String) -> String {
        ["swipe_up": "Mission Control", "swipe_down": "App windows",
         "swipe_left": "Next Space", "swipe_right": "Previous Space"][name] ?? name
    }
}
