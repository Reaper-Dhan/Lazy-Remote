import CoreBluetooth
import Foundation
import Combine

let remoteBluetoothService = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000001")
let remoteBluetoothInput = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000002")
let remoteBluetoothOutput = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000003")

/// Stage an entire ATT transaction before changing the stream or forwarding input.
struct BluetoothWriteBuffer {
    struct Fragment { let offset: Int; let value: Data }
    struct Packet { let id: String; let data: Data }
    enum Failure: Error { case invalidOffset, tooLarge, invalidPacket, busy }
    private(set) var data = Data()

    mutating func reset() { data.removeAll() }

    mutating func accept(_ fragments: [Fragment]) throws -> Packet? {
        var staged = data
        var offset = 0
        for fragment in fragments {
            if fragment.offset == 0 { offset = 0 }
            guard fragment.offset == offset else { throw Failure.invalidOffset }
            guard staged.count + fragment.value.count <= 8192 else { throw Failure.tooLarge }
            staged.append(fragment.value)
            offset += fragment.value.count
        }
        var packet: Packet?
        while let end = staged.firstIndex(of: 10) {
            guard packet == nil else { throw Failure.busy }
            let message = staged.subdata(in: 0..<end)
            staged.removeSubrange(0...end)
            guard let body = try? JSONSerialization.jsonObject(with: message) as? [String: Any],
                  let id = body["id"] as? String,
                  id.range(of: "^[A-Za-z0-9-]{1,64}$", options: .regularExpression) != nil
            else { throw Failure.invalidPacket }
            packet = Packet(id: id, data: message)
        }
        data = staged
        return packet
    }
}

final class BluetoothHost: NSObject, ObservableObject, CBPeripheralManagerDelegate {
    @Published var status = "Bluetooth stopped"
    var receive: ((String) -> Void)?
    private var manager: CBPeripheralManager?
    private var central: CBCentral?
    private var output: CBMutableCharacteristic?
    private var incoming = BluetoothWriteBuffer()
    private var outgoing: [Data] = []
    private var enabled = false
    private var origins: [String: UUID] = [:]

    func start() {
        enabled = true
        if manager == nil { manager = CBPeripheralManager(delegate: self, queue: .main) }
        else { publish() }
    }

    func stop() {
        enabled = false
        manager?.stopAdvertising()
        manager?.removeAllServices()
        central = nil
        output = nil
        incoming.reset()
        outgoing.removeAll()
        origins.removeAll()
        status = "Bluetooth stopped"
    }

    func reply(_ encoded: String) {
        guard let data = Data(base64Encoded: encoded), data.count <= 16384,
              let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = body["id"] as? String,
              let origin = origins.removeValue(forKey: id),
              let central, central.identifier == origin else { return }
        let message = data + Data([10])
        let size = min(central.maximumUpdateValueLength, 512)
        guard size > 0, outgoing.count < 1024 else { stop(); return }
        for offset in stride(from: 0, to: message.count, by: size) {
            outgoing.append(message.subdata(in: offset..<min(offset + size, message.count)))
        }
        flush()
    }

    private func publish() {
        guard enabled, let manager, manager.state == .poweredOn else { return }
        central = nil
        incoming.reset()
        outgoing.removeAll()
        origins.removeAll()
        manager.removeAllServices()
        let service = CBMutableService(type: remoteBluetoothService, primary: true)
        let input = CBMutableCharacteristic(type: remoteBluetoothInput, properties: [.write],
                                            value: nil, permissions: [.writeEncryptionRequired])
        let output = CBMutableCharacteristic(type: remoteBluetoothOutput,
                                             properties: [.notify, .notifyEncryptionRequired],
                                             value: nil, permissions: [])
        self.output = output
        service.characteristics = [input, output]
        manager.add(service)
    }

    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        if peripheral.state != .poweredOn {
            central = nil
            incoming.reset()
            outgoing.removeAll()
            origins.removeAll()
        }
        switch peripheral.state {
        case .poweredOn: publish()
        case .unauthorized: status = "Allow Bluetooth access for Lazy Remote in System Settings."
        case .poweredOff: status = "Turn on Bluetooth on this Mac."
        default: status = "Bluetooth is unavailable."
        }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didAdd service: CBService, error: Error?) {
        guard enabled else { return }
        guard error == nil else { status = "Could not publish Bluetooth controls."; return }
        peripheral.startAdvertising([CBAdvertisementDataServiceUUIDsKey: [remoteBluetoothService],
                                      CBAdvertisementDataLocalNameKey: "Lazy Remote"])
    }

    func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
        status = error == nil ? "Open Lazy Remote on your iPhone and choose this Mac." : "Bluetooth advertising failed."
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral,
                           didSubscribeTo characteristic: CBCharacteristic) {
        guard self.central == nil || self.central?.identifier == central.identifier else { return }
        self.central = central
        status = "Bluetooth connected. Pair and approve your iPhone."
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral,
                           didUnsubscribeFrom characteristic: CBCharacteristic) {
        guard self.central?.identifier == central.identifier else { return }
        self.central = nil
        incoming.reset()
        outgoing.removeAll()
        origins.removeAll()
        status = "Waiting for the iPhone to reconnect."
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveWrite writes: [CBATTRequest]) {
        guard let first = writes.first else { return }
        guard let central, writes.allSatisfy({ $0.central.identifier == central.identifier }) else {
            peripheral.respond(to: first, withResult: .insufficientAuthorization)
            return
        }
        guard enabled, origins.isEmpty,
              writes.allSatisfy({ $0.characteristic.uuid == remoteBluetoothInput && $0.value != nil }) else {
            peripheral.respond(to: first, withResult: .unlikelyError)
            return
        }
        do {
            let fragments = writes.map { BluetoothWriteBuffer.Fragment(offset: $0.offset, value: $0.value!) }
            let packet = try incoming.accept(fragments)
            // Core Bluetooth requires one acknowledgement for the whole batch.
            peripheral.respond(to: first, withResult: .success)
            if let packet {
                origins[packet.id] = central.identifier
                receive?(packet.data.base64EncodedString())
            }
        } catch BluetoothWriteBuffer.Failure.invalidOffset {
            peripheral.respond(to: first, withResult: .invalidOffset)
        } catch BluetoothWriteBuffer.Failure.tooLarge {
            peripheral.respond(to: first, withResult: .invalidAttributeValueLength)
        } catch {
            peripheral.respond(to: first, withResult: .unlikelyError)
        }
    }

    func peripheralManagerIsReady(toUpdateSubscribers peripheral: CBPeripheralManager) { flush() }

    private func flush() {
        guard let manager, let output, let central else { return }
        while let packet = outgoing.first {
            guard manager.updateValue(packet, for: output, onSubscribedCentrals: [central]) else { return }
            outgoing.removeFirst()
        }
    }
}
