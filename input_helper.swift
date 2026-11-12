import Foundation
import CoreGraphics
import AppKit

// Disable stdout buffering
setbuf(stdout, nil)

var isDragging = false

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
    let eventType: CGEventType = isDragging ? .leftMouseDragged : .mouseMoved
    if let moveEvent = CGEvent(mouseEventSource: nil, mouseType: eventType, mouseCursorPosition: newPt, mouseButton: .left) {
        moveEvent.setIntegerValueField(.mouseEventDeltaX, value: Int64(dx))
        moveEvent.setIntegerValueField(.mouseEventDeltaY, value: Int64(dy))
        moveEvent.post(tap: .cghidEventTap)
    }
}

func clickMouse(button: String, count: Int = 1, continuation: Bool = false) {
    let pos = getMousePos()
    if button == "right" {
        let down = CGEvent(mouseEventSource: nil, mouseType: .rightMouseDown, mouseCursorPosition: pos, mouseButton: .right)
        let up = CGEvent(mouseEventSource: nil, mouseType: .rightMouseUp, mouseCursorPosition: pos, mouseButton: .right)
        down?.post(tap: .cghidEventTap)
        usleep(12000)
        up?.post(tap: .cghidEventTap)
    } else {
        for i in 1...count {
            let clickState = continuation ? 2 : Int64(i)
            let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: pos, mouseButton: .left)
            let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: pos, mouseButton: .left)
            down?.setIntegerValueField(.mouseEventClickState, value: clickState)
            up?.setIntegerValueField(.mouseEventClickState, value: clickState)
            down?.post(tap: .cghidEventTap)
            usleep(12000)
            up?.post(tap: .cghidEventTap)
            if i < count {
                usleep(25000)
            }
        }
    }
}

func setDrag(active: Bool) {
    guard active != isDragging else { return }
    let pos = getMousePos()
    isDragging = active
    if active {
        let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: pos, mouseButton: .left)
        down?.post(tap: .cghidEventTap)
    } else {
        let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: pos, mouseButton: .left)
        up?.post(tap: .cghidEventTap)
    }
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
    "next_video": (47, [.maskShift]),                // Shift + N (YouTube / Anime player next)
    "prev_video": (35, [.maskShift]),                // Shift + P (YouTube / Anime player prev)
    "skip_intro": (49, [.maskShift]),                // Shift + Right or custom
    "cmd_left": (123, [.maskCommand]),               // Browser Back
    "cmd_right": (124, [.maskCommand]),              // Browser Forward
    "zoom_in": (24, [.maskCommand]),                 // Cmd + +
    "zoom_out": (27, [.maskCommand]),                // Cmd + -
    "zoom_reset": (29, [.maskCommand])               // Cmd + 0
]

if CommandLine.arguments.contains("--request-access") {
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(options)
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
        print(AXIsProcessTrusted() ? "ok" : "permission")
        continue
    }
    guard AXIsProcessTrusted() else {
        isDragging = false
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
        clickMouse(button: button, count: count, continuation: json["continuation"] as? Bool == true)
    case "drag":
        let active = (json["active"] as? Bool) ?? false
        setDrag(active: active)
    case "scroll":
        let dx = Int32((json["dx"] as? NSNumber)?.intValue ?? 0)
        let dy = Int32((json["dy"] as? NSNumber)?.intValue ?? 0)
        scrollMouse(dx: dx, dy: dy)
    case "shortcut":
        if let code = json["key_code"] as? UInt16, let flags = json["flags"] as? UInt64 {
            sendKey(keyCode: code, flags: CGEventFlags(rawValue: flags))
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
