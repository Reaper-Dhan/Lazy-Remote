import Foundation
import CoreGraphics
import AppKit
import Carbon

// Disable stdout buffering
setbuf(stdout, nil)

let mouseSource = CGEventSource(stateID: .privateState)
var dragEventNumber: Int64?
var nextMouseEventNumber: Int64 = 1

func newMouseEventNumber() -> Int64 {
    defer { nextMouseEventNumber += 1 }
    return nextMouseEventNumber
}

func makeMouseEvent(
    type: CGEventType,
    button: CGMouseButton,
    position: CGPoint,
    number: Int64,
    clickState: Int64
) -> CGEvent? {
    guard let event = CGEvent(
        mouseEventSource: mouseSource, mouseType: type, mouseCursorPosition: position, mouseButton: button
    ) else { return nil }
    event.flags = []
    event.setIntegerValueField(.mouseEventButtonNumber, value: Int64(button.rawValue))
    event.setIntegerValueField(.mouseEventNumber, value: number)
    event.setIntegerValueField(.mouseEventClickState, value: clickState)
    return event
}

func getMousePos() -> CGPoint {
    if let event = CGEvent(source: nil) {
        return event.location
    }
    return .zero
}

func moveMouse(dx: Double, dy: Double) {
    let current = getMousePos()
    let newPt = CGPoint(x: current.x + CGFloat(dx), y: current.y + CGFloat(dy))
    
    // 1. Warp hardware cursor position directly
    CGWarpMouseCursorPosition(newPt)
    
    // 2. Post mouse moved / dragged event with delta values
    let eventType: CGEventType = dragEventNumber == nil ? .mouseMoved : .leftMouseDragged
    if let moveEvent = makeMouseEvent(
        type: eventType, button: .left, position: newPt,
        number: dragEventNumber ?? 0, clickState: dragEventNumber == nil ? 0 : 1
    ) {
        moveEvent.setIntegerValueField(.mouseEventDeltaX, value: Int64(dx))
        moveEvent.setIntegerValueField(.mouseEventDeltaY, value: Int64(dy))
        moveEvent.post(tap: .cghidEventTap)
    }
}

func clickMouse(button: String, count: Int = 1, continuation: Bool = false) -> Bool {
    let pos = getMousePos()
    let secondary = button == "right"
    let mouseButton: CGMouseButton = secondary ? .right : .left
    for i in 1...count {
        let clickState: Int64 = continuation ? 2 : Int64(i)
        let number = newMouseEventNumber()
        guard let down = makeMouseEvent(
            type: secondary ? .rightMouseDown : .leftMouseDown,
            button: mouseButton, position: pos, number: number, clickState: clickState
        ), let up = makeMouseEvent(
            type: secondary ? .rightMouseUp : .leftMouseUp,
            button: mouseButton, position: pos, number: number, clickState: clickState
        ) else { return false }
        down.post(tap: .cghidEventTap)
        usleep(12000)
        up.post(tap: .cghidEventTap)
        if i < count { usleep(25000) }
    }
    return true
}

@discardableResult
func setDrag(active: Bool) -> Bool {
    guard active != (dragEventNumber != nil) else { return true }
    let number = dragEventNumber ?? newMouseEventNumber()
    guard let event = makeMouseEvent(
        type: active ? .leftMouseDown : .leftMouseUp, button: .left,
        position: getMousePos(), number: number, clickState: 1
    ) else { return false }
    event.post(tap: .cghidEventTap)
    dragEventNumber = active ? number : nil
    return true
}

func scrollMouse(dx: Int32, dy: Int32) {
    if let scrollEvent = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0) {
        scrollEvent.post(tap: .cghidEventTap)
    }
}

func sendKey(keyCode: CGKeyCode, flags: CGEventFlags = []) {
    let source = CGEventSource(stateID: .hidSystemState)
    let down = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: true)
    let up = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: false)
    down?.flags = flags
    up?.flags = flags
    down?.post(tap: .cghidEventTap)
    usleep(10000)
    up?.post(tap: .cghidEventTap)
}

