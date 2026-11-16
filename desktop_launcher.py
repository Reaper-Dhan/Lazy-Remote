"""Lazy Remote launcher window for Windows and Linux (the Mac uses Launcher.swift).

It starts the server as a child process (`--server --launcher`) and talks to it
over stdin/stdout with the same JSON lines the Mac launcher uses.
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from platform_support import IS_LINUX, IS_WINDOWS, default_state_dir, open_folder


def run_server():
    sys.argv = [sys.argv[0]] + [arg for arg in sys.argv[1:] if arg != "--server"]
    if sys.stdout is not None:
        sys.stdout.reconfigure(line_buffering=True)
    import server
    server.main()


KEEP_RUNNING = default_state_dir() / ".keep-running"
ALLOW_BACKGROUND = default_state_dir() / ".allow-background-resume"


def server_command(keep_running=False, allow_background=False, bind_address=""):
    extra = []
    if bind := bind_address.strip():
        extra.extend(("--bind", bind))
    if keep_running:
        extra.append("--keep-running")
    if allow_background:
        extra.append("--allow-background-resume")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--server", "--launcher", *extra]
    return [sys.executable, "-B", "-u", str(Path(__file__).resolve().with_name("server.py")), "--launcher", *extra]


def enable_sharp_scaling():
    """Tell Windows this window draws at the screen's real resolution.

    Without it, Windows stretches the Tk window on scaled displays (125%, 150%)
    and the text looks blurry. Must run before the first window is created.
    """
    import ctypes
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # per-monitor aware
    except (OSError, AttributeError):
        try:
            ctypes.WinDLL("user32").SetProcessDPIAware()  # Windows 7 and 8
        except (OSError, AttributeError):
            pass


class App:
    def __init__(self):
        if IS_WINDOWS:
            enable_sharp_scaling()
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root = tk.Tk()
        self.root.title("Lazy Remote")
        self.root.resizable(False, False)
        self.root.minsize(490, 0)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "touch-icon.png"
        try:
            self.icon = tk.PhotoImage(file=str(icon))
            self.root.iconphoto(True, self.icon)
            self.brand_icon = self.icon.subsample(4, 4)
        except tk.TclError:
            self.brand_icon = None
        self.configure_theme()

        self.events = queue.Queue()
        self.process = None
        self.guide = None
        self.show_code = tk.BooleanVar(value=False)
        self.address = tk.StringVar()
        self.bind_address = tk.StringVar()
        self.keep_running = tk.BooleanVar(value=KEEP_RUNNING.exists())
        self.allow_background = tk.BooleanVar(value=ALLOW_BACKGROUND.exists())
        self.reset_state()
        self.phase = "stopped"
        self.notice = None
        self.body = None
        self.render()
        self.root.after(100, self.poll)

    def configure_theme(self):
        style = self.ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        page, surface, text, muted = "#f2f4f5", "#ffffff", "#161f28", "#4c5967"
        self.root.configure(background=page)
        style.configure("Shell.TFrame", background=page)
        style.configure("TFrame", background=surface)
        style.configure("Card.TFrame", background=surface, relief="solid", borderwidth=1,
                        bordercolor="#cad4dc", lightcolor="#cad4dc", darkcolor="#cad4dc")
        style.configure("TLabel", background=surface, foreground=text)
        style.configure("TButton", padding=(12, 7), foreground=text, background="#ecf0f2")
        style.map("TButton", background=[("active", "#dbe3e8")])
        style.configure("Primary.TButton", background="#326218", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#274d12")])
        style.configure("TCheckbutton", background=surface, foreground=text)
        style.configure("TRadiobutton", background=surface, foreground=text)
        style.configure("Brand.TLabel", background=page, font=("TkDefaultFont", 19, "bold"))
        style.configure("Status.TLabel", background=page, foreground=muted)
        style.configure("Online.TLabel", background=page, foreground="#326218")
        style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Head.TLabel", font=("TkDefaultFont", 12, "bold"))
        style.configure("Match.TLabel", font=("TkFixedFont", 28, "bold"),
                        background="#e3efda", foreground="#326218", padding=(12, 5))
        style.configure("Muted.TLabel", foreground=muted)

    # Server process -------------------------------------------------------
    def reset_state(self):
        self.urls, self.code, self.qr, self.match = [], None, None, None
        self.paired = self.remembered = self.approved = self.loopback = False
        self.input_ready = self.certificates = self.fingerprint = None
        self.address.set("")

    def start(self):
        if self.phase != "stopped":
            return
        self.notice = None
        flags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW: no console window.
        try:
            self.process = subprocess.Popen(
                server_command(self.keep_running.get(), self.allow_background.get(), self.bind_address.get()),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=flags,
            )
        except OSError as error:
            self.notice = f"Could not start Lazy Remote. {error}"
            self.render()
            return
        self.phase = "starting"
        threading.Thread(target=self.read, args=(self.process,), daemon=True).start()
        self.render()

    def read(self, process):
        for line in process.stdout:
            try:
                self.events.put(json.loads(line.decode("utf-8")))
            except ValueError:
                continue
        process.wait()
        self.events.put({"event": "_stopped", "status": process.returncode})

    def send(self, command):
        if self.phase != "running" or not self.process:
            return
        try:
            self.process.stdin.write((command + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError:
            self.notice = "The server stopped responding. Stop and start it again."
            self.render()

    def stop(self):
        if not self.process or self.phase not in ("starting", "running"):
            return
        self.phase = "stopping"
        try:
            self.process.stdin.close()
        except OSError:
            pass
        process = self.process
        self.root.after(3000, lambda: process.poll() is None and process.terminate())
        self.render()

    def quit(self):
        if self.process and self.process.poll() is None and self.phase == "running":
            from tkinter import messagebox
            keep = messagebox.askyesnocancel(
                "Lazy Remote is running",
                "Keep the remote running in the background?\n\n"
                "Yes minimizes this window. No stops the server and quits.",
                parent=self.root,
            )
            if keep is None:
                return
            if keep:
                self.root.iconify()
                return
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
        self.root.destroy()

    def poll(self):
        changed = False
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self.receive(event)
            changed = True
        if changed:
            self.render()
        self.root.after(100, self.poll)

    def receive(self, event):
        kind = event.get("event")
        if kind == "_stopped":
            if self.phase != "stopping" and event["status"] and not self.notice:
                self.notice = "The server closed unexpectedly. Try Start again."
            self.process = None
            self.phase = "stopped"
            self.reset_state()
            return
        if kind == "notice":
            self.notice = event.get("message")
            return
        if self.phase not in ("starting", "running"):
            return
        if kind == "permission":
            self.input_ready = event.get("input_ready")
            return
        if kind == "ready":
            self.urls = event.get("urls") or []
            self.address.set(self.urls[0] if self.urls else "")
            self.certificates = event.get("certificates")
            self.fingerprint = event.get("fingerprint")
            self.loopback = bool(event.get("loopback"))
            self.input_ready = event.get("input_ready")
            self.phase = "running"
        if kind == "pairing" and event.get("match") and not self.match:
            # A phone asks for control while the window may be minimized: bring it back.
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        self.code, self.qr, self.match = event.get("code"), event.get("qr"), event.get("match")
        self.paired, self.remembered = bool(event.get("paired")), bool(event.get("remembered"))
        self.approved = bool(event.get("approved"))

    # Main window ----------------------------------------------------------
    def label(self, parent, text, style=None, **grid):
        widget = self.ttk.Label(parent, text=text, style=style, wraplength=460, justify="left")
        widget.pack(anchor="w", **grid)
        return widget

    def render(self):
        self.render_main()
        if self.guide is not None and self.guide.winfo_exists():
            self.render_guide()

    def render_main(self):
        ttk = self.ttk
        if self.body is not None:
            self.body.destroy()
        body = self.body = ttk.Frame(self.root, padding=20, style="Shell.TFrame")
        body.pack(fill="both", expand=True)

        header = ttk.Frame(body, style="Shell.TFrame")
        header.pack(fill="x", pady=(0, 14))
        if self.brand_icon is not None:
            ttk.Label(header, image=self.brand_icon, style="Brand.TLabel").pack(side="left", padx=(0, 9))
        brand = ttk.Frame(header, style="Shell.TFrame")
        brand.pack(side="left")
        ttk.Label(brand, text="Lazy Remote", style="Brand.TLabel").pack(anchor="w")
        status = {"stopped": "Server stopped", "starting": "Starting server…",
                  "running": "Phone connected" if self.paired else "Server running",
                  "stopping": "Stopping server…"}[self.phase]
        ttk.Label(brand, text=status, style="Online.TLabel" if self.phase == "running" else "Status.TLabel").pack(anchor="w")
        if self.phase in ("starting", "running"):
            ttk.Button(header, text="Stop server", command=self.stop).pack(side="right")
        ttk.Button(header, text="Set up phone", command=self.open_guide).pack(side="right", padx=6)
        ttk.Button(body, text="Bluetooth setup", command=self.open_bluetooth_guide).pack(anchor="w", pady=(0, 12))

        if self.notice:
            self.label(self.card(body), self.notice)

        if self.phase == "stopped":
            intro = self.card(body)
            self.label(intro, "Your phone is the remote", "Title.TLabel")
            self.label(intro, "Use it as a trackpad, keyboard, and media controller on your local network.",
                       "Muted.TLabel", pady=(5, 12))
            ttk.Button(intro, text="Start server", command=self.start, style="Primary.TButton").pack(anchor="w")
            settings = self.card(body)
            self.label(settings, "Connection and session settings", "Head.TLabel")
            self.network_setting(settings)
            ttk.Checkbutton(settings, text="Keep running when the phone is idle", variable=self.keep_running,
                            command=lambda: self.save_setting(KEEP_RUNNING, self.keep_running.get())).pack(anchor="w", pady=(8, 0))
            self.label(settings, "The server stays on until you stop it. The phone still locks after 5 minutes."
                       if self.keep_running.get() else
                       "The server stops after 30 minutes without phone activity.", "Muted.TLabel")
            self.background_setting(settings)
            return
        if self.phase != "running":
            self.label(self.card(body), status, "Head.TLabel")
            return

        if self.input_ready is False:
            permission = self.card(body)
            self.label(permission, "Input access needed", "Head.TLabel")
            self.label(permission, "Open the setup guide to enable mouse and keyboard control.", "Muted.TLabel")
        self.connection(self.card(body))
        self.pairing(self.card(body))

    def card(self, parent):
        card = self.ttk.Frame(parent, padding=14, style="Card.TFrame")
        card.pack(fill="x", pady=(0, 10))
        return card

    def connection(self, body):
        ttk = self.ttk
        self.label(body, "Phone address", "Head.TLabel", pady=(0, 7))
        row = ttk.Frame(body)
        row.pack(fill="x")
        if len(self.urls) > 1:
            addresses = ttk.Combobox(row, textvariable=self.address, values=self.urls, state="readonly", width=34)
            addresses.pack(side="left")
            addresses.bind("<<ComboboxSelected>>", lambda _: self.render())
        else:
            entry = ttk.Entry(row, textvariable=self.address, width=36, state="readonly")
            entry.pack(side="left")
        ttk.Button(row, text="Copy link", command=self.copy_address).pack(side="right")
        if self.loopback:
            self.label(body, "Connect this PC and your phone to the same private network, then restart the server.",
                       "Muted.TLabel")

    def save_setting(self, path, enabled):
        try:
            if enabled:
                path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                path.touch()
            else:
                path.unlink(missing_ok=True)
        except OSError:
            self.notice = "Could not save the setting. It still applies to this launch."
        self.render()

    def background_setting(self, parent):
        self.ttk.Checkbutton(
            parent, text="Extend phone reconnect window", variable=self.allow_background,
            command=lambda: self.save_setting(ALLOW_BACKGROUND, self.allow_background.get()),
        ).pack(anchor="w", pady=(8, 0))
        self.label(parent, "Resume the same tab within one minute, or up to five minutes with this on.",
                   "Muted.TLabel")

    def network_setting(self, parent):
        self.label(parent, "Host IPv4 address", pady=(8, 2))
        self.ttk.Entry(parent, textvariable=self.bind_address, width=25).pack(anchor="w")
        self.label(parent, "For Bluetooth tethering, enter this PC’s IPv4 address on the Bluetooth network. "
                           "Leave blank for automatic selection.", "Muted.TLabel")

    def copy_address(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.address.get())

    def pairing(self, body):
        ttk = self.ttk
        frame = ttk.Frame(body)
        frame.pack(fill="x")
        if self.match:
            self.label(frame, "Approved. Waiting for your phone…" if self.approved else "Phone requests control",
                       "Head.TLabel")
            self.label(frame, self.match, "Match.TLabel")
            if not self.approved:
                self.label(frame, "Approve only if this number matches the one on your phone.", "Muted.TLabel")
                row = ttk.Frame(frame)
                row.pack(anchor="w", pady=6)
                match = self.match
                ttk.Button(row, text="Approve", command=lambda: self.send(f"approve {match}"), style="Primary.TButton").pack(side="left")
                ttk.Button(row, text="Deny", command=lambda: self.send(f"deny {match}")).pack(side="left", padx=8)
        elif self.paired:
            self.label(frame, "Phone connected", "Head.TLabel")
            ttk.Button(frame, text="Lock phone", command=lambda: self.send("new_code")).pack(anchor="w", pady=6)
            if self.remembered:
                ttk.Button(frame, text="Forget browser", command=lambda: self.send("forget")).pack(anchor="w")
        elif self.qr and self.code and self.address.get():
            row = ttk.Frame(frame)
            row.pack(fill="x")
            self.draw_qr(row, self.address.get() + "#pair=" + self.qr)
            side = ttk.Frame(row)
            side.pack(side="left", padx=16, fill="both")
            ttk.Label(side, text="Scan to pair", style="Head.TLabel").pack(anchor="w")
            message = ("Open the same address to reconnect, or scan to pair at this address." if self.remembered
                       else "Tap Scan pairing QR in the phone app, or use your phone camera. Approve the request here.")
            ttk.Label(side, text=message,
                      style="Muted.TLabel", wraplength=240).pack(anchor="w")
            ttk.Label(side, text="QR code expires in 10 minutes.", style="Muted.TLabel").pack(anchor="w", pady=4)
            ttk.Checkbutton(side, text="Show pairing code", variable=self.show_code,
                            command=self.render).pack(anchor="w")
            if self.show_code.get():
                code = ttk.Entry(side, width=16, font=("TkFixedFont", 13, "bold"))
                code.insert(0, self.code)
                code.configure(state="readonly")
                code.pack(anchor="w", pady=4)
        else:
            if self.remembered:
                self.label(frame, "Open the same address on your phone and tap Request access.", "Muted.TLabel")
            ttk.Button(frame, text="Show a new pairing QR code", command=lambda: self.send("new_code")).pack(anchor="w")
        if self.remembered and not self.paired and not self.match:
            ttk.Button(frame, text="Forget browser", command=lambda: self.send("forget")).pack(anchor="w", pady=4)

    def draw_qr(self, parent, payload, size=176):
        try:
            import qrcode
        except ImportError:
            self.ttk.Label(parent, text="QR codes need the qrcode package.\nUse “Show pairing code”.",
                           style="Muted.TLabel").pack(side="left")
            return
        code = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        code.add_data(payload)
        code.make(fit=True)
        matrix = code.get_matrix()
        cell = max(2, size // len(matrix))
        size = cell * len(matrix)
        canvas = self.tk.Canvas(parent, width=size, height=size, background="white", highlightthickness=0)
        for y, line in enumerate(matrix):
            for x, dark in enumerate(line):
                if dark:
                    canvas.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell,
                                            fill="black", outline="black")
        canvas.pack(side="left")

    # Setup guide ----------------------------------------------------------
    def open_bluetooth_guide(self):
        guide = self.tk.Toplevel(self.root)
        guide.title("Bluetooth tethering")
        guide.transient(self.root)
        frame = self.ttk.Frame(guide, padding=20)
        frame.pack(fill="both")
        self.label(frame, "Connect without Wi-Fi", "Title.TLabel")
        self.label(frame, "1. Enable Bluetooth tethering on Android or Personal Hotspot on iPhone. "
                          "Join that Bluetooth network from this PC using system settings. "
                          "Pairing alone does not create a network.")
        self.label(frame, "2. Stop Lazy Remote. Enter this PC’s Bluetooth network IPv4 address "
                          "in Host IPv4 address, then start the server again. Find the address "
                          "in network adapter settings, ipconfig on Windows, or ip -4 address on Linux.")
        self.label(frame, "3. Open the new HTTPS address on the phone. Install the trust certificate "
                          "if needed, then request access and approve the matching number on this PC.")
        self.label(frame, "Direct Bluetooth is currently a native iPhone / Mac preview. "
                          "Windows, Linux and TV use network connections, including Bluetooth tethering "
                          "where the system supports it.", "Muted.TLabel")
        if IS_WINDOWS:
            self.ttk.Button(frame, text="Open Bluetooth Settings",
                            command=lambda: os.startfile("ms-settings:bluetooth")).pack(anchor="w", pady=8)
        self.ttk.Button(frame, text="Done", command=guide.destroy).pack(anchor="e")

    def open_guide(self):
        if self.guide is not None and self.guide.winfo_exists():
            self.guide.lift()
            return
        self.guide = self.tk.Toplevel(self.root)
        self.guide.title("Set up Lazy Remote")
        self.guide.resizable(False, False)
        self.phone = self.tk.IntVar(value=0)
        self.render_guide()

    def render_guide(self):
        ttk, guide = self.ttk, self.guide
        for child in guide.winfo_children():
            child.destroy()
        frame = ttk.Frame(guide, padding=20)
        frame.pack(fill="both")
        ttk.Label(frame, text="Set up your phone", style="Title.TLabel").pack(anchor="w", pady=(0, 10))

        def step(number, title):
            ttk.Label(frame, text=f"{number}.  {title}", style="Head.TLabel").pack(anchor="w", pady=(10, 2))

        step(1, "Start the server")
        if self.phase == "stopped":
            self.network_setting(frame)
            ttk.Button(frame, text="Start server", command=self.start, style="Primary.TButton").pack(anchor="w", pady=4)
            self.background_setting(frame)
        self.label(frame, "Starting the server creates this PC’s certificate. "
                          "If a firewall asks, allow Lazy Remote on private networks only.")
        if self.notice:
            self.label(frame, self.notice)

        step(2, "Allow mouse and keyboard control")
        if IS_LINUX:
            import input_backends
            self.label(frame, "Linux needs an active desktop with a readable lock state and access to /dev/uinput. "
                              "Run this once in a terminal, then log out and back in:")
            command = self.tk.Text(frame, height=4, width=62, wrap="word")
            command.insert("1.0", input_backends.LINUX_SETUP)
            command.configure(state="disabled")
            command.pack(anchor="w", pady=4)
            row = ttk.Frame(frame)
            row.pack(anchor="w")
            ttk.Button(row, text="Copy command", command=lambda: (
                self.root.clipboard_clear(), self.root.clipboard_append(input_backends.LINUX_SETUP))).pack(side="left")
            ttk.Button(row, text="Check again", command=lambda: self.send("check_access")).pack(side="left", padx=8)
            self.label(frame, "This lets any app you run, not only Lazy Remote, create a virtual keyboard "
                              "and mouse. Remove it when you stop using the remote:", "Muted.TLabel")
            row = ttk.Frame(frame)
            row.pack(anchor="w", pady=(2, 0))
            ttk.Button(row, text="Copy undo command", command=lambda: (
                self.root.clipboard_clear(), self.root.clipboard_append(input_backends.LINUX_UNDO))).pack(side="left")
        else:
            self.label(frame, "Nothing to do on Windows. The remote cannot control apps that run as "
                              "administrator.")

        step(3, "Trust this PC on your phone (once)")
        row = ttk.Frame(frame)
        row.pack(anchor="w")
        for value, text in ((0, "iPhone or iPad"), (1, "Android")):
            ttk.Radiobutton(row, text=text, value=value, variable=self.phone,
                            command=self.render_guide).pack(side="left", padx=(0, 10))
        if self.phase != "running" or not self.certificates:
            self.label(frame, "Start the server first to create the certificate.", "Muted.TLabel")
        else:
            ttk.Button(frame, text="Open certificate folder",
                       command=lambda: open_folder(self.certificates)).pack(anchor="w", pady=4)
            if self.phone.get() == 0:
                steps = ["Send “Lazy Remote.mobileconfig” to your own iPhone (cable, email to yourself, or cloud drive).",
                         "On the iPhone, open Settings → Profile Downloaded → Install.",
                         "Open Settings → General → About → Certificate Trust Settings.",
                         "Turn on Lazy Remote Local Root."]
            else:
                steps = ["Send “Lazy Remote Root.cer” to your own phone, for example with a USB cable.",
                         "Open Settings and search for “CA certificate”.",
                         "Choose Install a certificate → CA certificate, then pick the file."]
            for index, line in enumerate(steps, 1):
                self.label(frame, f"{index}. {line}", "Muted.TLabel")
            if self.fingerprint:
                fingerprint = ttk.Entry(frame, width=70)
                fingerprint.insert(0, self.fingerprint)
                fingerprint.configure(state="readonly")
                self.label(frame, "Fingerprint, to check on the phone:", "Muted.TLabel")
                fingerprint.pack(anchor="w")
            self.label(frame, "Transfer the profile or certificate directly to your own phone.", "Muted.TLabel")

        step(4, "Pair your phone")
        self.label(frame, "Connect both devices to the same network. Open the phone address or scan the pairing QR, "
                          "then approve on this computer.")
        ttk.Button(frame, text="Done", command=self.guide.destroy).pack(anchor="e", pady=(12, 0))


def main():
    if "--server" in sys.argv:
        run_server()
        return
    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    main()
