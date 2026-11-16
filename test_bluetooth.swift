import Foundation

@main
struct BluetoothBatchTests {
    static func main() throws {
        let message = Data("{\"id\":\"batch-1\",\"body\":{\"text\":\"hello\"}}\n".utf8)
        var stream = BluetoothWriteBuffer()
        let partial = message.prefix(10)
        let incomplete = try stream.accept([.init(offset: 0, value: partial)])
        precondition(incomplete == nil)
        let saved = stream.data
        do {
            _ = try stream.accept([.init(offset: 0, value: message.dropFirst(10)),
                                   .init(offset: 99, value: Data([1]))])
            preconditionFailure("A bad tail must reject the entire transaction")
        } catch BluetoothWriteBuffer.Failure.invalidOffset {}
        precondition(stream.data == saved, "Failed batches must not consume or forward earlier fragments")
        let packet = try stream.accept([.init(offset: 0, value: message.dropFirst(10))])
        precondition(packet?.id == "batch-1" && packet?.data == message.dropLast())
        precondition(stream.data.isEmpty)

        // Long ATT writes use offsets within one value, not offsets within the
        // application stream. They can arrive together in a single callback.
        let longWrite = try stream.accept([
            .init(offset: 0, value: message.prefix(15)),
            .init(offset: 15, value: message.dropFirst(15)),
        ])
        precondition(longWrite?.id == "batch-1")

        for invalid in [message + message, Data(repeating: 32, count: 8193), Data("{\"id\":\"bad id\"}\n".utf8)] {
            do {
                _ = try stream.accept([.init(offset: 0, value: invalid)])
                preconditionFailure("Invalid or pipelined requests must be rejected atomically")
            } catch {}
            precondition(stream.data.isEmpty)
        }
        print("Bluetooth fragmented writes, atomic rejection, offsets and limits passed.")
    }
}