func shortcutEvents(keyCode: CGKeyCode, flags: CGEventFlags, held: CGEventFlags) -> [CGEvent]? {
    let source = CGEventSource(stateID: .privateState)
    let modifiers: [(CGEventFlags, CGKeyCode, UInt64)] = [
        (.maskControl, CGKeyCode(kVK_Control), 0x01),
        (.maskShift, CGKeyCode(kVK_Shift), 0x02),
        (.maskAlternate, CGKeyCode(kVK_Option), 0x20),
        (.maskCommand, CGKeyCode(kVK_Command), 0x08),
        (.maskSecondaryFn, CGKeyCode(kVK_Function), 0),
    ]
    let pressed = modifiers.filter { flags.contains($0.0) && !held.contains($0.0) }
    var events: [CGEvent] = []
    var current = held

    func append(_ code: CGKeyCode, _ type: CGEventType, _ flags: CGEventFlags) -> Bool {
        guard let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: type != .keyUp)
        else { return false }
        event.type = type
        event.flags = flags
        events.append(event)
        return true
    }

    for (mask, code, side) in pressed {
        current.formUnion([mask, CGEventFlags(rawValue: side)])
        guard append(code, .flagsChanged, current) else { return nil }
    }
    let sideFlags = pressed.reduce(UInt64(0)) { $0 | $1.2 }
    let chord = flags.union(CGEventFlags(rawValue: sideFlags))
    guard append(keyCode, .keyDown, chord), append(keyCode, .keyUp, chord) else { return nil }
    for (mask, code, side) in pressed.reversed() {
        current.subtract([mask, CGEventFlags(rawValue: side)])
        guard append(code, .flagsChanged, current) else { return nil }
    }
    return events
}

func sendMacShortcut(name: String, keyCode: CGKeyCode, flags: CGEventFlags) -> Bool {
    // The default Mission Control action has a public system app entry point.
    // A customized keyboard binding still goes through the user's key chord.
    if name == "swipe_up", keyCode == CGKeyCode(kVK_UpArrow), flags == .maskControl {
        let missionControl = URL(fileURLWithPath: "/System/Applications/Mission Control.app", isDirectory: true)
        if FileManager.default.fileExists(atPath: missionControl.path), NSWorkspace.shared.open(missionControl) {
            return true
        }
    }
    let held = CGEventSource.flagsState(.hidSystemState)
    guard let events = shortcutEvents(keyCode: keyCode, flags: flags, held: held) else { return false }
    for event in events {
        event.post(tap: .cghidEventTap)
        usleep(8000)
    }
    return true
}

func typeUnicodeString(_ text: String) {
    let source = CGEventSource(stateID: .hidSystemState)
    for char in text {
        let str = String(char)
        if str == "\n" || str == "\r" {
            sendKey(keyCode: 36)
            continue
        }
        var utf16 = Array(str.utf16)
        if let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
           let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) {
            down.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: &utf16)
            up.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: &utf16)
            down.post(tap: .cghidEventTap)
            usleep(8000)
            up.post(tap: .cghidEventTap)
            usleep(8000)
        }
    }
}

// Media / System key helper using NSEvent (for native macOS volume/brightness/media keys)
func sendMediaKey(keyType: Int32) {
    func doKey(down: Bool) {
        let flags = NSEvent.ModifierFlags(rawValue: (down ? 0xa00 : 0xb00))
        let data1 = Int((keyType << 16) | (down ? 0xa00 : 0xb00))
        if let ev = NSEvent.otherEvent(
            with: .systemDefined,
            location: .zero,
            modifierFlags: flags,
            timestamp: 0,
            windowNumber: 0,
            context: nil,
            subtype: 8,
            data1: data1,
            data2: -1
        ) {
            ev.cgEvent?.post(tap: .cghidEventTap)
        }
    }
    doKey(down: true)
    usleep(10000)
    doKey(down: false)
}

