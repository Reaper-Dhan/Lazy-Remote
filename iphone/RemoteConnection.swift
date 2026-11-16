import CoreBluetooth
import CoreGraphics
import Foundation
import Combine

@MainActor
final class RemoteConnection: NSObject, ObservableObject, @preconcurrency CBCentralManagerDelegate, @preconcurrency CBPeripheralDelegate {
    struct Device: Identifiable {
        let peripheral: CBPeripheral
        var id: UUID { peripheral.identifier }
        var name: String { peripheral.name ?? "Lazy Remote host" }
    }
    @Published var devices: [Device] = []
    @Published var status = "Tap Find hosts to start."
    @Published var connected = false
    @Published private(set) var connecting = false
    @Published var ready = false
    @Published var match = ""
    @Published var error = ""
    @Published var pairingCode = ""

    private let serviceID = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000001")
    private let inputID = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000002")
    private let outputID = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000003")
    private var manager: CBCentralManager?
    private var peripheral: CBPeripheral?
    private var input: CBCharacteristic?
    private var buffer = Data()
    private var frames: [Data] = []
    private var awaiting: (id: String, generation: Int, completion: ([String: Any]) -> Void)?
    private var timeout: Task<Void, Never>?
    private var poll: Task<Void, Never>?
    private var connectionTimeout: Task<Void, Never>?
    private var connectionAttempt: UUID?
    private var autoReconnect = true
    private var requestKey = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    private var token: String?
    private var tab: String?
    private var pending: String?
    private var active = true
    private var delta = CGSize.zero
    private var movement: Task<Void, Never>?
    private var cumulative = CGSize.zero
    private var sequence = 0
    private var drag = false
    private var generation = 0
    private var queued: [[String: Any]] = []
    private var pendingLock: (key: String, token: String?, tab: String?)?
    private var lockNeedsSend = false
    @Published private(set) var locking = false
    @Published private(set) var approved = false
    @Published private(set) var swipes: [String] = []
    @Published private(set) var unavailable: [String] = []

    #if DEBUG
    // Test the session state machine without a physical Core Bluetooth link.
    var testSend: (([String: Any]) -> Void)?
    func testReply(_ response: [String: Any]) { acceptResponse(response) }
    func testPoll() { check() }
    func testBeginConnection(timeout: Duration) { armConnectionDeadline(timeout) }
    #endif

    func scan() {
        guard !connecting else { return }
        devices = []
        error = ""
        if manager == nil { manager = CBCentralManager(delegate: self, queue: .main) }
        else { beginScan() }
    }

    private func beginScan() {
        guard manager?.state == .poweredOn else { status = "Turn on Bluetooth and allow access."; return }
        manager?.scanForPeripherals(withServices: [serviceID])
        status = "Looking for Lazy Remote hosts…"
    }

    func connect(_ device: Device) {
        guard !connecting else { return }
        guard pendingLock == nil || peripheral?.identifier == device.id else {
            error = "Confirm Lock on the previous host before choosing another host."
            return
        }
        manager?.stopScan()
        if let previous = peripheral { manager?.cancelPeripheralConnection(previous) }
        timeout?.cancel()
        awaiting = nil
        frames = []
        buffer.removeAll()
        connected = false
        input = nil
        if pendingLock == nil { clearSession() }
        peripheral = device.peripheral
        peripheral?.delegate = self
        autoReconnect = true
        beginConnection(device.peripheral)
    }

    private func armConnectionDeadline(_ duration: Duration = .seconds(30)) {
        connectionTimeout?.cancel()
        let attempt = UUID()
        connectionAttempt = attempt
        connecting = true
        status = "Connecting…"
        connectionTimeout = Task { [weak self] in
            try? await Task.sleep(for: duration)
            guard !Task.isCancelled, let self, self.connectionAttempt == attempt else { return }
            self.stopConnection("Connection timed out. Choose a host and try again.")
        }
    }

