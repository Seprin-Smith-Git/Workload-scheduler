"""
Dynamic XGBoost Workload Scheduler - GUI

Run:
    python scheduler2_gui.py

Requirements:
    pip install requests xgboost matplotlib

The GUI:
- Lets you choose the number of client/worker devices.
- Accepts one IP address per client.
- Detects the client OS from /info.
- Polls CPU/RAM utilization.
- Uses the existing xgboost_model.json for worker selection.
- Runs the remote /run endpoint.
- Displays the scheduler response and a live transparent-blue comparison graph.
- Saves the configured clients to scheduler_workers.json.
- "Export to scheduler2.py" updates the WORKERS section in an existing scheduler2.py.
"""

import json
import os
import re
import socket
import subprocess
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import xgboost as xgb
except ImportError:
    xgb = None

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "xgboost_model.json"
CONFIG_PATH = BASE_DIR / "scheduler_workers.json"
SCHEDULER_PATH = BASE_DIR / "scheduler2.py"


# ----------------------------------------------------------------------
# Worker/model logic
# ----------------------------------------------------------------------

def load_model():
    if xgb is None:
        raise RuntimeError(
            "XGBoost is not installed. Run: pip install xgboost"
        )

    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Model not found:\n{MODEL_PATH}\n"
            "Place xgboost_model.json beside this file."
        )

    model = xgb.XGBClassifier()
    model.load_model(str(MODEL_PATH))
    return model


def get_info(worker):
    try:
        r = requests.get(worker["url"] + "/info", timeout=3)
        r.raise_for_status()
        data = r.json()

        return {
            "os": data.get("os", "Unknown"),
            "cpu": float(data.get("cpu", 0)),
            "memory": float(data.get("memory", 0)),
            "network": float(data.get("network", 0)),
        }
    except Exception as exc:
        return {"error": str(exc)}


def prepare_features(info):
    return [[
        float(info.get("cpu", 0)),
        float(info.get("memory", 0)),
        float(info.get("network", 0)),
    ]]


def worker_probability(model, worker, info):
    probabilities = model.predict_proba(prepare_features(info))[0]

    # Existing project model:
    # Class 0 = Windows
    # Class 1 = Kali
    windows_probability = float(probabilities[0])
    kali_probability = float(probabilities[1])

    os_name = worker.get("os", "").lower()

    if os_name == "windows":
        selected_probability = windows_probability
    elif os_name in ("linux", "kali"):
        selected_probability = kali_probability
    else:
        # Keep the original project's named-worker behavior as a fallback.
        name = worker.get("name", "").lower()
        selected_probability = (
            kali_probability if "kali" in name else windows_probability
        )

    return {
        "windows": windows_probability,
        "kali": kali_probability,
        "selected": selected_probability,
    }


def find_best_worker(model, available):
    best = None
    best_probability = -1.0

    for item in available:
        probs = worker_probability(model, item["worker"], item["info"])
        item["probabilities"] = probs

        if probs["selected"] > best_probability:
            best_probability = probs["selected"]
            best = item

    return best


# ----------------------------------------------------------------------
# Persistence / scheduler.py export
# ----------------------------------------------------------------------

def normalize_workers(rows):
    workers = []

    for i, row in enumerate(rows, start=1):
        ip = row.get("ip", "").strip()
        if not ip:
            continue

        workers.append({
            "name": row.get("name") or f"Client-{i}",
            "url": f"http://{ip}:5000",
        })

    return workers


def save_config(workers):
    CONFIG_PATH.write_text(
        json.dumps(workers, indent=4),
        encoding="utf-8"
    )


def load_config():
    if not CONFIG_PATH.exists():
        return []

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass

    return []


