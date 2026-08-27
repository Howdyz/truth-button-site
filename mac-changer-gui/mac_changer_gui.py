#!/usr/bin/env python3
"""A modern GUI for changing your network interface's MAC address.

Uses GNU macchanger on Linux, and a built-in PowerShell/registry-based
changer (with a UAC elevation prompt) on Windows. macOS is not supported.
"""

import base64
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

import customtkinter as ctk

IS_WINDOWS = os.name == "nt"

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
SHOW_RE = re.compile(
    r"Current MAC:\s+([0-9A-Fa-f:]{17})\s+\((.*?)\)\s*"
    r"Permanent MAC:\s+([0-9A-Fa-f:]{17})\s+\((.*?)\)",
    re.DOTALL,
)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

ACCENT = "#3b82f6"
ACCENT_HOVER = "#2563eb"
DANGER = "#ef4444"
DANGER_HOVER = "#dc2626"
SUCCESS = "#22c55e"
CARD_BG = "#1c1f26"
LOG_OK = "#4ade80"
LOG_ERR = "#f87171"
LOG_INFO = "#93c5fd"
LOG_WARN = "#fbbf24"

# Windows registry class for network adapters — used to reach the
# per-adapter "NetworkAddress" override that Restart-NetAdapter picks up.
WIN_NET_CLASS_GUID = "{4d36e972-e325-11ce-bfc1-08002be10318}"


