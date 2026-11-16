import AppKit
import CoreImage.CIFilterBuiltins
import SwiftUI

private struct ServerEvent: Decodable {
    let event: String
    let urls: [String]?
    let certificates: String?
    let fingerprint: String?
    let loopback: Bool?
    let inputReady: Bool?
    let code: String?
    let qr: String?
    let paired: Bool?
    let remembered: Bool?
    let match: String?
    let approved: Bool?
    let message: String?

    enum CodingKeys: String, CodingKey {
        case event, urls, certificates, fingerprint, loopback, code, qr, paired, remembered, match, approved, message
        case inputReady = "input_ready"
    }
}

@MainActor
private final class Launcher: ObservableObject {
    enum Phase { case stopped, starting, running, stopping }

    /// The app delegate reads this to keep the app alive while the server runs.
    static weak var current: Launcher?
    /// Opens the main window again; set by the window itself when it first appears.
    var showWindow: (() -> Void)?

    init() { Launcher.current = self }

    var isActive: Bool { phase == .starting || phase == .running }

    @Published private(set) var phase = Phase.stopped
    @Published private(set) var urls: [String] = []
    @Published var address = ""
    @Published private(set) var code: String?
    @Published private(set) var qr: String?
    @Published private(set) var match: String?
    @Published private(set) var paired = false
    @Published private(set) var remembered = false
    @Published private(set) var approved = false
    @Published private(set) var loopback = false
    @Published private(set) var inputReady: Bool?
    @Published private(set) var notice: String?
    @Published private(set) var certificates: String?
    @Published private(set) var fingerprint: String?
    /// Off by default: the server stops after 30 minutes without phone activity.
    @Published var keepRunning = UserDefaults.standard.bool(forKey: "keepRunning") {
        didSet { UserDefaults.standard.set(keepRunning, forKey: "keepRunning") }
    }

    @Published var allowBackgroundResume = UserDefaults.standard.bool(forKey: "allowBackgroundResume") {
        didSet { UserDefaults.standard.set(allowBackgroundResume, forKey: "allowBackgroundResume") }
    }

    private var process: Process?
    private var controls: FileHandle?

    /// The server ships inside the app (Contents/Resources/server). A source
    /// checkout next to the app is used as a fallback for development.
    private static func serverDirectory() -> URL? {
        let candidates = [
            Bundle.main.resourceURL?.appendingPathComponent("server", isDirectory: true),
            Bundle.main.bundleURL.deletingLastPathComponent(),
        ].compactMap { $0 }
        return candidates.first {
            FileManager.default.fileExists(atPath: $0.appendingPathComponent("server.py").path)
        }
    }