def export_workers_to_scheduler(workers, target=SCHEDULER_PATH):
    """
    Replace the WORKERS list in scheduler2.py.

    This keeps the original scheduler structure intact while making the
    GUI-managed IP list available to the command-line scheduler as well.
    """
    if not target.exists():
        raise FileNotFoundError(f"Scheduler file not found: {target}")

    source = target.read_text(encoding="utf-8")

    replacement = (
        "# ============================================================\n"
        "# WORKERS\n"
        "# ============================================================\n\n"
        "workers = " + repr(workers) + "\n"
    )

    pattern = (
        r"# ============================================================\s*"
        r"# WORKERS\s*"
        r"# ============================================================\s*"
        r"workers\s*=\s*\[.*?\]\s*"
    )

    updated, count = re.subn(
        pattern,
        replacement,
        source,
        count=1,
        flags=re.DOTALL
    )

    if count != 1:
        raise RuntimeError(
            "Could not locate the WORKERS section in scheduler2.py. "
            "Make a backup and check the section header."
        )

    target.write_text(updated, encoding="utf-8")


# ----------------------------------------------------------------------
# LAN discovery
# ----------------------------------------------------------------------

def get_local_subnets():
    """
    Determine likely local IPv4 /24 networks without requiring nmap.
    Returns networks such as 192.168.1.0/24.
    """
    subnets = set()

    # Primary route interface/address.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        local_ip = probe.getsockname()[0]
        probe.close()

        parts = local_ip.split(".")
        if len(parts) == 4:
            subnets.add(".".join(parts[:3]) + ".0/24")
    except Exception:
        pass

    # Hostname-resolved addresses can catch another active interface.
    try:
        host = socket.gethostname()
        for addr in socket.gethostbyname_ex(host)[2]:
            parts = addr.split(".")
            if len(parts) == 4 and not addr.startswith("127."):
                subnets.add(".".join(parts[:3]) + ".0/24")
    except Exception:
        pass

    return sorted(subnets)


