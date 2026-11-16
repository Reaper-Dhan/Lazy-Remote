import Foundation
import CoreGraphics

/// Exercises the real native client's session transitions over a simulated link.
/// Run with Swift's DEBUG flag; production builds exclude the injection hooks.
@main
struct NativeSessionTests {
    @MainActor
    static func main() async {
        var requests: [[String: Any]] = []
        let remote = RemoteConnection()
        remote.testSend = { requests.append($0) }
        remote.connected = true

        func reply(_ status: Int, _ fields: [String: Any] = [:]) {
            let message = requests.last!
            remote.testReply(fields.merging(["id": message["id"]!, "status": status]) { _, new in new })
        }
        func operation() -> String { requests.last!["op"] as! String }
        func approve() {
            remote.pairingCode = "ABCD-EFGH-JKLM"
            remote.pair()
            precondition(operation() == "pair")
            reply(202, ["pending": String(repeating: "p", count: 43)])
            precondition(operation() == "status")
            reply(401, ["approved": true])
            precondition(operation() == "claim")
            reply(200, ["token": String(repeating: "t", count: 43), "tab": String(repeating: "s", count: 43)])
            precondition(operation() == "status")
            reply(200, ["ready": true])
            precondition(remote.ready)
        }

        approve()
        remote.testPoll()
        let previousToken = requests.last!["token"] as! String
        reply(503, ["error": "Connection lost"])
        precondition(!remote.ready)
        remote.testPoll()
        precondition(requests.last!["token"] as? String == previousToken,
                     "Transient failures must retain credentials for reconnection")
        reply(200, ["ready": true])

        // A command arriving during a status check waits; it must not disappear.
        remote.testPoll()
        let count = requests.count
        remote.command(["action": "click", "button": "right", "count": 1])
        precondition(requests.count == count)
        reply(200, ["ready": true])
        precondition(operation() == "cmd")
        precondition((requests.last!["body"] as? [String: Any])?["button"] as? String == "right")
        reply(200)

        remote.command(["action": "click", "button": "left", "count": 1])
        remote.command(["action": "type", "text": "must not replay after backgrounding"])
        remote.setActive(false)
        reply(200)
        let hiddenCount = requests.count
        remote.command(["action": "key", "name": "space"])
        precondition(requests.count == hiddenCount)
        remote.setActive(true)
        precondition(operation() == "status")
        precondition(requests.last!["token"] as? String == previousToken)
        reply(200, ["ready": true])
        precondition(operation() == "status", "Backgrounded commands must not replay on resume")

        // A status revocation is a session boundary, including input queued
        // behind that status check and the cumulative motion coordinates.
        remote.move(CGSize(width: 100, height: 0))
        remote.command(["action": "click", "button": "left", "count": 1])
        reply(200); reply(200)
        remote.testPoll()
        remote.command(["action": "type", "text": "must not cross session boundary"])
        reply(401)
        approve()
        precondition(operation() == "status", "An expired session's commands must not replay")
        remote.move(CGSize(width: 1, height: 0))
        remote.command(["action": "click", "button": "left", "count": 1])
        let motion = requests.last!["body"] as! [String: Any]
        precondition(motion["seq"] as? Int == 1)
        precondition(motion["dx"] as? CGFloat == 2, "A new session starts at zero motion")
        reply(200); reply(200)

        remote.lock()
        let originalLock = requests.last!
        reply(503, ["error": "Lock packet never reached host"])
        precondition(remote.locking)
        let failedCount = requests.count
        remote.testPoll()
        precondition(requests.count == failedCount, "A failed Lock must not silently resume status polling")
        remote.lock()
        precondition(requests.last!["token"] as? String == originalLock["token"] as? String)
        precondition(requests.last!["request_key"] as? String == originalLock["request_key"] as? String)
        reply(200) // A generic no-op acknowledgement is not confirmation.
        precondition(remote.locking && remote.status.contains("unconfirmed"))
        remote.lock()
        reply(200, ["locked": true])
        precondition(!remote.locking && remote.status == "Access locked")

        // Lock while a claim is in flight. Its late credential must be ignored,
        // and the original request key must cancel the host's unconfirmed claim.
        remote.pairingCode = "ABCD-EFGH-JKLM"
        remote.pair()
        reply(202)
        reply(401, ["approved": true])
        precondition(operation() == "claim")
        let originalKey = requests.last!["request_key"] as! String
        remote.lock()
        reply(200, ["token": "late-token", "tab": "late-tab"])
        precondition(operation() == "cancel")
        precondition(requests.last!["request_key"] as? String == originalKey)
        precondition(requests.last!["token"] == nil, "A locked client must ignore a late claim")
        reply(200, ["locked": true])
        remote.testPoll()
        precondition(requests.last!["request_key"] as? String != originalKey)
        precondition(requests.last!["token"] == nil)
        reply(401)
        precondition(!remote.ready && !remote.approved)

        let connecting = RemoteConnection()
        connecting.testBeginConnection(timeout: .milliseconds(20))
        precondition(connecting.connecting)
        try? await Task.sleep(for: .milliseconds(80))
        precondition(!connecting.connecting && connecting.status.contains("timed out"))
        connecting.testBeginConnection(timeout: .seconds(5))
        connecting.cancelConnection()
        precondition(!connecting.connecting && connecting.status.contains("cancelled"))
        try? await Task.sleep(for: .milliseconds(40))
        precondition(!connecting.connecting && connecting.status.contains("cancelled"))
        print("Native session boundaries, Lock retries, connection deadlines and cancellation passed.")
    }
}