    /// The app ships its own Python (Contents/Resources/python), signed with the app,
    /// so no developer tools are needed. Apple's Python is the fallback for a
    /// development build; /usr/bin/python3 is only a stub until the Command Line
    /// Tools are installed, so check for those first. Homebrew comes last because
    /// any program running as the user can replace it, and the server runs with
    /// this app's Accessibility access.
    private static func python() -> String? {
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("python/bin/python3").path,
           FileManager.default.isExecutableFile(atPath: bundled) {
            return bundled
        }
        let tools = Process()
        tools.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
        tools.arguments = ["-p"]
        tools.standardOutput = FileHandle.nullDevice
        tools.standardError = FileHandle.nullDevice
        if (try? tools.run()) != nil {
            tools.waitUntilExit()
            if tools.terminationStatus == 0 { return "/usr/bin/python3" }
        }
        return ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    /// The phone files, found by type so certificates from Couch Remote still work.
    func certificateFile(_ fileExtension: String) -> URL? {
        guard let certificates else { return nil }
        let directory = URL(fileURLWithPath: certificates, isDirectory: true)
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.first { $0.pathExtension == fileExtension }
    }

    func start() {
        guard phase == .stopped else { return }
        notice = nil
        guard let project = Self.serverDirectory() else {
            notice = "This copy of Lazy Remote is incomplete. Download it again."
            return
        }
        let script = project.appendingPathComponent("server.py")
        guard let python = Self.python() else {
            notice = "Install Python 3 to start Lazy Remote. Run xcode-select --install in Terminal."
            return
        }

        let server = Process()
        let input = Pipe()
        let output = Pipe()
        server.executableURL = URL(fileURLWithPath: python)
        var arguments = ["-E", "-s", "-B", "-u", script.path, "--launcher"]
        if keepRunning { arguments.append("--keep-running") }
        if allowBackgroundResume { arguments.append("--allow-background-resume") }
        server.arguments = arguments
        server.currentDirectoryURL = project
        server.standardInput = input
        server.standardOutput = output
        server.standardError = FileHandle.nullDevice

        do {
            try server.run()
        } catch {
            notice = "Could not start Lazy Remote. \(error.localizedDescription)"
            return
        }

        phase = .starting
        process = server
        controls = input.fileHandleForWriting

        let stream = output.fileHandleForReading
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var buffer = Data()
            while true {
                let chunk = stream.availableData
                guard !chunk.isEmpty else { break }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = Data(buffer[..<newline])
                    buffer.removeSubrange(...newline)
                    if let event = try? JSONDecoder().decode(ServerEvent.self, from: line) {
                        DispatchQueue.main.async { self?.receive(event) }
                    }
                }
            }
            server.waitUntilExit()
            DispatchQueue.main.async { self?.didStop(status: server.terminationStatus) }
        }
    }

    func stop() {
        guard let process, phase == .starting || phase == .running else { return }
        phase = .stopping
        try? controls?.close()
        if process.isRunning { process.terminate() }
    }

    func send(_ command: String) {
        guard phase == .running, let controls else { return }
        do {
            try controls.write(contentsOf: Data((command + "\n").utf8))
        } catch {
            notice = "The server stopped responding. Stop and start it again."
        }
    }

    func openAccessibility(request: Bool = false) {
        if request { send("request_input_access") }
        let settings = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!
        if !NSWorkspace.shared.open(settings) {
            notice = "Open System Settings → Privacy & Security → Accessibility or Device Control and Data Access."
        }
    }

    func copyAddress() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(address, forType: .string)
    }

    private func receive(_ event: ServerEvent) {
        if event.event == "notice" {
            notice = event.message
            return
        }
        guard phase == .starting || phase == .running else { return }
        if event.event == "permission" {
            inputReady = event.inputReady
            return
        }
        if event.event == "ready" {
            urls = event.urls ?? []
            address = urls.first ?? ""
            certificates = event.certificates
            fingerprint = event.fingerprint
            loopback = event.loopback == true
            inputReady = event.inputReady
            phase = .running
            if inputReady == false { openAccessibility() }
        }
        code = event.code
        qr = event.qr
        if event.match != nil && match == nil {
            // A phone asks for control while the window may be closed: bring it back.
            showWindow?()
            NSApp.activate(ignoringOtherApps: true)
        }
        match = event.match
        paired = event.paired == true
        remembered = event.remembered == true
        approved = event.approved == true
    }

    private func didStop(status: Int32) {
        if phase != .stopping, status != 0, notice == nil {
            notice = "The server closed unexpectedly. Try Start again."
        }
        process = nil
        controls = nil
        phase = .stopped
        urls = []
        address = ""
        code = nil
        qr = nil
        match = nil
        paired = false
        remembered = false
        approved = false
        inputReady = nil
        certificates = nil
        fingerprint = nil
    }
}

private struct PairingQR: View {
    let payload: String

    private static let context = CIContext()

    var body: some View {
        if let image = makeImage() {
            Image(decorative: image, scale: 1)
                .resizable()
                .interpolation(.none)
                .scaledToFit()
                .frame(width: 144, height: 144)
                .padding(16)
                .background(.white, in: RoundedRectangle(cornerRadius: 12))
                .accessibilityLabel("Pairing QR code. Scan with your phone camera.")
        }
    }