let keyMap: [String: (CGKeyCode, CGEventFlags)] = [
    "space": (49, []),
    "enter": (36, []),
    "backspace": (51, []),
    "escape": (53, []),
    "tab": (48, []),
    "up": (126, []),
    "down": (125, []),
    "left": (123, []),
    "right": (124, []),
    "f": (3, []),
    "m": (46, []),
    "c": (8, []),
    "k": (40, []),
    "j": (38, []),
    "l": (37, []),
    "close_tab": (13, [.maskCommand]),               // Cmd + W (Close ad tab!)
    "reopen_tab": (17, [.maskCommand, .maskShift]),  // Cmd + Shift + T (Oops, reopen closed tab)
    "next_tab": (48, [.maskControl]),                // Ctrl + Tab (Next browser tab)
    "prev_tab": (48, [.maskControl, .maskShift]),    // Ctrl + Shift + Tab (Previous browser tab)
    "new_tab": (17, [.maskCommand]),                 // Cmd + T
    "refresh": (15, [.maskCommand]),                 // Cmd + R
    "fullscreen_mac": (3, [.maskCommand, .maskControl]), // Ctrl + Cmd + F
    "next_video": (45, [.maskShift]),                // Shift + N (YouTube / Anime player next)
    "prev_video": (35, [.maskShift]),                // Shift + P (YouTube / Anime player prev)
    "skip_intro": (49, [.maskShift]),                // Shift + Right or custom
    "cmd_left": (123, [.maskCommand]),               // Browser Back
    "cmd_right": (124, [.maskCommand]),              // Browser Forward
    "zoom_in": (24, [.maskCommand]),                 // Cmd + +
    "zoom_out": (27, [.maskCommand]),                // Cmd + -
    "zoom_reset": (29, [.maskCommand])               // Cmd + 0
]

if CommandLine.arguments.contains("--request-access") {
    _ = CGRequestPostEventAccess()
}

while let line = readLine() {
    let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty { continue }
    
    guard let data = trimmed.data(using: .utf8),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let action = json["action"] as? String else {
        print("invalid")
        continue
    }

    if action == "check_perm" {
        print(CGPreflightPostEventAccess() ? "ok" : "permission")
        continue
    }
    if action == "request_perm" {
        print(CGRequestPostEventAccess() ? "ok" : "permission")
        continue
    }
    guard CGPreflightPostEventAccess() else {
        dragEventNumber = nil
        print("permission")
        continue
    }

    switch action {
    case "move":
        let dx = (json["dx"] as? NSNumber)?.doubleValue ?? 0.0
        let dy = (json["dy"] as? NSNumber)?.doubleValue ?? 0.0
        moveMouse(dx: dx, dy: dy)
    case "click":
        let button = (json["button"] as? String) ?? "left"
        let count = (json["count"] as? NSNumber)?.intValue ?? 1
        guard clickMouse(button: button, count: count, continuation: json["continuation"] as? Bool == true) else {
            print("invalid")
            continue
        }
    case "drag":
        let active = (json["active"] as? Bool) ?? false
        guard setDrag(active: active) else {
            print("invalid")
            continue
        }
    case "scroll":
        let dx = Int32((json["dx"] as? NSNumber)?.intValue ?? 0)
        let dy = Int32((json["dy"] as? NSNumber)?.intValue ?? 0)
        scrollMouse(dx: dx, dy: dy)
    case "shortcut":
        guard let name = json["name"] as? String,
              let code = json["key_code"] as? UInt16, let flags = json["flags"] as? UInt64,
              sendMacShortcut(name: name, keyCode: code, flags: CGEventFlags(rawValue: flags)) else {
            print("invalid")
            continue
        }
    case "key":
        if let name = json["name"] as? String {
            if name == "vol_up" {
                sendMediaKey(keyType: 0) // NX_KEYTYPE_SOUND_UP
            } else if name == "vol_down" {
                sendMediaKey(keyType: 1) // NX_KEYTYPE_SOUND_DOWN
            } else if name == "vol_mute" {
                sendMediaKey(keyType: 7) // NX_KEYTYPE_MUTE
            } else if name == "media_play" {
                sendMediaKey(keyType: 16) // NX_KEYTYPE_PLAY
            } else if name == "bright_up" {
                sendMediaKey(keyType: 2) // NX_KEYTYPE_BRIGHTNESS_UP
            } else if name == "bright_down" {
                sendMediaKey(keyType: 3) // NX_KEYTYPE_BRIGHTNESS_DOWN
            } else if let (code, flags) = keyMap[name] {
                sendKey(keyCode: code, flags: flags)
            }
        }
    case "type":
        if let text = json["text"] as? String {
            typeUnicodeString(text)
        }
    default:
        print("invalid")
        continue
    }
    print("ok")
}
setDrag(active: false)