def discover_lan_ips():
    """
    Discover live LAN IPv4 addresses.

    Preferred method:
      nmap -sn <subnet>

    Fallback:
      Python TCP connect scan against common HTTP/Flask port 5000.
      This fallback is intentionally conservative because a pure Python
      ARP implementation would require extra platform-specific privileges.
    """
    discovered = set()
    subnets = get_local_subnets()

    # Try nmap first.
    nmap = shutil.which("nmap") if "shutil" in globals() else None
    if nmap:
        for subnet in subnets:
            try:
                result = subprocess.run(
                    [nmap, "-sn", "-n", subnet],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                for line in result.stdout.splitlines():
                    match = re.search(
                        r"Nmap scan report for (?:[^\s(]+\s+\()?(\d+\.\d+\.\d+\.\d+)",
                        line
                    )
                    if match:
                        discovered.add(match.group(1))
            except Exception:
                continue

    # If nmap isn't installed or found nothing, use a Python fallback.
    if not discovered:
        local_ips = set()
        for subnet in subnets:
            base = subnet.split("/")[0].rsplit(".", 1)[0]
            for last in range(1, 255):
                local_ips.add(f"{base}.{last}")

        def check_ip(ip):
            # Port 5000 is especially useful for this project because
            # your resource agents expose Flask on that port.
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.18)
                ok = sock.connect_ex((ip, 5000)) == 0
                sock.close()
                if ok:
                    return ip
            except Exception:
                pass
            return None

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=64) as pool:
            for ip in pool.map(check_ip, sorted(local_ips)):
                if ip:
                    discovered.add(ip)

    return sorted(
        discovered,
        key=lambda ip: tuple(int(x) for x in ip.split("."))
    )


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class SchedulerGUI(tk.Tk):
    BG = "#070b12"
    PANEL = "#0d1420"
    PANEL_2 = "#111b29"
    TEXT = "#e8f0ff"
    MUTED = "#8fa3bd"
    BLUE = "#35a7ff"
    BLUE_2 = "#1976d2"
    GREEN = "#36d399"
    RED = "#ff5c70"
    BORDER = "#1d2b3d"

    def __init__(self):
        super().__init__()

        self.title("Dynamic XGBoost Workload Scheduler")
        self.geometry("1320x820")
        self.minsize(1100, 720)
        self.configure(bg=self.BG)

        self.model = None
        self.client_rows = []
        self.polling = False
        self.history = {}
        self.max_history = 30
        self.last_snapshot = []
        self.discovered_ips = []
        self.discovery_running = False

        self._configure_style()
        self._build_header()
        self._build_config_panel()
        self._build_dashboard()
        self._build_graph()
        self._build_footer()

        existing = load_config()
        if existing:
            self._set_from_config(existing)
        else:
            self.count_var.set("2")
            self._rebuild_client_inputs()

        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ---------------- UI helpers ----------------

    def _configure_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(
            "TFrame",
            background=self.BG
        )
        style.configure(
            "Panel.TFrame",
            background=self.PANEL
        )
        style.configure(
            "TLabel",
            background=self.PANEL,
            foreground=self.TEXT,
            font=("Segoe UI", 10)
        )
        style.configure(
            "Title.TLabel",
            background=self.BG,
            foreground=self.TEXT,
            font=("Segoe UI Semibold", 22)
        )
        style.configure(
            "Sub.TLabel",
            background=self.BG,
            foreground=self.MUTED,
            font=("Segoe UI", 10)
        )
        style.configure(
            "PanelTitle.TLabel",
            background=self.PANEL,
            foreground=self.TEXT,
            font=("Segoe UI Semibold", 12)
        )
        style.configure(
            "TButton",
            background=self.PANEL_2,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            focusthickness=0,
            padding=(12, 8),
            font=("Segoe UI Semibold", 9)
        )
        style.map(
            "TButton",
            background=[("active", self.BLUE_2)],
            foreground=[("active", "#ffffff")]
        )
        style.configure(
            "Accent.TButton",
            background=self.BLUE_2,
            foreground="#ffffff",
            bordercolor=self.BLUE_2,
            padding=(15, 9),
            font=("Segoe UI Semibold", 9)
        )
        style.map(
            "Accent.TButton",
            background=[("active", self.BLUE)]
        )
        style.configure(
            "TEntry",
            fieldbackground=self.PANEL_2,
            foreground=self.TEXT,
            insertcolor=self.TEXT,
            bordercolor=self.BORDER,
            padding=7
        )
        style.configure(
            "Treeview",
            background=self.PANEL_2,
            fieldbackground=self.PANEL_2,
            foreground=self.TEXT,
            rowheight=30,
            bordercolor=self.BORDER,
            font=("Segoe UI", 9)
        )
        style.configure(
            "Treeview.Heading",
            background="#162235",
            foreground=self.TEXT,
            relief="flat",
            font=("Segoe UI Semibold", 9)
        )
        style.map(
            "Treeview",
            background=[("selected", "#164d80")],
            foreground=[("selected", "#ffffff")]
        )

    def _build_header(self):
        header = tk.Frame(self, bg=self.BG)
        header.pack(fill="x", padx=24, pady=(20, 8))

        left = tk.Frame(header, bg=self.BG)
        left.pack(side="left")

        ttk.Label(
            left,
            text="DYNAMIC XGBOOST WORKLOAD SCHEDULER",
            style="Title.TLabel"
        ).pack(anchor="w")

        ttk.Label(
            left,
            text="Resource-aware client selection • Flask agents • live telemetry",
            style="Sub.TLabel"
        ).pack(anchor="w", pady=(3, 0))

        self.status_var = tk.StringVar(value="● READY")
        self.status_label = tk.Label(
            header,
            textvariable=self.status_var,
            bg=self.BG,
            fg=self.GREEN,
            font=("Segoe UI Semibold", 10)
        )
        self.status_label.pack(side="right", pady=12)

    def _build_config_panel(self):
        panel = ttk.Frame(self, style="Panel.TFrame")
        panel.pack(fill="x", padx=24, pady=8)

        top = tk.Frame(panel, bg=self.PANEL)
        top.pack(fill="x", padx=16, pady=(13, 7))

        ttk.Label(
            top,
            text="CLIENT CONFIGURATION",
            style="PanelTitle.TLabel"
        ).pack(side="left")

        ttk.Label(
            top,
            text="  Each client must expose Flask on port 5000",
            style="TLabel"
        ).pack(side="left")

        controls = tk.Frame(panel, bg=self.PANEL)
        controls.pack(fill="x", padx=16, pady=(0, 12))

        ttk.Label(
            controls,
            text="Client count:"
        ).pack(side="left")

        self.count_var = tk.StringVar(value="2")
        count_entry = ttk.Entry(
            controls,
            textvariable=self.count_var,
            width=7
        )
        count_entry.pack(side="left", padx=(7, 10))

        ttk.Button(
            controls,
            text="Apply Count",
            command=self._rebuild_client_inputs
        ).pack(side="left")

        ttk.Button(
            controls,
            text="🔎 Scan LAN",
            style="Accent.TButton",
            command=self.scan_lan
        ).pack(side="left", padx=(8, 4))

        ttk.Button(
            controls,
            text="Save + Export to scheduler2.py",
            command=self._save_and_export
        ).pack(side="left", padx=4)

        ttk.Button(
            controls,
            text="Refresh Now",
            command=self.refresh_now
        ).pack(side="right")

        self.discovery_var = tk.StringVar(
            value="LAN scan: not run — click Scan LAN to discover devices"
        )
        tk.Label(
            panel,
            textvariable=self.discovery_var,
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 8)
        ).pack(anchor="w", padx=16, pady=(0, 6))

        self.client_input_frame = tk.Frame(panel, bg=self.PANEL)
        self.client_input_frame.pack(fill="x", padx=16, pady=(0, 15))

    def _rebuild_client_inputs(self):
        try:
            count = int(self.count_var.get())
            if count < 1 or count > 32:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid client count",
                "Enter a whole number from 1 to 32."
            )
            return

        old = [
            (row["name_var"].get(), row["ip_var"].get())
            for row in self.client_rows
        ]

        for widget in self.client_input_frame.winfo_children():
            widget.destroy()

        self.client_rows = []

        for i in range(count):
            name = old[i][0] if i < len(old) and old[i][0] else f"Client-{i+1}"
            ip = old[i][1] if i < len(old) else ""

            card = tk.Frame(
                self.client_input_frame,
                bg=self.PANEL_2,
                highlightbackground=self.BORDER,
                highlightthickness=1
            )
            card.pack(
                side="left",
                fill="x",
                expand=True,
                padx=(0 if i == 0 else 5, 5)
            )

            tk.Label(
                card,
                text=f"CLIENT {i+1}",
                bg=self.PANEL_2,
                fg=self.BLUE,
                font=("Segoe UI Semibold", 9)
            ).pack(anchor="w", padx=10, pady=(8, 3))

            name_var = tk.StringVar(value=name)
            ip_var = tk.StringVar(value=ip)

            ttk.Entry(
                card,
                textvariable=name_var
            ).pack(fill="x", padx=10, pady=(0, 4))

            # Hidden selection bar: it is a compact combobox that expands
            # only when the user clicks the IP field.
            ip_combo = ttk.Combobox(
                card,
                textvariable=ip_var,
                values=self._available_ip_values(ip),
                state="normal"
            )
            ip_combo.pack(fill="x", padx=10, pady=(0, 9))

            ip_combo.bind(
                "<<ComboboxSelected>>",
                lambda event, row_index=i: self._ip_selected(row_index)
            )
            ip_combo.bind(
                "<Button-1>",
                lambda event, row_index=i: self._refresh_combo_values(row_index)
            )

            self.client_rows.append({
                "name_var": name_var,
                "ip_var": ip_var,
                "ip_combo": ip_combo,
            })

    def _available_ip_values(self, current_ip=""):
        used = {
            row["ip_var"].get().strip()
            for row in self.client_rows
            if row["ip_var"].get().strip()
        }

        # Keep the current row's existing value visible while editing it.
        values = [
            ip for ip in self.discovered_ips
            if ip not in used or ip == current_ip
        ]

        if current_ip and current_ip not in values:
            values.insert(0, current_ip)

        return values

    def _refresh_combo_values(self, row_index):
        if row_index >= len(self.client_rows):
            return

        combo = self.client_rows[row_index]["ip_combo"]
        current = self.client_rows[row_index]["ip_var"].get().strip()
        combo["values"] = self._available_ip_values(current)

    def _ip_selected(self, row_index):
        selected = self.client_rows[row_index]["ip_var"].get().strip()

        # Prevent duplicate selection immediately.
        duplicates = [
            i for i, row in enumerate(self.client_rows)
            if i != row_index and row["ip_var"].get().strip() == selected
        ]

        if duplicates:
            self.client_rows[row_index]["ip_var"].set("")
            self._refresh_combo_values(row_index)
            messagebox.showwarning(
                "IP already selected",
                f"{selected} is already assigned to another client."
            )
            return

        # Refresh every other combo so this IP disappears from their list.
        for i, row in enumerate(self.client_rows):
            if i != row_index:
                current = row["ip_var"].get().strip()
                row["ip_combo"]["values"] = self._available_ip_values(current)

    def scan_lan(self):
        if self.discovery_running:
            return

        self.discovery_running = True
        self._set_status("● SCANNING LAN...", self.BLUE)
        self.discovery_var.set(
            "LAN scan: discovering active devices..."
        )

        threading.Thread(
            target=self._scan_lan_thread,
            daemon=True
        ).start()

    def _scan_lan_thread(self):
        try:
            ips = discover_lan_ips()
            self.after(
                0,
                lambda: self._apply_discovered_ips(ips)
            )
        except Exception as exc:
            self.after(
                0,
                lambda: self._lan_scan_failed(exc)
            )

    def _apply_discovered_ips(self, ips):
        self.discovery_running = False
        self.discovered_ips = ips

        for row in self.client_rows:
            current = row["ip_var"].get().strip()
            row["ip_combo"]["values"] = self._available_ip_values(current)

        self.discovery_var.set(
            f"LAN scan: {len(ips)} device(s) discovered — "
            "click an IP field to choose"
        )

        self._set_status(
            f"● {len(ips)} LAN DEVICES FOUND",
            self.GREEN if ips else self.RED
        )

        if ips:
            self._write_output(
                "LAN discovery completed.\n"
                "Discovered IPs: " + ", ".join(ips) + "\n"
            )
        else:
            self._write_output(
                "LAN discovery found no devices.\n"
                "Install nmap for reliable -sn discovery, or verify "
                "that the machine is connected to the LAN.\n"
            )

    def _lan_scan_failed(self, exc):
        self.discovery_running = False
        self._set_status("● LAN SCAN ERROR", self.RED)
        self.discovery_var.set("LAN scan failed")
        self._write_output(f"LAN scan error: {exc}\n")

    def _build_dashboard(self):
        wrapper = tk.Frame(self, bg=self.BG)
        wrapper.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        left = ttk.Frame(wrapper, style="Panel.TFrame")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        right = ttk.Frame(wrapper, style="Panel.TFrame")
        right.pack(side="right", fill="both", expand=True, padx=(8, 0))

        ttk.Label(
            left,
            text="CLIENT RESOURCE COMPARISON",
            style="PanelTitle.TLabel"
        ).pack(anchor="w", padx=14, pady=(12, 7))

        columns = ("client", "ip", "os", "cpu", "ram", "score", "state")
        self.tree = ttk.Treeview(
            left,
            columns=columns,
            show="headings",
            height=10
        )

        headings = {
            "client": "CLIENT",
            "ip": "IP",
            "os": "OS",
            "cpu": "CPU %",
            "ram": "RAM %",
            "score": "XGB SCORE",
            "state": "STATE"
        }

        widths = {
            "client": 105,
            "ip": 130,
            "os": 80,
            "cpu": 70,
            "ram": 70,
            "score": 90,
            "state": 85
        }

        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center")

        self.tree.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        ttk.Label(
            right,
            text="SCHEDULER DECISION",
            style="PanelTitle.TLabel"
        ).pack(anchor="w", padx=14, pady=(12, 7))

        self.selected_var = tk.StringVar(value="No client selected")
        tk.Label(
            right,
            textvariable=self.selected_var,
            bg=self.PANEL,
            fg=self.BLUE,
            font=("Segoe UI Semibold", 20)
        ).pack(anchor="w", padx=16, pady=(8, 2))

        self.score_var = tk.StringVar(value="XGBoost score: —")
        tk.Label(
            right,
            textvariable=self.score_var,
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Segoe UI", 10)
        ).pack(anchor="w", padx=16, pady=(0, 10))

        ttk.Label(
            right,
            text="Task command",
            style="TLabel"
        ).pack(anchor="w", padx=16)

        self.command_var = tk.StringVar(value="python3 --version")
        ttk.Entry(
            right,
            textvariable=self.command_var
        ).pack(fill="x", padx=16, pady=(5, 9))

        ttk.Button(
            right,
            text="RUN TASK ON SELECTED CLIENT",
            style="Accent.TButton",
            command=self.run_selected_task
        ).pack(fill="x", padx=16, pady=(2, 8))

        ttk.Button(
            right,
            text="AUTO SELECT + RUN TASK",
            command=self.auto_select_and_run
        ).pack(fill="x", padx=16, pady=(0, 12))

        self.output_text = tk.Text(
            right,
            bg="#08101a",
            fg="#cfe4ff",
            insertbackground=self.TEXT,
            relief="flat",
            wrap="word",
            font=("Consolas", 9),
            height=9
        )
        self.output_text.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.output_text.insert(
            "end",
            "Scheduler response will appear here...\n"
        )
        self.output_text.configure(state="disabled")

    def _build_graph(self):
        graph_panel = ttk.Frame(self, style="Panel.TFrame")
        graph_panel.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        ttk.Label(
            graph_panel,
            text="LIVE CPU / RAM UTILIZATION",
            style="PanelTitle.TLabel"
        ).pack(anchor="w", padx=14, pady=(10, 2))

        self.figure = Figure(
            figsize=(8, 2.7),
            dpi=100,
            facecolor=self.PANEL
        )
        self.ax = self.figure.add_subplot(111)
        self.ax.set_facecolor(self.PANEL)
        self.ax.tick_params(colors=self.MUTED, labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color(self.BORDER)

        self.ax.set_ylim(0, 100)
        self.ax.set_ylabel("Utilization %", color=self.MUTED, fontsize=9)
        self.ax.set_xlabel("Sample", color=self.MUTED, fontsize=9)
        self.ax.grid(alpha=0.12, color="white")

        self.canvas = FigureCanvasTkAgg(
            self.figure,
            master=graph_panel
        )
        self.canvas.get_tk_widget().pack(
            fill="both",
            expand=True,
            padx=10,
            pady=(0, 10)
        )

    def _build_footer(self):
        footer = tk.Frame(self, bg=self.BG)
        footer.pack(fill="x", padx=24, pady=(0, 14))

        self.last_update_var = tk.StringVar(value="Last update: —")
        tk.Label(
            footer,
            textvariable=self.last_update_var,
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 8)
        ).pack(side="left")

        tk.Label(
            footer,
            text="XGBoost • Flask • psutil",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 8)
        ).pack(side="right")

    # ---------------- Data/config ----------------

    def _set_from_config(self, workers):
        self.count_var.set(str(len(workers)))

        # Temporarily create correct number of rows.
        self._rebuild_client_inputs()

        for row, worker in zip(self.client_rows, workers):
            row["name_var"].set(worker.get("name", "Client"))
            url = worker.get("url", "")
            ip = url.replace("http://", "").replace("https://", "")
            ip = ip.rsplit(":", 1)[0]
            row["ip_var"].set(ip)

    def _current_workers(self):
        rows = []
        for i, row in enumerate(self.client_rows, start=1):
            name = row["name_var"].get().strip() or f"Client-{i}"
            ip = row["ip_var"].get().strip()

            if not ip:
                continue

            rows.append({
                "name": name,
                "url": f"http://{ip}:5000"
            })

        return rows

    def _save_and_export(self):
        workers = self._current_workers()

        if not workers:
            messagebox.showwarning(
                "No clients",
                "Enter at least one client IP address."
            )
            return

        try:
            save_config(workers)
            export_workers_to_scheduler(workers)
        except Exception as exc:
            messagebox.showerror(
                "Save failed",
                str(exc)
            )
            return

        self._set_status("● CONFIGURATION SAVED", self.GREEN)
        self._write_output(
            "Configuration saved to scheduler_workers.json\n"
            f"Workers exported to: {SCHEDULER_PATH}\n"
        )

    # ---------------- Polling / graph ----------------

    def refresh_now(self):
        workers = self._current_workers()

        if not workers:
            self._write_output("Enter at least one client IP address.\n")
            return

        self._set_status("● CHECKING CLIENTS...", self.BLUE)

        threading.Thread(
            target=self._refresh_worker_thread,
            args=(workers,),
            daemon=True
        ).start()

    def _refresh_worker_thread(self, workers):
        results = []

        for worker in workers:
            info = get_info(worker)
            results.append({
                "worker": worker,
                "info": info
            })

        self.after(
            0,
            lambda: self._apply_snapshot(results)
        )

    def _apply_snapshot(self, results):
        self.last_snapshot = results

        for child in self.tree.get_children():
            self.tree.delete(child)

        for item in results:
            worker = item["worker"]
            info = item["info"]

            if "error" in info:
                self.tree.insert(
                    "",
                    "end",
                    iid=worker["url"],
                    values=(
                        worker["name"],
                        worker["url"].replace("http://", ""),
                        "—",
                        "—",
                        "—",
                        "—",
                        "OFFLINE"
                    )
                )
                continue

            self.tree.insert(
                "",
                "end",
                iid=worker["url"],
                values=(
                    worker["name"],
                    worker["url"].replace("http://", ""),
                    info["os"],
                    f"{info['cpu']:.1f}",
                    f"{info['memory']:.1f}",
                    "—",
                    "ONLINE"
                )
            )

            name = worker["name"]
            self.history.setdefault(name, {"cpu": [], "ram": []})
            self.history[name]["cpu"].append(info["cpu"])
            self.history[name]["ram"].append(info["memory"])

            self.history[name]["cpu"] = self.history[name]["cpu"][-self.max_history:]
            self.history[name]["ram"] = self.history[name]["ram"][-self.max_history:]

        self._redraw_graph()

        online = sum(
            1 for item in results if "error" not in item["info"]
        )

        self._set_status(
            f"● {online}/{len(results)} CLIENTS ONLINE",
            self.GREEN if online else self.RED
        )
        self.last_update_var.set(
            "Last update: " + datetime.now().strftime("%H:%M:%S")
        )

    def _redraw_graph(self):
        self.ax.clear()
        self.ax.set_facecolor(self.PANEL)

        self.ax.set_ylim(0, 100)
        self.ax.set_ylabel("Utilization %", color=self.MUTED, fontsize=9)
        self.ax.set_xlabel("Sample", color=self.MUTED, fontsize=9)
        self.ax.tick_params(colors=self.MUTED, labelsize=8)
        self.ax.grid(alpha=0.12, color="white")

        for spine in self.ax.spines.values():
            spine.set_color(self.BORDER)

        first = True
        for name, data in self.history.items():
            if not data["cpu"]:
                continue

            x = list(range(len(data["cpu"])))

            # Transparent blue CPU fill/line.
            self.ax.plot(
                x,
                data["cpu"],
                color="#35a7ff",
                linewidth=1.8,
                label=f"{name} CPU"
            )
            self.ax.fill_between(
                x,
                data["cpu"],
                alpha=0.12,
                color="#35a7ff"
            )

            # RAM is shown as a dashed blue trace so CPU/RAM remain
            # distinguishable while preserving the requested blue theme.
            self.ax.plot(
                x,
                data["ram"],
                color="#65c5ff",
                linewidth=1.2,
                linestyle="--",
                alpha=0.65,
                label=f"{name} RAM"
            )

            first = False

        if not first:
            legend = self.ax.legend(
                loc="upper left",
                fontsize=7,
                frameon=False
            )
            for text in legend.get_texts():
                text.set_color(self.TEXT)

        self.figure.tight_layout(pad=1.1)
        self.canvas.draw_idle()

    # ---------------- Scheduling ----------------

    def _online_snapshot(self):
        return [
            item for item in self.last_snapshot
            if "error" not in item["info"]
        ]

    def auto_select_and_run(self):
        workers = self._current_workers()

        if not workers:
            messagebox.showwarning(
                "No clients",
                "Enter client IP addresses first."
            )
            return

        self._set_status("● XGBOOST SELECTING...", self.BLUE)

        threading.Thread(
            target=self._auto_select_thread,
            args=(workers,),
            daemon=True
        ).start()

    def _auto_select_thread(self, workers):
        try:
            model = load_model()
        except Exception as exc:
            self.after(
                0,
                lambda: self._show_error("Model error", str(exc))
            )
            return

        available = []

        for worker in workers:
            info = get_info(worker)
            if "error" not in info:
                available.append({
                    "worker": worker,
                    "info": info
                })

        if not available:
            self.after(
                0,
                lambda: self._show_error(
                    "No clients available",
                    "None of the configured Flask agents responded on port 5000."
                )
            )
            return

        best = find_best_worker(model, available)

        self.after(
            0,
            lambda: self._selected_and_run(best)
        )

    def _selected_and_run(self, best):
        if not best:
            self._show_error("Selection failed", "XGBoost did not select a client.")
            return

        worker = best["worker"]
        info = best["info"]
        probs = best["probabilities"]

        # Refresh the table with the latest model-selection score.
        for item in self.last_snapshot:
            if item["worker"]["url"] == worker["url"]:
                break

        self.selected_var.set(
            f"{worker['name']}  •  {info['os']}"
        )
        self.score_var.set(
            "XGBoost score: "
            f"{probs['selected'] * 100:.2f}%  |  "
            f"Windows: {probs['windows'] * 100:.2f}%  |  "
            f"Kali: {probs['kali'] * 100:.2f}%"
        )

        self._write_output(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"Selected {worker['name']} ({worker['url']})\n"
            f"OS: {info['os']}\n"
            f"CPU: {info['cpu']:.1f}% | RAM: {info['memory']:.1f}%\n"
            f"XGBoost selected-class probability: "
            f"{probs['selected'] * 100:.2f}%\n\n"
        )

        # Run the existing Flask /run endpoint.
        self._set_status("● RUNNING REMOTE TASK...", self.BLUE)

        threading.Thread(
            target=self._run_task_thread,
            args=(worker,),
            daemon=True
        ).start()

    def run_selected_task(self):
        selection = self.tree.selection()

        if not selection:
            messagebox.showinfo(
                "Select a client",
                "Select an ONLINE client from the table first."
            )
            return

        url = selection[0]
        worker = next(
            (w for w in self._current_workers() if w["url"] == url),
            None
        )

        if not worker:
            return

        self.selected_var.set(worker["name"])
        self._set_status("● RUNNING REMOTE TASK...", self.BLUE)

        threading.Thread(
            target=self._run_task_thread,
            args=(worker,),
            daemon=True
        ).start()

    def _run_task_thread(self, worker):
        try:
            # The supplied Linux and Windows resource agents currently
            # ignore the JSON command and execute their OS-specific task
            # file from the /run endpoint. Therefore the GUI calls /run
            # directly, preserving the project's current behavior.
            response = requests.post(
                worker["url"] + "/run",
                json={"command": self.command_var.get().strip()},
                timeout=90
            )
            response.raise_for_status()
            result = response.json()

            self.after(
                0,
                lambda: self._show_task_result(worker, result)
            )

        except Exception as exc:
            self.after(
                0,
                lambda: self._show_error(
                    "Task execution failed",
                    f"{worker['name']} ({worker['url']})\n\n{exc}"
                )
            )

    def _show_task_result(self, worker, result):
        success = result.get("success", False)
        output = result.get("output", "")
        error = result.get("error", "")
        return_code = result.get("return_code", "—")

        self._write_output(
            "\n" + "=" * 68 + "\n"
            f"REMOTE TASK RESULT — {worker['name']}\n"
            + "=" * 68 + "\n"
            f"Success: {success}\n"
            f"Return code: {return_code}\n\n"
            f"OUTPUT:\n{output}\n"
            f"ERROR:\n{error}\n"
        )

        self._set_status(
            "● TASK COMPLETED" if success else "● TASK FAILED",
            self.GREEN if success else self.RED
        )

    # ---------------- Misc ----------------

    def _write_output(self, text):
        self.output_text.configure(state="normal")
        self.output_text.insert("end", text)
        self.output_text.see("end")
        self.output_text.configure(state="disabled")

    def _show_error(self, title, message):
        self._set_status("● ERROR", self.RED)
        self._write_output(f"\nERROR: {message}\n")
        messagebox.showerror(title, message)

    def _set_status(self, text, color):
        self.status_var.set(text)
        self.status_label.configure(fg=color)


def main():
    app = SchedulerGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