    private func makeImage() -> CGImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(payload.utf8)
        filter.correctionLevel = "M"
        guard let image = filter.outputImage else { return nil }
        let scaled = image.transformed(by: CGAffineTransform(scaleX: 8, y: 8))
        return Self.context.createCGImage(scaled, from: scaled.extent)
    }
}

private struct SetupGuide: View {
    @ObservedObject var launcher: Launcher
    @Environment(\.dismiss) private var dismiss
    @State private var phone = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("Set up your phone", systemImage: "iphone").font(.title2.bold())

            step(1, "Start the server") {
                if launcher.phase == .stopped {
                    Button("Start server", action: launcher.start).buttonStyle(.borderedProminent)
                    Toggle("Extend phone reconnect window", isOn: $launcher.allowBackgroundResume)
                    Text("Resume the same tab within one minute, or up to five minutes with this on.")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Starting the server creates this Mac’s certificate.")
                } else if launcher.phase == .running {
                    Text("Server running.")
                } else {
                    ProgressView().controlSize(.small)
                }
                if let notice = launcher.notice { Text(notice) }
            }

            step(2, "Allow mouse and keyboard control") {
                Text("Turn on Lazy Remote in System Settings → Privacy & Security → Accessibility.")
                if launcher.phase == .running && launcher.inputReady != true {
                    Button("Open Accessibility Settings") { launcher.openAccessibility(request: true) }
                }
            }

            step(3, "Trust this Mac on your phone (once)") {
                Picker("Phone", selection: $phone) {
                    Text("iPhone or iPad").tag(0)
                    Text("Android").tag(1)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 260)

                if launcher.phase != .running {
                    Text("Start the server first to create the certificate.").foregroundStyle(.secondary)
                } else if phone == 0 {
                    share(launcher.certificateFile("mobileconfig"), label: "Send profile to iPhone…")
                    numbered([
                        "Send it with AirDrop to your own iPhone.",
                        "On the iPhone, open Settings → Profile Downloaded → Install.",
                        "Open Settings → General → About → Certificate Trust Settings.",
                        "Turn on Lazy Remote Local Root.",
                    ])
                } else {
                    share(launcher.certificateFile("cer"), label: "Send certificate to Android…")
                    numbered([
                        "Send it to your own phone, for example with Quick Share or a USB cable.",
                        "Open Settings and search for “CA certificate”.",
                        "Choose Install a certificate → CA certificate, then pick the file.",
                    ])
                }
                if let fingerprint = launcher.fingerprint {
                    Text("Fingerprint, to check on the phone: \(fingerprint)")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("Transfer the profile or certificate directly to your own phone. Use AirDrop or a cable.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            step(4, "Pair your phone") {
                Text("Join the same Wi-Fi. Open the phone address to install Lazy Remote, or scan the pairing QR. Approve the request on this Mac.")
            }

            HStack {
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(24)
        .frame(width: 520, alignment: .leading)
    }

    private func step<Content: View>(_ number: Int, _ title: String, @ViewBuilder content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text("\(number)")
                .font(.headline)
                .frame(width: 26, height: 26)
                .background(Color.accentColor.opacity(0.18), in: Circle())
            VStack(alignment: .leading, spacing: 8) {
                Text(title).font(.headline)
                content()
            }
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func share(_ file: URL?, label: String) -> some View {
        if let file {
            HStack {
                ShareLink(item: file) { Label(label, systemImage: "square.and.arrow.up") }
                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([file]) }
            }
        } else {
            Text("The certificate file is missing. Stop the server and run it again.")
                .foregroundStyle(.secondary)
        }
    }

    private func numbered(_ lines: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(lines.enumerated()), id: \.offset) { index, line in
                Text("\(index + 1). \(line)").foregroundStyle(.secondary)
            }
        }
    }
}

private extension View {
    func remoteCard() -> some View {
        padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
            .overlay {
                RoundedRectangle(cornerRadius: 14).strokeBorder(.primary.opacity(0.1))
            }
    }
}