    private func finishConnectionAttempt() {
        connectionTimeout?.cancel()
        connectionTimeout = nil
        connectionAttempt = nil
        connecting = false
    }

    private func beginConnection(_ peer: CBPeripheral) {
        guard !connecting, manager?.state == .poweredOn else { return }
        armConnectionDeadline()
        manager?.connect(peer)
    }

    func cancelConnection() { stopConnection("Connection cancelled. Choose a host to reconnect.") }

    private func stopConnection(_ message: String) {
        autoReconnect = false
        finishConnectionAttempt()
        connected = false
        ready = false
        poll?.cancel()
        input = nil
        buffer.removeAll()
        failRequest(message)
        if let peripheral { manager?.cancelPeripheralConnection(peripheral) }
        status = locking ? message + " Lock is still unconfirmed; use Lock phone on the host." : message
    }

    func setActive(_ value: Bool) {
        active = value
        ready = false
        delta = .zero
        queued.removeAll()
        if value {
            if connected { check() }
            else if autoReconnect, let peripheral { beginConnection(peripheral) }
        } else {
            manager?.stopScan()
            if drag { command(["action": "drag", "active": false], allowHidden: true) }
        }
    }

    func pair(qr: String? = nil) {
        guard pendingLock == nil else { error = "Confirm Lock before requesting access again."; return }
        var body: [String: Any] = ["remember": false]
        if let qr { body["qr"] = qr }
        else { body["code"] = pairingCode }
        request("pair", body: body) { [weak self] response in
            guard let self else { return }
            if response["status"] as? Int == 202 {
                self.pending = response["pending"] as? String
                self.pairingCode = ""
                self.check()
            } else { self.error = response["error"] as? String ?? "Pairing failed. Try again." }
        }
    }

    func lock() {
        if pendingLock == nil {
            pendingLock = (requestKey, token, tab)
            clearSession()
        }
        locking = true
        lockNeedsSend = true
        status = "Locking access…"
        flushCommands()
    }

    private func clearSession() {
        generation += 1
        approved = false
        swipes = []
        unavailable = []
        resetControls()
        token = nil
        tab = nil
        pending = nil
        match = ""
        ready = false
        requestKey = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
            + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }

    private func resetControls() {
        queued.removeAll()
        movement?.cancel()
        movement = nil
        drag = false
        cumulative = .zero
        sequence = 0
        delta = .zero
    }

    func command(_ body: [String: Any], allowHidden: Bool = false) {
        guard (active && ready) || (allowHidden && token != nil) else { return }
        guard queued.count < 15 else { error = "Too many controls queued. Wait a moment."; return }
        if delta != .zero { queued.append(takeMotion()) }
        queued.append(body)
        flushCommands()
    }

    private func takeMotion() -> [String: Any] {
        cumulative.width += delta.width
        cumulative.height += delta.height
        delta = .zero
        sequence += 1
        return ["action": "motion", "seq": sequence, "dx": cumulative.width, "dy": cumulative.height]
    }

    private func flushCommands() {
        guard connected, awaiting == nil else { return }
        if let lock = pendingLock {
            guard lockNeedsSend else { return }
            lockNeedsSend = false
            request("cancel", credentials: (lock.token, lock.tab), key: lock.key) { [weak self] response in
                guard let self else { return }
                if response["status"] as? Int == 200 && response["locked"] as? Bool == true {
                    self.pendingLock = nil
                    self.lockNeedsSend = false
                    self.locking = false
                    self.error = ""
                    self.status = "Access locked"
                } else {
                    self.status = "Lock unconfirmed. Retry Lock or use Lock phone on the host."
                }
            }
            return
        }
        guard active, ready else { return }
        let body: [String: Any]
        if !queued.isEmpty { body = queued.removeFirst() }
        else if delta != .zero { body = takeMotion() }
        else { return }
        request("cmd", body: body) { [weak self] response in
            guard let self else { return }
            if response["status"] as? Int == 401 {
                self.clearSession()
                self.status = "Request approval again."
            } else if response["status"] as? Int != 200 {
                self.error = response["error"] as? String ?? "Command could not be confirmed."
            }
            self.drag = response["dragging"] as? Bool ?? false
        }
    }

