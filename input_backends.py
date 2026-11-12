"""Mouse and keyboard output for Windows and Linux hosts.

Each backend accepts the same validated commands as the macOS input helper
(input_helper.swift) and returns True when the input was sent, or False when
the operating system has not granted input access yet.
"""
import json
import os
import struct
import sys
import time


# Remote key names -> (modifiers, key). The Mac's Command shortcuts become Ctrl,
# and its browser Back/Forward (Cmd+arrow) become Alt+arrow.
KEYMAP = {
    "space": ((), "space"), "enter": ((), "enter"), "backspace": ((), "backspace"),
    "escape": ((), "escape"), "tab": ((), "tab"),
    "up": ((), "up"), "down": ((), "down"), "left": ((), "left"), "right": ((), "right"),
    "f": ((), "f"), "m": ((), "m"), "c": ((), "c"), "k": ((), "k"), "j": ((), "j"), "l": ((), "l"),
    "close_tab": (("ctrl",), "w"),
    "reopen_tab": (("ctrl", "shift"), "t"),
    "next_tab": (("ctrl",), "tab"),
    "prev_tab": (("ctrl", "shift"), "tab"),
    "new_tab": (("ctrl",), "t"),
    "refresh": (("ctrl",), "r"),
    "fullscreen_mac": ((), "f11"),
    "next_video": (("shift",), "n"),
    "prev_video": (("shift",), "p"),
    "skip_intro": (("shift",), "space"),
    "cmd_left": (("alt",), "left"),
    "cmd_right": (("alt",), "right"),
    "zoom_in": (("ctrl",), "equal"),
    "zoom_out": (("ctrl",), "minus"),
    "zoom_reset": (("ctrl",), "0"),
    "vol_up": ((), "vol_up"), "vol_down": ((), "vol_down"), "vol_mute": ((), "vol_mute"),
    "media_play": ((), "media_play"), "bright_up": ((), "bright_up"), "bright_down": ((), "bright_down"),
}
# Phone scroll deltas are in pixels. One wheel notch (120 units) scrolls about 40 pixels.
WHEEL_UNITS_PER_PIXEL = 3


class InputBackend:
    """Turns remote commands into primitive events. Subclasses send the primitives."""

    supported_keys = None  # None means every key in KEYMAP.

    def __init__(self):
        self.dragging = False
        self.remainder_x = self.remainder_y = 0.0

    # Primitives implemented by each operating system.
    def ready(self):
        return True

    def move_by(self, dx, dy):
        raise NotImplementedError

    def button(self, name, down):
        raise NotImplementedError

    def wheel(self, vertical, horizontal):
        """Scroll by wheel units (120 = one notch): positive vertical is up, positive horizontal is right."""
        raise NotImplementedError

    def key(self, name, down):
        raise NotImplementedError

    def text(self, value):
        raise NotImplementedError

    def close(self):
        if self.dragging:
            self.button("left", False)
            self.dragging = False

    def unavailable_keys(self):
        if self.supported_keys is None:
            return set()
        return {name for name, (_, key) in KEYMAP.items() if key not in self.supported_keys}

    # Shared translation.
    def press(self, modifiers, name):
        if self.supported_keys is not None and name not in self.supported_keys:
            return
        for modifier in modifiers:
            self.key(modifier, True)
        self.key(name, True)
        time.sleep(0.01)
        self.key(name, False)
        for modifier in reversed(modifiers):
            self.key(modifier, False)

    def click(self, name, count):
        for index in range(count):
            self.button(name, True)
            time.sleep(0.012)
            self.button(name, False)
            if index + 1 < count:
                time.sleep(0.025)

    def send(self, command):
        action = command["action"]
        if action == "check_perm":
            return self.ready()
        if action == "request_perm":
            return self.ready()
        if not self.ready():
            self.dragging = False
            return False
        if action == "move":
            x = command["dx"] + self.remainder_x
            y = command["dy"] + self.remainder_y
            step_x, step_y = int(x), int(y)
            self.remainder_x, self.remainder_y = x - step_x, y - step_y
            if step_x or step_y:
                self.move_by(step_x, step_y)
        elif action == "click":
            self.click(command["button"], command["count"])
        elif action == "drag":
            if command["active"] != self.dragging:
                self.button("left", command["active"])
                self.dragging = command["active"]
        elif action == "scroll":
            # Same meaning as macOS: positive dy scrolls up, positive dx scrolls left.
            # wheel() takes (up, right), so dx is flipped here once.
            self.wheel(command["dy"] * WHEEL_UNITS_PER_PIXEL, -command["dx"] * WHEEL_UNITS_PER_PIXEL)
        elif action == "key":
            modifiers, name = KEYMAP[command["name"]]
            self.press(modifiers, name)
        elif action == "type":
            self.text(command["text"])
        else:
            # "shortcut" carries macOS key codes and is only produced on a Mac.
            raise ValueError(f"Unsupported command: {action}")
        return True