private struct LauncherView: View {
    @ObservedObject var launcher: Launcher
    @State private var showCode = false
    @State private var copiedAddress: String?
    @State private var showGuide = false
    @State private var showSettings = false
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            if let notice = launcher.notice {
                Label(notice, systemImage: "exclamationmark.circle")
                    .fixedSize(horizontal: false, vertical: true)
                    .remoteCard()
            }
            switch launcher.phase {
            case .stopped:
                startCard
                sessionSettings
            case .starting, .stopping:
                HStack(spacing: 12) {
                    ProgressView().controlSize(.small)
                    Text(status).font(.headline)
                }
                .remoteCard()
            case .running:
                if launcher.inputReady == false {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Input access needed", systemImage: "hand.raised")
                            .font(.headline)
                        Text("Allow Accessibility access to use the mouse and keyboard.")
                            .foregroundStyle(.secondary)
                        Button("Open Accessibility Settings") { launcher.openAccessibility(request: true) }
                    }
                    .remoteCard()
                }
                connection.remoteCard()
                VStack(alignment: .leading, spacing: 10) { pairing }
                    .remoteCard()
            }
        }
        .padding(22)
        .frame(width: 560, alignment: .leading)
        .sheet(isPresented: $showGuide) { SetupGuide(launcher: launcher) }
        .onAppear { launcher.showWindow = { openWindow(id: "launcher") } }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            launcher.send("check_access")
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(nsImage: NSApplication.shared.applicationIconImage)
                .resizable()
                .scaledToFit()
                .frame(width: 48, height: 48)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 5) {
                Text("Lazy Remote").font(.system(size: 23, weight: .bold))
                HStack(spacing: 5) {
                    Circle().fill(launcher.phase == .running ? Color.green : Color.secondary)
                        .frame(width: 6, height: 6)
                        .accessibilityHidden(true)
                    Text(status).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                }
            }
            Spacer()
            Button { showGuide = true } label: {
                Label("Set up phone", systemImage: "iphone")
            }
            if launcher.phase == .running || launcher.phase == .starting {
                Button("Stop server", action: launcher.stop)
            }
        }
    }

    private var startCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Your phone is the remote", systemImage: "iphone.and.arrow.forward")
                .font(.title2.bold())
                .foregroundStyle(.white)
            Text("Use it as a trackpad, keyboard, and media controller on your Wi-Fi.")
                .foregroundStyle(Color.white.opacity(0.78))
                .fixedSize(horizontal: false, vertical: true)
            Button("Start server", action: launcher.start)
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .tint(Color(red: 0.29, green: 0.48, blue: 0.19))
                .keyboardShortcut(.defaultAction)
                .padding(.top, 3)
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(red: 0.13, green: 0.17, blue: 0.22), in: RoundedRectangle(cornerRadius: 16))
    }

    private var sessionSettings: some View {
        DisclosureGroup("Session settings", isExpanded: $showSettings) {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Toggle("Extend phone reconnect window", isOn: $launcher.allowBackgroundResume)
                    Text("Resume the same tab within one minute, or up to five minutes with this on.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Divider()
                VStack(alignment: .leading, spacing: 4) {
                    Toggle("Keep running when the phone is idle", isOn: $launcher.keepRunning)
                    Text("Off by default. The server stops after 30 minutes without phone activity.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.top, 10)
        }
        .remoteCard()
    }

    private var status: String {
        switch launcher.phase {
        case .stopped: "Server stopped"
        case .starting: "Starting server…"
        case .running: launcher.paired ? "Phone connected" : "Server running"
        case .stopping: "Stopping server…"
        }
    }

    private var connection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Phone address", systemImage: "link").font(.headline)
            HStack {
                if launcher.urls.count > 1 {
                    Picker("Phone address", selection: $launcher.address) {
                        ForEach(launcher.urls, id: \.self) { address in Text(address).tag(address) }
                    }
                    .labelsHidden()
                } else {
                    Text(launcher.address).font(.system(.body, design: .monospaced)).textSelection(.enabled)
                }
                Spacer()
                Button(copiedAddress == launcher.address ? "Copied" : "Copy link") {
                    launcher.copyAddress()
                    copiedAddress = launcher.address
                }
                .disabled(launcher.address.isEmpty)
            }
            if launcher.loopback {
                Text("Connect this Mac and your phone to the same private network, then restart the server.")
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var pairing: some View {
        if let match = launcher.match {
            VStack(alignment: .leading, spacing: 10) {
                Text(launcher.approved ? "Approved. Waiting for your phone…" : "Phone requests control")
                    .font(.headline)
                Text(match).font(.system(size: 32, weight: .semibold, design: .monospaced))
                    .padding(.horizontal, 14)
                    .padding(.vertical, 7)
                    .background(Color.green.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                    .textSelection(.enabled)
                if !launcher.approved {
                    Text("Approve only if this number matches the one on your phone.")
                        .foregroundStyle(.secondary)
                    HStack {
                        Button("Approve", action: { launcher.send("approve \(match)") })
                            .buttonStyle(.borderedProminent)
                        Button("Deny", action: { launcher.send("deny \(match)") })
                    }
                }
            }
        } else if launcher.paired {
            VStack(alignment: .leading, spacing: 8) {
                Label("Phone connected", systemImage: "checkmark.circle").font(.headline)
                HStack {
                    Button("Lock phone") { launcher.send("new_code") }
                    if launcher.remembered {
                        Button("Forget browser") { launcher.send("forget") }
                    }
                }
            }
        } else if let qr = launcher.qr, let code = launcher.code, !launcher.address.isEmpty {
            HStack(alignment: .center, spacing: 20) {
                PairingQR(payload: launcher.address + "#pair=" + qr)
                VStack(alignment: .leading, spacing: 9) {
                    Text("Scan to pair").font(.headline)
                    Text(launcher.remembered
                         ? "Open the same address to reconnect, or scan to pair at this address."
                         : "Tap Scan pairing QR in the phone app, or use your phone camera. Approve the request here.")
                        .foregroundStyle(.secondary)
                    Text("QR code expires in 10 minutes.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    DisclosureGroup("Show pairing code", isExpanded: $showCode) {
                        Text(code)
                            .font(.system(.body, design: .monospaced).weight(.semibold))
                            .textSelection(.enabled)
                            .padding(.top, 5)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            VStack(alignment: .leading, spacing: 8) {
                if launcher.remembered {
                    Text("Open the same address on your phone and tap Request access.")
                }
                Button("Show a new pairing QR code") { launcher.send("new_code") }
            }
        }
        if launcher.remembered && !launcher.paired && launcher.match == nil {
            Button("Forget browser") { launcher.send("forget") }
        }
    }
}

private final class LauncherDelegate: NSObject, NSApplicationDelegate {
    /// Closing the window quits only when the server is stopped. While it runs,
    /// Lazy Remote stays in the menu bar.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        !(Launcher.current?.isActive ?? false)
    }
}

private struct MenuBarMenu: View {
    @ObservedObject var launcher: Launcher
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text(launcher.isActive ? (launcher.paired ? "Phone connected" : "Server running") : "Server stopped")
        Button("Open Lazy Remote") {
            openWindow(id: "launcher")
            NSApp.activate(ignoringOtherApps: true)
        }
        if launcher.isActive {
            Button("Stop server", action: launcher.stop)
        }
        Divider()
        Button("Quit Lazy Remote") {
            launcher.stop()
            NSApp.terminate(nil)
        }
        .keyboardShortcut("q")
    }
}

@main
struct LazyRemoteApp: App {
    @NSApplicationDelegateAdaptor(LauncherDelegate.self) private var delegate
    @StateObject private var launcher = Launcher()

    var body: some Scene {
        Window("Lazy Remote", id: "launcher") {
            LauncherView(launcher: launcher)
        }
        .windowResizability(.contentSize)

        MenuBarExtra(isInserted: .constant(true)) {
            MenuBarMenu(launcher: launcher)
        } label: {
            Image(systemName: launcher.isActive ? "iphone.radiowaves.left.and.right" : "iphone")
                .accessibilityLabel("Lazy Remote")
        }
    }
}