    func move(_ change: CGSize) {
        guard active && ready else { return }
        delta.width += change.width * 2
        delta.height += change.height * 2
        if movement == nil {
            movement = Task { [weak self] in
                try? await Task.sleep(for: .milliseconds(40))
                guard !Task.isCancelled, let self else { return }
                self.movement = nil
                self.flushCommands()
            }
        }
    }

    private func check() {
        guard active, connected, awaiting == nil, pendingLock == nil else { return }
        request("status") { [weak self] response in
            guard let self else { return }
            if response["status"] as? Int == 200 {
                self.ready = response["ready"] as? Bool == true && self.active
                self.approved = true
                self.swipes = (response["gestures"] as? [String: Any])?["swipes"] as? [String] ?? []
                self.unavailable = response["unavailable"] as? [String] ?? []
                self.match = ""
                self.status = self.ready ? "Connected by Bluetooth" : "Allow input access on the host."
                self.error = ""
            } else if response["status"] as? Int != 401 {
                // A lost status reply is not a revocation. Keep this session's
                // credentials so reconnect can resume within the host timeout.
                self.ready = false
                self.status = "Host unavailable. Reconnecting…"
                self.error = response["error"] as? String ?? "Could not confirm host status."
            } else if response["approved"] as? Bool == true {
                self.resetControls()
                self.token = nil
                self.tab = nil
                self.request("claim") { [weak self] result in
                    guard let self, result["status"] as? Int == 200 else { return }
                    self.token = result["token"] as? String
                    self.tab = result["tab"] as? String
                    self.check()
                }
            } else {
                self.resetControls()
                self.ready = false
                self.approved = false
                self.token = nil
                self.tab = nil
                self.match = response["match"] as? String ?? ""
                self.status = self.match.isEmpty ? "Enter the host’s pairing code or scan its QR."
                    : "Approve the matching number on the host."
            }
        }
    }