def list_interfaces():
    if IS_WINDOWS:
        return win_list_interfaces()
    try:
        out = subprocess.run(
            ["ip", "-o", "link", "show"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        name = parts[1].strip().split("@")[0]
        if name == "lo":
            continue
        names.append(name)
    return names


def win_list_interfaces():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def ps_quote(value):
    """Escape a value for embedding in a single-quoted PowerShell string."""
    return "'" + str(value).replace("'", "''") + "'"


def normalize_mac(raw):
    hexchars = re.sub(r"[^0-9A-Fa-f]", "", raw or "")
    if len(hexchars) != 12:
        return ""
    return ":".join(hexchars[i:i + 2] for i in range(0, 12, 2)).upper()


def win_mac_info(iface):
    """Returns (current_mac, permanent_mac) or (None, None) on failure."""
    ps = (
        f"$a = Get-NetAdapter -Name {ps_quote(iface)}; "
        "[PSCustomObject]@{Current=$a.MacAddress; Permanent=$a.PermanentAddress} "
        "| ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        data = json.loads(out)
        cur = normalize_mac(data.get("Current", ""))
        perm = normalize_mac(data.get("Permanent", ""))
        return (cur or None), (perm or None)
    except Exception:
        return None, None


def random_mac_bytes(keep_prefix=None):
    """6 random bytes. With no keep_prefix, the vendor half is also random
    but forced to the locally-administered, unicast address space so it
    never collides with a real assigned OUI or a multicast address."""
    if keep_prefix and len(keep_prefix) == 3:
        b = list(keep_prefix)
    else:
        first = random.randint(0, 255)
        first = (first & 0xFC) | 0x02  # clear multicast bit, set locally-administered bit
        b = [first, random.randint(0, 255), random.randint(0, 255)]
    b += [random.randint(0, 255) for _ in range(3)]
    return b


def mac_to_str(byte_list):
    return ":".join(f"{b:02X}" for b in byte_list)


def win_set_mac(iface, target_mac):
    """Sets (or, if target_mac is None, clears) the NetworkAddress override
    for `iface` via an elevated (UAC) PowerShell process, then restarts the
    adapter so it takes effect. Returns (success, message)."""
    result_path = os.path.join(
        tempfile.gettempdir(), f"macchangergui_{os.getpid()}_{int(time.time() * 1000)}.json"
    )
    mac_no_sep = (target_mac or "").replace(":", "").replace("-", "")

    inner = f"""
$ErrorActionPreference = 'Stop'
$result = @{{ success = $false; message = '' }}
try {{
  $adapter = Get-NetAdapter -Name {ps_quote(iface)}
  $classKey = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{WIN_NET_CLASS_GUID}'
  $sub = Get-ChildItem $classKey -ErrorAction Stop | Where-Object {{
    (Get-ItemProperty $_.PSPath -Name NetCfgInstanceId -ErrorAction SilentlyContinue).NetCfgInstanceId -eq $adapter.InterfaceGuid
  }}
  if (-not $sub) {{ throw 'Could not locate this adapter''s registry key.' }}
  if ('{mac_no_sep}' -eq '') {{
    Remove-ItemProperty -Path $sub.PSPath -Name NetworkAddress -ErrorAction SilentlyContinue
  }} else {{
    Set-ItemProperty -Path $sub.PSPath -Name NetworkAddress -Value '{mac_no_sep}'
  }}
  Restart-NetAdapter -Name {ps_quote(iface)} -Confirm:$false
  Start-Sleep -Seconds 2
  $after = Get-NetAdapter -Name {ps_quote(iface)}
  $result.success = $true
  $result.message = "Adapter now reports MAC $($after.MacAddress)"
}} catch {{
  $result.message = $_.Exception.Message
}}
$result | ConvertTo-Json -Compress | Out-File -FilePath '{result_path}' -Encoding utf8
"""
    inner_b64 = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    launcher = (
        "$p = Start-Process powershell -ArgumentList "
        f"'-NoProfile','-EncodedCommand','{inner_b64}' "
        "-Verb RunAs -Wait -WindowStyle Hidden -PassThru; exit $p.ExitCode"
    )

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", launcher],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for the elevated PowerShell process."
    except Exception as exc:
        return False, str(exc)

    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        finally:
            try:
                os.remove(result_path)
            except OSError:
                pass
        return bool(data.get("success")), data.get("message", "")

    return False, "No response — the UAC prompt may have been cancelled."


def win_toggle_adapter(iface, direction):
    """direction: 'up' or 'down'. Returns (success, message)."""
    result_path = os.path.join(
        tempfile.gettempdir(), f"macchangergui_toggle_{os.getpid()}_{int(time.time() * 1000)}.json"
    )
    verb = "Enable-NetAdapter" if direction == "up" else "Disable-NetAdapter"

    inner = f"""
$ErrorActionPreference = 'Stop'
$result = @{{ success = $false; message = '' }}
try {{
  {verb} -Name {ps_quote(iface)} -Confirm:$false
  Start-Sleep -Seconds 1
  $result.success = $true
}} catch {{
  $result.message = $_.Exception.Message
}}
$result | ConvertTo-Json -Compress | Out-File -FilePath '{result_path}' -Encoding utf8
"""
    inner_b64 = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    launcher = (
        "$p = Start-Process powershell -ArgumentList "
        f"'-NoProfile','-EncodedCommand','{inner_b64}' "
        "-Verb RunAs -Wait -WindowStyle Hidden -PassThru; exit $p.ExitCode"
    )

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", launcher],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for the elevated PowerShell process."
    except Exception as exc:
        return False, str(exc)

    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        finally:
            try:
                os.remove(result_path)
            except OSError:
                pass
        return bool(data.get("success")), data.get("message", "")

    return False, "No response — the UAC prompt may have been cancelled."


class MacChangerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("MAC Changer")
        self.geometry("760x680")
        self.minsize(680, 620)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        self.busy = False
        self.current_process = None  # in-flight Popen for the Linux privileged path, so Kill can abort it

        if IS_WINDOWS:
            self.macchanger_path = None
            self.pkexec_path = None
            self.powershell_path = shutil.which("powershell") or shutil.which("pwsh")
        else:
            self.macchanger_path = shutil.which("macchanger")
            self.pkexec_path = shutil.which("pkexec")
            self.powershell_path = None

        self._build_header()
        self._build_interface_row()
        self._build_info_cards()
        self._build_actions()
        self._build_log()
        self._build_statusbar()

        self.refresh_interfaces()

        if IS_WINDOWS:
            if not self.powershell_path:
                self.log("PowerShell was not found on PATH — this app needs it to read and change adapters.", "err")
            else:
                self.log("Windows detected — changes are applied via PowerShell and will prompt for Administrator access (UAC).", "info")
        else:
            if not self.macchanger_path:
                self.log("macchanger is not installed. Install it with: sudo apt install macchanger", "err")
            if not self.pkexec_path:
                self.log("pkexec not found — privileged actions will fail without it.", "warn")

    # ---------- UI construction ----------

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text="MAC Changer", font=ctk.CTkFont(size=26, weight="bold")
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text="Randomize or set your network interface's MAC address — Linux & Windows",
            font=ctk.CTkFont(size=13),
            text_color="#8b93a1",
        ).grid(row=1, column=0, sticky="w")

    def _build_interface_row(self):
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", padx=24, pady=8)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(row, text="Interface", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, padx=(0, 10), sticky="w"
        )
        self.iface_var = ctk.StringVar(value="")
        self.iface_menu = ctk.CTkOptionMenu(
            row,
            variable=self.iface_var,
            values=["(no interfaces found)"],
            command=lambda _: self.refresh_mac_info(),
            width=220,
        )
        self.iface_menu.grid(row=0, column=1, sticky="w")

        ctk.CTkButton(
            row, text="Refresh", width=90, fg_color="#2a2e38", hover_color="#3a3f4b",
            command=self.refresh_interfaces,
        ).grid(row=0, column=2, padx=(10, 0))

    def _build_info_cards(self):
        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.grid(row=2, column=0, sticky="ew", padx=24, pady=8)
        cards.grid_columnconfigure((0, 1), weight=1)

        self.current_card, self.current_mac_label, self.current_vendor_label = self._make_card(
            cards, 0, "CURRENT MAC", ACCENT
        )
        self.perm_card, self.perm_mac_label, self.perm_vendor_label = self._make_card(
            cards, 1, "PERMANENT (HARDWARE) MAC", "#8b93a1"
        )

    def _make_card(self, parent, col, title, accent):
        card = ctk.CTkFrame(parent, fg_color=CARD_BG, corner_radius=12)
        card.grid(row=0, column=col, sticky="nsew", padx=(0, 10) if col == 0 else (10, 0))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text=title, font=ctk.CTkFont(size=11, weight="bold"), text_color=accent
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 2))
        mac_label = ctk.CTkLabel(
            card, text="—", font=ctk.CTkFont(family="monospace", size=20, weight="bold")
        )
        mac_label.grid(row=1, column=0, sticky="w", padx=16)
        vendor_label = ctk.CTkLabel(
            card, text="", font=ctk.CTkFont(size=12), text_color="#8b93a1"
        )
        vendor_label.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 14))
        return card, mac_label, vendor_label

    def _build_actions(self):
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=24, pady=8)
        actions.grid_columnconfigure((0, 1), weight=1)

        self.btn_random = ctk.CTkButton(
            actions, text="Randomize (fully random)", height=42,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=lambda: self.change_mac("random", "randomize the MAC"),
        )
        self.btn_random.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=4)

        self.btn_same_vendor = ctk.CTkButton(
            actions, text="Randomize (keep current vendor)", height=42,
            fg_color="#2a2e38", hover_color="#3a3f4b",
            command=lambda: self.change_mac("keep_vendor", "randomize the host bytes, keeping the vendor"),
        )
        self.btn_same_vendor.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=4)

        self.btn_new_vendor = ctk.CTkButton(
            actions, text="Randomize (new random vendor)", height=42,
            fg_color="#2a2e38", hover_color="#3a3f4b",
            command=lambda: self.change_mac("new_vendor", "assign a random vendor MAC"),
        )
        self.btn_new_vendor.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=4)

        self.btn_reset = ctk.CTkButton(
            actions, text="Reset to permanent MAC", height=42,
            fg_color="#2a2e38", hover_color="#3a3f4b",
            command=lambda: self.change_mac("reset", "reset to the permanent hardware MAC"),
        )
        self.btn_reset.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=4)

        custom = ctk.CTkFrame(actions, fg_color="transparent")
        custom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        custom.grid_columnconfigure(0, weight=1)

        self.custom_entry = ctk.CTkEntry(
            custom, placeholder_text="XX:XX:XX:XX:XX:XX", font=ctk.CTkFont(family="monospace", size=14)
        )
        self.custom_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.custom_entry.bind("<KeyRelease>", self._format_custom_entry)

        self.btn_custom = ctk.CTkButton(
            custom, text="Set custom MAC", width=150,
            fg_color=SUCCESS, hover_color="#16a34a",
            command=self.set_custom_mac,
        )
        self.btn_custom.grid(row=0, column=1)

        ctrl = ctk.CTkFrame(actions, fg_color="transparent")
        ctrl.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ctrl.grid_columnconfigure((0, 1), weight=1)

        self.btn_start_iface = ctk.CTkButton(
            ctrl, text="▶ Start Interface (bring up)", height=38,
            fg_color="#2a2e38", hover_color="#3a3f4b",
            command=self.start_interface,
        )
        self.btn_start_iface.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.btn_kill_iface = ctk.CTkButton(
            ctrl, text="🛑 Kill Interface (bring down)", height=38,
            fg_color=DANGER, hover_color=DANGER_HOVER,
            command=self.kill_interface,
        )
        self.btn_kill_iface.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        # Kill is deliberately left out of action_buttons/set_busy's disable list —
        # it's the escape hatch when something else is stuck (e.g. a hung pkexec
        # prompt), so it must stay clickable even while the app reports busy.

        self.action_buttons = [
            self.btn_random, self.btn_same_vendor, self.btn_new_vendor,
            self.btn_reset, self.btn_custom, self.btn_start_iface,
        ]

    def _format_custom_entry(self, _event):
        raw = self.custom_entry.get()
        hexchars = re.sub(r"[^0-9A-Fa-f]", "", raw)[:12]
        groups = [hexchars[i:i + 2] for i in range(0, len(hexchars), 2)]
        formatted = ":".join(groups)
        if formatted != raw:
            self.custom_entry.delete(0, "end")
            self.custom_entry.insert(0, formatted)

    def _build_log(self):
        ctk.CTkLabel(
            self, text="Activity Log", font=ctk.CTkFont(size=12, weight="bold"), text_color="#8b93a1"
        ).grid(row=4, column=0, sticky="sw", padx=24, pady=(8, 0))

        self.log_box = ctk.CTkTextbox(
            self, fg_color=CARD_BG, corner_radius=10,
            font=ctk.CTkFont(family="monospace", size=12), wrap="word",
        )
        self.log_box.grid(row=5, column=0, sticky="nsew", padx=24, pady=(4, 8))
        self.log_box.configure(state="disabled")
        for tag, color in (("ok", LOG_OK), ("err", LOG_ERR), ("info", LOG_INFO), ("warn", LOG_WARN)):
            self.log_box.tag_config(tag, foreground=color)

        self.grid_rowconfigure(5, weight=1)

    def _build_statusbar(self):
        self.status_var = ctk.StringVar(value="Ready")
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=6, column=0, sticky="ew", padx=24, pady=(0, 14))
        self.status_label = ctk.CTkLabel(
            bar, textvariable=self.status_var, font=ctk.CTkFont(size=11), text_color="#8b93a1"
        )
        self.status_label.pack(side="left")
        self.spinner = ctk.CTkProgressBar(bar, mode="indeterminate", width=140)

    # ---------- logging / state helpers ----------

    def log(self, message, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] ", "info")
        self.log_box.insert("end", message + "\n", level)
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def set_busy(self, busy, status=""):
        self.busy = busy
        for b in self.action_buttons:
            b.configure(state="disabled" if busy else "normal")
        self.iface_menu.configure(state="disabled" if busy else "normal")
        if busy:
            self.spinner.pack(side="right")
            self.spinner.start()
            self.status_var.set(status)
        else:
            self.spinner.stop()
            self.spinner.pack_forget()
            self.status_var.set(status or "Ready")

    def current_interface(self):
        iface = self.iface_var.get()
        if not iface or iface == "(no interfaces found)":
            return None
        return iface

    # ---------- interface / info refresh ----------

    def refresh_interfaces(self):
        ifaces = list_interfaces()
        if not ifaces:
            self.iface_menu.configure(values=["(no interfaces found)"])
            self.iface_var.set("(no interfaces found)")
            self.log("No network interfaces found.", "warn")
            return
        current = self.iface_var.get()
        self.iface_menu.configure(values=ifaces)
        if current not in ifaces:
            self.iface_var.set(ifaces[0])
        self.refresh_mac_info()

    def refresh_mac_info(self, after=None):
        iface = self.current_interface()
        if not iface:
            return
        if not IS_WINDOWS and not self.macchanger_path:
            return

        def work():
            if IS_WINDOWS:
                cur, perm = win_mac_info(iface)
                if cur is None:
                    self.after(0, lambda: self.log(f"Failed to read adapter info for {iface}.", "err"))
                    self.after(0, lambda: self._apply_mac_info_direct(None, None, after))
                    return
                self.after(0, lambda: self._apply_mac_info_direct(cur, perm, after))
            else:
                try:
                    result = subprocess.run(
                        [self.macchanger_path, "-s", iface],
                        capture_output=True, text=True, timeout=5,
                    )
                    self.after(0, lambda: self._apply_mac_info(result.stdout, result.stderr, after))
                except Exception as exc:
                    self.after(0, lambda: self.log(f"Failed to read MAC info: {exc}", "err"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_mac_info(self, stdout, stderr, after=None):
        m = SHOW_RE.search(stdout)
        if not m:
            self._set_mac_labels("—", "", "—", "")
            if stderr.strip():
                self.log(stderr.strip(), "err")
            if after:
                after()
            return
        cur_mac, cur_vendor, perm_mac, perm_vendor = m.groups()
        self._set_mac_labels(cur_mac, cur_vendor, perm_mac, perm_vendor)
        if after:
            after()

    def _apply_mac_info_direct(self, cur_mac, perm_mac, after=None):
        if not cur_mac:
            self._set_mac_labels("—", "", "—", "")
        else:
            self._set_mac_labels(cur_mac, "", perm_mac or cur_mac, "")
        if after:
            after()

    def _set_mac_labels(self, cur_mac, cur_vendor, perm_mac, perm_vendor):
        self.current_mac_label.configure(text=cur_mac)
        self.current_vendor_label.configure(text=cur_vendor)
        self.perm_mac_label.configure(text=perm_mac)
        self.perm_vendor_label.configure(text=perm_vendor)
        if cur_mac.lower() == perm_mac.lower():
            self.current_mac_label.configure(text_color=("gray10", "gray90"))
        else:
            self.current_mac_label.configure(text_color=SUCCESS)

    # ---------- actions ----------

    def change_mac(self, action, description):
        iface = self.current_interface()
        if not iface:
            self.log("No interface selected.", "err")
            return
        self._run_privileged(iface, action, description)

    def set_custom_mac(self):
        iface = self.current_interface()
        if not iface:
            self.log("No interface selected.", "err")
            return
        mac = self.custom_entry.get().strip()
        if not MAC_RE.match(mac):
            self.log(f"'{mac}' is not a valid MAC address (expected XX:XX:XX:XX:XX:XX).", "err")
            return
        self._run_privileged(iface, ("custom", mac), f"set the MAC to {mac}")

    def start_interface(self):
        self._run_interface_toggle("up", "bring the interface up")

    def kill_interface(self):
        # Kill is the escape hatch: first abort anything stuck (e.g. a hung pkexec
        # prompt from a previous action), un-stick the busy UI, then force the
        # interface down directly — regardless of whatever else was happening.
        if self.current_process is not None and self.current_process.poll() is None:
            try:
                self.current_process.kill()
                self.log("Sent a kill signal to the in-progress privileged command.", "warn")
            except Exception as exc:
                self.log(f"Could not kill the in-progress command: {exc}", "err")
            self.current_process = None
        if self.busy:
            self.set_busy(False)
        self._run_interface_toggle("down", "bring the interface down")

    def _run_interface_toggle(self, direction, description):
        iface = self.current_interface()
        if not iface:
            self.log("No interface selected.", "err")
            return
        if self.busy:
            return

        if IS_WINDOWS:
            self._run_windows_interface_toggle(iface, direction, description)
            return

        if not self.pkexec_path:
            self.log("pkexec is not available; cannot request privileges.", "err")
            return

        self.log(f"Requesting privileges to {description} on {iface}…", "info")
        self.set_busy(True, f"Working on {iface}…")

        def work():
            try:
                proc = subprocess.Popen(
                    [self.pkexec_path, "ip", "link", "set", "dev", iface, direction],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                self.current_process = proc
                try:
                    stdout, stderr = proc.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    self.after(0, lambda: self._on_change_error("Command timed out.", iface))
                    return
                result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
                self.after(0, lambda: self._on_toggle_done(result, iface, direction))
            except Exception as exc:
                self.after(0, lambda: self._on_change_error(str(exc), iface))
            finally:
                self.current_process = None

        threading.Thread(target=work, daemon=True).start()

    def _run_windows_interface_toggle(self, iface, direction, description):
        if not self.powershell_path:
            self.log("PowerShell is not available; cannot change the adapter.", "err")
            return

        self.log(f"Requesting Administrator access (UAC) to {description} on {iface}…", "info")
        self.set_busy(True, f"Working on {iface}…")

        def work():
            try:
                ok, message = win_toggle_adapter(iface, direction)
            except Exception as exc:
                ok, message = False, str(exc)
            self.after(0, lambda: self._on_windows_toggle_done(ok, message, iface, direction))

        threading.Thread(target=work, daemon=True).start()

    def _on_windows_toggle_done(self, ok, message, iface, direction):
        self.set_busy(False)
        if ok:
            self.log(f"{iface} is now {direction}.", "ok")
        else:
            self.log(f"Failed to bring {iface} {direction}: {message}", "err")
        self.refresh_mac_info()

    def _on_toggle_done(self, result, iface, direction):
        self.set_busy(False)
        if result.returncode == 126 or result.returncode == 127:
            self.log("Authentication cancelled or failed.", "warn")
        elif result.returncode != 0:
            self.log(f"Failed to bring {iface} {direction}: exit code {result.returncode}.", "err")
            err = (result.stderr or "").strip()
            if err:
                self.log(err, "err")
        else:
            self.log(f"{iface} is now {direction}.", "ok")
        self.refresh_mac_info()

    def _run_privileged(self, iface, action, description):
        if self.busy:
            return

        if IS_WINDOWS:
            self._run_windows_change(iface, action, description)
            return

        if not self.macchanger_path:
            self.log("macchanger is not installed.", "err")
            return
        if not self.pkexec_path:
            self.log("pkexec is not available; cannot request privileges.", "err")
            return

        if isinstance(action, tuple) and action[0] == "custom":
            flags = ["-m", action[1]]
        else:
            flags = {
                "random": ["-r"],
                "keep_vendor": ["-e"],
                "new_vendor": ["-A"],
                "reset": ["-p"],
            }[action]

        down_cmd = " ".join(shlex.quote(p) for p in ["ip", "link", "set", "dev", iface, "down"])
        mac_cmd = " ".join(shlex.quote(p) for p in [self.macchanger_path, *flags, iface])
        up_cmd = " ".join(shlex.quote(p) for p in ["ip", "link", "set", "dev", iface, "up"])
        # Capture macchanger's own exit code explicitly — the interface must come
        # back up either way, but a plain ";" chain would report ip's exit code
        # (almost always 0) and silently mask a failed macchanger invocation.
        cmd_str = f"{down_cmd} && {mac_cmd} ; rc=$? ; {up_cmd} ; exit $rc"

        self.log(f"Requesting privileges to {description} on {iface}…", "info")
        self.set_busy(True, f"Working on {iface}…")

        def work():
            try:
                proc = subprocess.Popen(
                    [self.pkexec_path, "bash", "-c", cmd_str],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                self.current_process = proc
                try:
                    stdout, stderr = proc.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    self.after(0, lambda: self._on_change_error("Command timed out.", iface))
                    return
                result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
                self.after(0, lambda: self._on_change_done(result, iface, flags))
            except Exception as exc:
                self.after(0, lambda: self._on_change_error(str(exc), iface))
            finally:
                self.current_process = None

        threading.Thread(target=work, daemon=True).start()

    def _run_windows_change(self, iface, action, description):
        if not self.powershell_path:
            self.log("PowerShell is not available; cannot change the adapter.", "err")
            return

        if isinstance(action, tuple) and action[0] == "custom":
            target_mac = action[1].upper()
        elif action == "reset":
            target_mac = None
        elif action == "random":
            target_mac = mac_to_str(random_mac_bytes(keep_prefix=None))
        elif action == "keep_vendor":
            cur = self.current_mac_label.cget("text")
            prefix = None
            if MAC_RE.match(cur):
                prefix = [int(b, 16) for b in cur.split(":")[:3]]
            target_mac = mac_to_str(random_mac_bytes(keep_prefix=prefix))
        elif action == "new_vendor":
            target_mac = mac_to_str(random_mac_bytes(keep_prefix=None))
            self.log(
                "Note: the Windows path has no vendor/OUI database, so \"new vendor\" "
                "assigns a fresh randomized address rather than a real assigned vendor prefix.",
                "warn",
            )
        else:
            self.log(f"Unknown action: {action}", "err")
            return

        self.log(f"Requesting Administrator access (UAC) to {description} on {iface}…", "info")
        self.set_busy(True, f"Working on {iface}…")

        def work():
            try:
                ok, message = win_set_mac(iface, target_mac)
            except Exception as exc:
                ok, message = False, str(exc)
            self.after(0, lambda: self._on_windows_change_done(ok, message, iface, action))

        threading.Thread(target=work, daemon=True).start()

    def _on_windows_change_done(self, ok, message, iface, action):
        self.set_busy(False)
        if ok:
            self.log(f"Success on {iface}. {message}".strip(), "ok")
        else:
            self.log(f"Failed on {iface}: {message}", "err")
        self.refresh_mac_info(
            after=lambda: self._verify_change(iface, ["-p"] if action == "reset" else [], 0 if ok else 1)
        )

    def _on_change_done(self, result, iface, flags):
        self.set_busy(False)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode == 126 or result.returncode == 127:
            self.log("Authentication cancelled or failed.", "warn")
        elif result.returncode != 0:
            self.log(f"macchanger exited with code {result.returncode}.", "err")
            if err:
                self.log(err, "err")
        else:
            self.log(f"Success on {iface}.", "ok")
        if out:
            for line in out.splitlines():
                self.log(line, "ok" if result.returncode == 0 else "info")
        self.refresh_mac_info(after=lambda: self._verify_change(iface, flags, result.returncode))

    def _verify_change(self, iface, flags, returncode):
        if returncode != 0:
            return
        cur = self.current_mac_label.cget("text")
        perm = self.perm_mac_label.cget("text")
        if "-p" in flags and cur.lower() != perm.lower():
            self.log(
                "Reset reported success, but the current MAC still differs from the "
                "permanent one — this NIC's driver likely doesn't support restoring "
                "the burned-in address while up. Some drivers need the interface "
                "reset (unplug/replug, or on Linux `modprobe -r <driver> && modprobe <driver>`).",
                "warn",
            )

    def _on_change_error(self, message, iface):
        self.set_busy(False)
        self.log(f"Error changing MAC on {iface}: {message}", "err")
        self.refresh_mac_info()


if __name__ == "__main__":
    app = MacChangerApp()
    app.mainloop()
