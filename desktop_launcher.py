"""Lazy Remote launcher window for Windows and Linux (the Mac uses Launcher.swift).

It starts the server as a child process (`--server --launcher`) and talks to it
over stdin/stdout with the same JSON lines the Mac launcher uses.
"""
import json
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


def server_command(keep_running=False):
    extra = ["--keep-running"] if keep_running else []
    if getattr(sys, "frozen", False):
        return [sys.executable, "--server", "--launcher", *extra]
    return [sys.executable, "-B", "-u", str(Path(__file__).resolve().with_name("server.py")), "--launcher", *extra]


class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root = tk.Tk()
        self.root.title("Lazy Remote")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "touch-icon.png"
        try:
            self.icon = tk.PhotoImage(file=str(icon))
            self.root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("TkDefaultFont", 16, "bold"))
        style.configure("Head.TLabel", font=("TkDefaultFont", 11, "bold"))
        style.configure("Match.TLabel", font=("TkFixedFont", 28, "bold"))
        style.configure("Muted.TLabel", foreground="#666666")

        self.events = queue.Queue()
        self.process = None
        self.guide = None
        self.show_code = tk.BooleanVar(value=False)
        self.address = tk.StringVar()
        self.keep_running = tk.BooleanVar(value=KEEP_RUNNING.exists())
        self.reset_state()
        self.phase = "stopped"
        self.notice = None
        self.body = None
        self.render()
        self.root.after(100, self.poll)

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
                server_command(self.keep_running.get()), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
        body = self.body = ttk.Frame(self.root, padding=20)
        body.pack(fill="both", expand=True)

        header = ttk.Frame(body)
        header.pack(fill="x")
        ttk.Label(header, text="Lazy Remote", style="Title.TLabel").pack(side="left")
        status = {"stopped": "Server stopped", "starting": "Starting server…",
                  "running": "Server running on this PC", "stopping": "Stopping server…"}[self.phase]
        ttk.Label(header, text="  " + status, style="Muted.TLabel").pack(side="left")
        if self.phase in ("starting", "running"):
            ttk.Button(header, text="Stop server", command=self.stop).pack(side="right")

        if self.notice:
            self.label(body, "⚠ " + self.notice, pady=(10, 0))

        if self.phase == "stopped":
            self.label(body, "Control this PC from your phone.", pady=(14, 8))
            row = ttk.Frame(body)
            row.pack(anchor="w")
            ttk.Button(row, text="Start server", command=self.start).pack(side="left")
            ttk.Button(row, text="Setup guide", command=self.open_guide).pack(side="left", padx=8)
            ttk.Checkbutton(body, text="Keep running when the phone is idle", variable=self.keep_running,
                            command=self.save_keep_running).pack(anchor="w", pady=(12, 0))
            self.label(body, "The server stays on until you stop it. The phone still locks after 5 minutes."
                       if self.keep_running.get() else
                       "The server stops after 30 minutes without phone activity.", "Muted.TLabel")
            return
        if self.phase != "running":
            return

        if self.input_ready is False:
            self.label(body, "Mouse and keyboard control is not allowed yet. Open the setup guide, step 2.",
                       pady=(10, 0))
        self.connection(body)
        self.pairing(body)
        ttk.Separator(body).pack(fill="x", pady=12)
        ttk.Button(body, text="Set up phone", command=self.open_guide).pack(anchor="e")

    def connection(self, body):
        ttk = self.ttk
        self.label(body, "Phone address", "Head.TLabel", pady=(14, 4))
        row = ttk.Frame(body)
        row.pack(fill="x")
        if len(self.urls) > 1:
            ttk.Combobox(row, textvariable=self.address, values=self.urls, state="readonly", width=34).pack(side="left")
        else:
            entry = ttk.Entry(row, textvariable=self.address, width=36, state="readonly")
            entry.pack(side="left")
        ttk.Button(row, text="Copy link", command=self.copy_address).pack(side="right")
        if self.loopback:
            self.label(body, "Connect this PC and your phone to the same private network, then restart the server.",
                       "Muted.TLabel")

    def save_keep_running(self):
        try:
            if self.keep_running.get():
                KEEP_RUNNING.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                KEEP_RUNNING.touch()
            else:
                KEEP_RUNNING.unlink(missing_ok=True)
        except OSError:
            pass
        self.render()

    def copy_address(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.address.get())

    def pairing(self, body):
        ttk = self.ttk
        frame = ttk.Frame(body)
        frame.pack(fill="x", pady=(14, 0))
        if self.match:
            self.label(frame, "Approved. Waiting for your phone…" if self.approved else "Phone requests control",
                       "Head.TLabel")
            self.label(frame, self.match, "Match.TLabel")
            if not self.approved:
                self.label(frame, "Approve only if this number matches the one on your phone.", "Muted.TLabel")
                row = ttk.Frame(frame)
                row.pack(anchor="w", pady=6)
                match = self.match
                ttk.Button(row, text="Approve", command=lambda: self.send(f"approve {match}")).pack(side="left")
                ttk.Button(row, text="Deny", command=lambda: self.send(f"deny {match}")).pack(side="left", padx=8)
        elif self.paired:
            self.label(frame, "✓ Phone connected", "Head.TLabel")
            text = "Forget browser" if self.remembered else "Revoke and show a new QR code"
            ttk.Button(frame, text=text,
                       command=lambda: self.send("forget" if self.remembered else "new_code")).pack(anchor="w", pady=6)
        elif self.remembered:
            self.label(frame, "Browser remembered", "Head.TLabel")
            self.label(frame, "Open the same address on your phone and tap Request access.", "Muted.TLabel")
            ttk.Button(frame, text="Forget browser & show QR code",
                       command=lambda: self.send("forget")).pack(anchor="w", pady=6)
        elif self.qr and self.code and self.address.get():
            row = ttk.Frame(frame)
            row.pack(fill="x")
            self.draw_qr(row, self.address.get() + "#pair=" + self.qr)
            side = ttk.Frame(row)
            side.pack(side="left", padx=16, fill="both")
            ttk.Label(side, text="Scan to pair", style="Head.TLabel").pack(anchor="w")
            ttk.Label(side, text="Use your phone camera, then request access and approve it here.",
                      style="Muted.TLabel", wraplength=240).pack(anchor="w")
            ttk.Label(side, text="QR code expires in 10 minutes.", style="Muted.TLabel").pack(anchor="w", pady=4)
            ttk.Checkbutton(side, text="Enter a code instead", variable=self.show_code,
                            command=self.render).pack(anchor="w")
            if self.show_code.get():
                code = ttk.Entry(side, width=16, font=("TkFixedFont", 13, "bold"))
                code.insert(0, self.code)
                code.configure(state="readonly")
                code.pack(anchor="w", pady=4)
        else:
            ttk.Button(frame, text="Show a new pairing QR code", command=lambda: self.send("new_code")).pack(anchor="w")

    def draw_qr(self, parent, payload):
        try:
            import qrcode
        except ImportError:
            self.ttk.Label(parent, text="QR codes need the qrcode package.\nUse “Enter a code instead”.",
                           style="Muted.TLabel").pack(side="left")
            return
        code = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        code.add_data(payload)
        code.make(fit=True)
        matrix = code.get_matrix()
        cell = max(2, 176 // len(matrix))
        size = cell * len(matrix)
        canvas = self.tk.Canvas(parent, width=size, height=size, background="white", highlightthickness=0)
        for y, line in enumerate(matrix):
            for x, dark in enumerate(line):
                if dark:
                    canvas.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell,
                                            fill="black", outline="black")
        canvas.pack(side="left")

    # Setup guide ----------------------------------------------------------
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
        ttk.Label(frame, text="Set up Lazy Remote", style="Title.TLabel").pack(anchor="w", pady=(0, 10))

        def step(number, title):
            ttk.Label(frame, text=f"{number}.  {title}", style="Head.TLabel").pack(anchor="w", pady=(10, 2))

        step(1, "Start the server")
        self.label(frame, "Click Start server in the main window. It creates this PC’s certificate. "
                          "If a firewall asks, allow Lazy Remote on private networks only.")

        step(2, "Allow mouse and keyboard control")
        if IS_LINUX:
            import input_backends
            self.label(frame, "Linux needs access to /dev/uinput, the virtual input device. "
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
            self.label(frame, "This gives only the person at this screen access to the device.", "Muted.TLabel")
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
            self.label(frame, "This file is public. Send it only from this PC to your own phone.", "Muted.TLabel")

        step(4, "Pair your phone")
        self.label(frame, "Join the same Wi-Fi. Scan the QR code with the phone camera, tap Request access, "
                          "then click Approve here if the numbers match.")
        ttk.Button(frame, text="Done", command=self.guide.destroy).pack(anchor="e", pady=(12, 0))


def first_launch():
    """True once: the setup guide opens by itself the first time, like the Mac app."""
    marker = default_state_dir() / ".setup-guide-seen"
    if marker.exists():
        return False
    try:
        marker.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        marker.touch()
    except OSError:
        pass
    return True


def main():
    if "--server" in sys.argv:
        run_server()
        return
    app = App()
    if first_launch():
        app.root.after(300, app.open_guide)
    app.root.mainloop()


if __name__ == "__main__":
    main()