    private func request(_ op: String, body: [String: Any]? = nil,
                         credentials: (String?, String?)? = nil, key: String? = nil,
                         completion: @escaping ([String: Any]) -> Void) {
        guard connected, awaiting == nil else { return }
        let id = UUID().uuidString
        var message: [String: Any] = ["id": id, "op": op, "request_key": key ?? requestKey]
        if let value = credentials?.0 ?? token { message["token"] = value }
        if let value = credentials?.1 ?? tab { message["tab"] = value }
        if let pending { message["pending"] = pending }
        if let body { message["body"] = body }
        #if DEBUG
        if let testSend {
            awaiting = (id, generation, completion)
            testSend(message)
            return
        }
        #endif
        guard let peripheral, let input else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: message), data.count < 8192 else { return }
        let packet = data + Data([10])
        let size = min(peripheral.maximumWriteValueLength(for: .withResponse), 512)
        guard size > 0 else { return }
        frames = stride(from: 0, to: packet.count, by: size).map {
            packet.subdata(in: $0..<min($0 + size, packet.count))
        }
        awaiting = (id, generation, completion)
        peripheral.writeValue(frames.removeFirst(), for: input, type: .withResponse)
        timeout?.cancel()
        timeout = Task { [weak self] in
            try? await Task.sleep(for: .seconds(8))
            guard !Task.isCancelled, let self, self.awaiting?.id == id else { return }
            self.failRequest("Bluetooth response timed out. Reconnecting…")
            if let peripheral = self.peripheral { self.manager?.cancelPeripheralConnection(peripheral) }
        }
    }

    private func failRequest(_ message: String) {
        let callback = awaiting?.generation == generation ? awaiting?.completion : nil
        awaiting = nil
        timeout?.cancel()
        frames = []
        queued.removeAll()
        delta = .zero
        error = message
        callback?(["status": 503, "error": message])
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            if active, autoReconnect, let peripheral { beginConnection(peripheral) }
            else if active { beginScan() }
        }
        else {
            connected = false
            finishConnectionAttempt()
            poll?.cancel()
            ready = false
            input = nil
            buffer.removeAll()
            failRequest("Bluetooth unavailable.")
            queued.removeAll()
            delta = .zero
            status = central.state == .unauthorized ? "Allow Bluetooth access in iPhone Settings." : "Turn on Bluetooth."
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        if !devices.contains(where: { $0.id == peripheral.identifier }) {
            devices.append(Device(peripheral: peripheral))
        }
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard self.peripheral?.identifier == peripheral.identifier else { return }
        guard autoReconnect else { central.cancelPeripheralConnection(peripheral); return }
        status = "Securing Bluetooth connection…"
        peripheral.discoverServices([serviceID])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        guard self.peripheral?.identifier == peripheral.identifier else { return }
        finishConnectionAttempt()
        autoReconnect = false
        status = "Could not connect. Find hosts and try again."
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        guard self.peripheral?.identifier == peripheral.identifier else { return }
        connected = false
        finishConnectionAttempt()
        poll?.cancel()
        ready = false
        input = nil
        buffer.removeAll()
        failRequest("Bluetooth disconnected.")
        if active && autoReconnect && central.state == .poweredOn {
            status = "Reconnecting…"
            beginConnection(peripheral)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard self.peripheral?.identifier == peripheral.identifier, autoReconnect else { return }
        guard error == nil, let service = peripheral.services?.first(where: { $0.uuid == serviceID }) else {
            stopConnection("This host does not expose Lazy Remote controls.")
            return
        }
        peripheral.discoverCharacteristics([inputID, outputID], for: service)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard self.peripheral?.identifier == peripheral.identifier, autoReconnect else { return }
        guard error == nil else {
            stopConnection("Could not discover Bluetooth controls. Choose a host and try again.")
            return
        }
        input = service.characteristics?.first(where: { $0.uuid == inputID })
        if let output = service.characteristics?.first(where: { $0.uuid == outputID }), input != nil {
            peripheral.setNotifyValue(true, for: output)
        } else { stopConnection("This host is missing Bluetooth controls.") }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard self.peripheral?.identifier == peripheral.identifier, autoReconnect else { return }
        guard error == nil, characteristic.isNotifying, input != nil else {
            stopConnection("Bluetooth pairing was declined or unavailable. Choose a host to retry.")
            return
        }
        connected = true
        finishConnectionAttempt()
        if pendingLock != nil { lockNeedsSend = true }
        flushCommands()
        check()
        poll?.cancel()
        poll = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled else { return }
                self?.check()
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        guard self.peripheral?.identifier == peripheral.identifier, autoReconnect else { return }
        guard error == nil else {
            failRequest("Bluetooth write failed.")
            manager?.cancelPeripheralConnection(peripheral)
            return
        }
        if !frames.isEmpty, let input {
            peripheral.writeValue(frames.removeFirst(), for: input, type: .withResponse)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard self.peripheral?.identifier == peripheral.identifier, autoReconnect else { return }
        guard error == nil, let data = characteristic.value, buffer.count + data.count <= 32768 else {
            buffer.removeAll(); failRequest("Invalid Bluetooth response."); return
        }
        buffer.append(data)
        while let end = buffer.firstIndex(of: 10) {
            let packet = buffer.subdata(in: 0..<end)
            buffer.removeSubrange(0...end)
            guard let response = try? JSONSerialization.jsonObject(with: packet) as? [String: Any] else { continue }
            acceptResponse(response)
        }
    }

    private func acceptResponse(_ response: [String: Any]) {
        guard let id = response["id"] as? String, id == awaiting?.id else { return }
        let callback = awaiting?.generation == generation ? awaiting?.completion : nil
        awaiting = nil
        timeout?.cancel()
        callback?(response)
        flushCommands()
    }
}