class WindowsInput(InputBackend):
    """SendInput-based output. Windows blocks input into apps running as administrator."""

    VK = {
        "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
        "escape": 0x1B, "space": 0x20, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
        "0": 0x30, "c": 0x43, "f": 0x46, "j": 0x4A, "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E,
        "p": 0x50, "r": 0x52, "t": 0x54, "w": 0x57, "f11": 0x7A,
        "vol_mute": 0xAD, "vol_down": 0xAE, "vol_up": 0xAF, "media_play": 0xB3,
        "equal": 0xBB, "minus": 0xBD,
    }
    EXTENDED = {"left", "up", "right", "down", "vol_mute", "vol_down", "vol_up", "media_play"}
    # Windows has no standard brightness key; those buttons do nothing here.
    supported_keys = set(VK)

    def __init__(self):
        super().__init__()
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        user32 = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:  # Use physical pixels so moves land where expected on scaled displays.
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except (OSError, AttributeError):
            user32.SetProcessDPIAware()

        ulong_ptr = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", UNION)]

        self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT = MOUSEINPUT, KEYBDINPUT, INPUT
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        self.point = wintypes.POINT

    def _send(self, *inputs):
        array = (self.INPUT * len(inputs))(*inputs)
        return self.user32.SendInput(len(inputs), array, self.ctypes.sizeof(self.INPUT)) == len(inputs)

    def _mouse(self, flags, data=0, dx=0, dy=0):
        event = self.INPUT(type=0)
        event.union.mi = self.MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        return self._send(event)

    def _keyboard(self, vk=0, scan=0, flags=0):
        event = self.INPUT(type=1)
        event.union.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return self._send(event)

    def move_by(self, dx, dy):
        # Place the cursor exactly, like the Mac helper, so Windows mouse
        # acceleration does not change the distance. A zero-length move then
        # tells apps (and drags) that the pointer moved.
        position = self.point()
        self.user32.GetCursorPos(self.ctypes.byref(position))
        left, top = self.user32.GetSystemMetrics(76), self.user32.GetSystemMetrics(77)
        width, height = self.user32.GetSystemMetrics(78), self.user32.GetSystemMetrics(79)
        x = min(max(position.x + dx, left), left + width - 1)
        y = min(max(position.y + dy, top), top + height - 1)
        self.user32.SetCursorPos(x, y)
        self._mouse(0x0001)

    def button(self, name, down):
        flags = {("left", True): 0x0002, ("left", False): 0x0004,
                 ("right", True): 0x0008, ("right", False): 0x0010}[(name, down)]
        self._mouse(flags)

    def wheel(self, vertical, horizontal):
        if vertical:
            self._mouse(0x0800, vertical)
        if horizontal:
            self._mouse(0x1000, horizontal)  # Windows: positive horizontal scrolls right.

    def key(self, name, down):
        flags = (0 if down else 0x0002) | (0x0001 if name in self.EXTENDED else 0)
        self._keyboard(vk=self.VK[name], flags=flags)

    def text(self, value):
        for char in value:
            if char in "\r\n":
                self.press((), "enter")
                continue
            if char == "\t":
                self.press((), "tab")
                continue
            units = char.encode("utf-16-le")
            for index in range(0, len(units), 2):
                unit = int.from_bytes(units[index:index + 2], "little")
                self._keyboard(scan=unit, flags=0x0004)
                self._keyboard(scan=unit, flags=0x0004 | 0x0002)
            time.sleep(0.008)


