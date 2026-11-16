import CoreBluetooth
import Foundation
import Combine

let remoteBluetoothService = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000001")
let remoteBluetoothInput = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000002")
let remoteBluetoothOutput = CBUUID(string: "6C617A79-5265-4D6F-7465-000000000003")

final class BluetoothHost: NSObject, ObservableObject, CBPeripheralManagerDelegate {
    @Published var status = "Bluetooth stopped"
    var receive: ((String) -> Void)?
    private var manager: CBPeripheralManager?
    private var central: CBCentral?
    private var output: CBMutableCharacteristic?
    private var incoming = Data()
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
        incoming.removeAll()
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
        incoming.removeAll()
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
            incoming.removeAll()
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
        incoming.removeAll()
        outgoing.removeAll()
        origins.removeAll()
        status = "Waiting for the iPhone to reconnect."
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveWrite writes: [CBATTRequest]) {
        for request in writes {
            guard central?.identifier == request.central.identifier else {
                peripheral.respond(to: request, withResult: .insufficientAuthorization)
                continue
            }
            guard enabled, request.characteristic.uuid == remoteBluetoothInput,
                  request.offset == 0, let value = request.value,
                  incoming.count + value.count <= 8192, origins.isEmpty else {
                incoming.removeAll()
                peripheral.respond(to: request, withResult: .unlikelyError)
                continue
            }
            incoming.append(value)
            peripheral.respond(to: request, withResult: .success)
            while let end = incoming.firstIndex(of: 10) {
                let packet = incoming.subdata(in: 0..<end)
                incoming.removeSubrange(0...end)
                guard !packet.isEmpty, origins.isEmpty,
                      let body = try? JSONSerialization.jsonObject(with: packet) as? [String: Any],
                      let id = body["id"] as? String,
                      id.range(of: "^[A-Za-z0-9-]{1,64}$", options: .regularExpression) != nil,
                      origins[id] == nil else { continue }
                origins[id] = request.central.identifier
                receive?(packet.base64EncodedString())
            }
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