class LinuxInput(InputBackend):
    """A virtual mouse and keyboard through /dev/uinput. Works on X11 and Wayland."""

    EV_SYN, EV_KEY, EV_REL = 0, 1, 2
    REL_X, REL_Y, REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0, 1, 6, 8, 11, 12
    BTN = {"left": 0x110, "right": 0x111}
    KEY = {
        "escape": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
        "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
        "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
        "leftbrace": 26, "rightbrace": 27, "enter": 28, "ctrl": 29,
        "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
        "semicolon": 39, "apostrophe": 40, "grave": 41, "shift": 42, "backslash": 43,
        "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
        "comma": 51, "dot": 52, "slash": 53, "alt": 56, "space": 57, "f11": 87,
        "up": 103, "left": 105, "right": 106, "down": 108,
        "vol_mute": 113, "vol_down": 114, "vol_up": 115, "media_play": 164,
        "bright_down": 224, "bright_up": 225,
    }
    # Characters typed through a US keyboard layout: char -> (shift, key).
    CHARACTERS = {
        **{letter: (False, letter) for letter in "abcdefghijklmnopqrstuvwxyz"},
        **{letter.upper(): (True, letter) for letter in "abcdefghijklmnopqrstuvwxyz"},
        **{digit: (False, digit) for digit in "0123456789"},
        **{char: (True, digit) for char, digit in zip("!@#$%^&*()", "1234567890")},
        " ": (False, "space"), "\t": (False, "tab"), "\n": (False, "enter"), "\r": (False, "enter"),
        "-": (False, "minus"), "_": (True, "minus"), "=": (False, "equal"), "+": (True, "equal"),
        "[": (False, "leftbrace"), "{": (True, "leftbrace"), "]": (False, "rightbrace"), "}": (True, "rightbrace"),
        ";": (False, "semicolon"), ":": (True, "semicolon"), "'": (False, "apostrophe"), '"': (True, "apostrophe"),
        "`": (False, "grave"), "~": (True, "grave"), "\\": (False, "backslash"), "|": (True, "backslash"),
        ",": (False, "comma"), "<": (True, "comma"), ".": (False, "dot"), ">": (True, "dot"),
        "/": (False, "slash"), "?": (True, "slash"),
    }
    UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
    UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x5501, 0x5502
    DEVICE = "/dev/uinput"

    def __init__(self, opener=None, ioctl=None):
        super().__init__()
        self.opener = opener or (lambda: os.open(self.DEVICE, os.O_WRONLY | os.O_NONBLOCK))
        if ioctl is None:
            import fcntl
            ioctl = fcntl.ioctl
        self.ioctl = ioctl
        self.fd = None
        self.wheel_rest = [0, 0]  # Hi-res units not yet sent as whole notches.

    def ready(self):
        if self.fd is not None:
            return True
        try:
            fd = self.opener()
        except OSError:
            return False
        try:
            self.ioctl(fd, self.UI_SET_EVBIT, self.EV_KEY)
            self.ioctl(fd, self.UI_SET_EVBIT, self.EV_REL)
            for code in sorted(set(self.KEY.values()) | set(self.BTN.values())):
                self.ioctl(fd, self.UI_SET_KEYBIT, code)
            for code in (self.REL_X, self.REL_Y, self.REL_WHEEL, self.REL_HWHEEL,
                         self.REL_WHEEL_HI_RES, self.REL_HWHEEL_HI_RES):
                self.ioctl(fd, self.UI_SET_RELBIT, code)
            # struct uinput_setup: input_id (bus, vendor, product, version), name[80], ff_effects_max.
            setup = struct.pack("HHHH80sI", 0x06, 0x1209, 0x4C52, 1, b"Lazy Remote", 0)
            self.ioctl(fd, self.UI_DEV_SETUP, setup)
            self.ioctl(fd, self.UI_DEV_CREATE)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        time.sleep(0.3)  # Let the desktop pick up the new device before the first event.
        return True

    def emit(self, event_type, code, value):
        # struct input_event: struct timeval, __u16 type, __u16 code, __s32 value.
        os.write(self.fd, struct.pack("llHHi", 0, 0, event_type, code, value))

    def sync(self):
        self.emit(self.EV_SYN, 0, 0)

    def move_by(self, dx, dy):
        if dx:
            self.emit(self.EV_REL, self.REL_X, dx)
        if dy:
            self.emit(self.EV_REL, self.REL_Y, dy)
        self.sync()

    def button(self, name, down):
        self.emit(self.EV_KEY, self.BTN[name], int(down))
        self.sync()

    def wheel(self, vertical, horizontal):
        # Linux: positive REL_WHEEL scrolls up, positive REL_HWHEEL scrolls right.
        for index, (value, fine, notch) in enumerate((
            (vertical, self.REL_WHEEL_HI_RES, self.REL_WHEEL),
            (horizontal, self.REL_HWHEEL_HI_RES, self.REL_HWHEEL),
        )):
            if not value:
                continue
            self.emit(self.EV_REL, fine, value)
            total = self.wheel_rest[index] + value
            notches = int(total / 120)
            self.wheel_rest[index] = total - notches * 120
            if notches:
                self.emit(self.EV_REL, notch, notches)
        self.sync()

    def key(self, name, down):
        self.emit(self.EV_KEY, self.KEY[name], int(down))
        self.sync()

    def text(self, value):
        # uinput sends key positions, not characters, so this assumes a US layout.
        # Characters outside it are skipped.
        for char in value:
            entry = self.CHARACTERS.get(char)
            if entry is None:
                continue
            shift, name = entry
            self.press(("shift",) if shift else (), name)
            time.sleep(0.004)

    def close(self):
        if self.fd is None:
            return
        try:
            super().close()
            self.ioctl(self.fd, self.UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None


LINUX_SETUP = (
    'echo \'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"\' '
    "| sudo tee /etc/udev/rules.d/70-lazy-remote-uinput.rules && "
    "sudo modprobe uinput && sudo udevadm control --reload-rules && sudo udevadm trigger"
)


def create():
    if os.name == "nt":
        return WindowsInput()
    if sys.platform.startswith("linux"):
        return LinuxInput()
    raise RuntimeError("This platform uses the macOS input helper.")


if __name__ == "__main__":  # Manual check: send one JSON command per line on stdin.
    backend = create()
    for line in sys.stdin:
        if line.strip():
            print("ok" if backend.send(json.loads(line)) else "permission", flush=True)
    backend.close()
