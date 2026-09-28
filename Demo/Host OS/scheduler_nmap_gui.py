"""
Dynamic XGBoost Workload Scheduler - GUI
========================================

Run:
    python scheduler_nmap_gui.py

Requirements:
    pip install requests xgboost matplotlib

Optional:
    nmap - recommended for LAN discovery

Features:
- Dark dashboard
- LAN discovery using nmap -sn, with a Python Flask-port fallback
- Dynamic client count
- Dropdown IP selection with duplicate prevention
- Client / Flask-agent health checks
- CPU/RAM/network telemetry
- XGBoost model loading and probability-based worker selection
- Robust Tkinter thread/callback handling
- Manual and automatic task execution
- Live transparent-blue CPU/RAM comparison graph
- Resource gauges / summary cards
- Saves configuration
- Exports worker list into scheduler2.py
"""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
SCHEDULER_PATH = BASE_DIR / "scheduler_nmap_gui.py"

FLASK_PORT = 5000
HTTP_TIMEOUT = 3
TASK_TIMEOUT = 90
MAX_CLIENTS = 32
HISTORY_LENGTH = 40


# ----------------------------------------------------------------------
# XGBoost
# ----------------------------------------------------------------------

def load_model():
    if xgb is None:
        raise RuntimeError(
            "XGBoost is not installed.\n\n"
            "Run:\n"
            "pip install xgboost"
        )

    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"XGBoost model not found:\n{MODEL_PATH}\n\n"
            "Place xgboost_model.json beside this GUI."
        )

    model = xgb.XGBClassifier()
    model.load_model(str(MODEL_PATH))

    if not hasattr(model, "classes_") or len(model.classes_) < 2:
        # XGBoost JSON models can still predict correctly even when the
        # sklearn wrapper does not expose classes_ in the expected way.
        pass

    return model


def prepare_features(info):
    """
    Feature order must match the trained model:
        CPU, memory, network
    """
    return [[
        float(info.get("cpu", 0.0)),
        float(info.get("memory", 0.0)),
        float(info.get("network", 0.0)),
    ]]


def worker_probability(model, worker, info):
    probabilities = model.predict_proba(
        prepare_features(info)
    )[0]

    if len(probabilities) < 2:
        raise RuntimeError(
            "The XGBoost model does not contain two classes."
        )

    # Existing project convention:
    # class 0 = Windows
    # class 1 = Kali/Linux
    windows_probability = float(probabilities[0])
    kali_probability = float(probabilities[1])

    os_name = str(
        info.get("os", worker.get("os", ""))
    ).strip().lower()

    if "windows" in os_name:
        selected_probability = windows_probability
        selected_class = "Windows"
    elif os_name in ("linux", "kali") or "linux" in os_name:
        selected_probability = kali_probability
        selected_class = "Kali/Linux"
    else:
        name = worker.get("name", "").lower()
        if "kali" in name or "linux" in name:
            selected_probability = kali_probability
            selected_class = "Kali/Linux"
        else:
            selected_probability = windows_probability
            selected_class = "Windows"

    return {
        "windows": windows_probability,
        "kali": kali_probability,
        "selected": selected_probability,
        "selected_class": selected_class,
    }


def find_best_worker(model, available):
    best = None
    best_probability = -1.0

    for item in available:
        probs = worker_probability(
            model,
            item["worker"],
            item["info"]
        )

        item["probabilities"] = probs

        if probs["selected"] > best_probability:
            best_probability = probs["selected"]
            best = item

    return best


# ----------------------------------------------------------------------
# Network / LAN discovery
# ----------------------------------------------------------------------

def get_local_subnets():
    subnets = set()

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()

        parts = local_ip.split(".")
        if len(parts) == 4:
            subnets.add(
                ".".join(parts[:3]) + ".0/24"
            )
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        addresses = socket.gethostbyname_ex(hostname)[2]

        for address in addresses:
            parts = address.split(".")
            if len(parts) == 4 and not address.startswith("127."):
                subnets.add(
                    ".".join(parts[:3]) + ".0/24"
                )
    except Exception:
        pass

    return sorted(subnets)


def discover_with_nmap():
    nmap = shutil.which("nmap")

    if not nmap:
        return []

    found = set()

    for subnet in get_local_subnets():
        try:
            result = subprocess.run(
                [
                    nmap,
                    "-sn",
                    "-n",
                    subnet
                ],
                capture_output=True,
                text=True,
                timeout=30
            )

            for line in result.stdout.splitlines():
                match = re.search(
                    r"Nmap scan report for "
                    r"(?:[^\s(]+\s+\()?(\d+\.\d+\.\d+\.\d+)",
                    line
                )

                if match:
                    found.add(match.group(1))

        except Exception:
            continue

    return sorted(
        found,
        key=lambda ip: tuple(
            int(x) for x in ip.split(".")
        )
    )


def discover_flask_agents():
    """
    Fallback discovery.

    It checks TCP/5000 because the project's client resource agents
    expose Flask on port 5000.
    """
    addresses = set()

    for subnet in get_local_subnets():
        base = subnet.split("/")[0].rsplit(".", 1)[0]

        for number in range(1, 255):
            addresses.add(
                f"{base}.{number}"
            )

    def check(ip):
        try:
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM
            )
            sock.settimeout(0.15)

            result = sock.connect_ex(
                (ip, FLASK_PORT)
            )

            sock.close()

            if result == 0:
                return ip

        except Exception:
            pass

        return None

    found = set()

    with ThreadPoolExecutor(max_workers=64) as pool:
        for ip in pool.map(check, sorted(addresses)):
            if ip:
                found.add(ip)

    return sorted(
        found,
        key=lambda ip: tuple(
            int(x) for x in ip.split(".")
        )
    )


def discover_lan_ips():
    """
    Prefer nmap host discovery.

    If nmap is unavailable or finds nothing, use the Flask-port
    fallback so this project remains usable without nmap.
    """
    found = discover_with_nmap()

    if found:
        return found, "nmap -sn"

    found = discover_flask_agents()

    return found, "Python TCP/5000 fallback"


# ----------------------------------------------------------------------
# Client communication
# ----------------------------------------------------------------------

def clean_ip(value):
    value = value.strip()

    if value.startswith("http://"):
        value = value[7:]

    if value.startswith("https://"):
        value = value[8:]

    if ":" in value:
        value = value.rsplit(":", 1)[0]

    return value.strip("/ ")


def get_info(worker):
    try:
        response = requests.get(
            worker["url"] + "/info",
            timeout=HTTP_TIMEOUT
        )

        response.raise_for_status()

        data = response.json()

        return {
            "os": data.get("os", "Unknown"),
            "cpu": float(data.get("cpu", 0)),
            "memory": float(data.get("memory", 0)),
            "network": float(data.get("network", 0)),
        }

    except Exception as exc:
        return {
            "error": str(exc)
        }


def run_remote_task(worker, command):
    response = requests.post(
        worker["url"] + "/run",
        json={
            "command": command
        },
        timeout=TASK_TIMEOUT
    )

    response.raise_for_status()

    return response.json()


# ----------------------------------------------------------------------
# Persistence / export
# ----------------------------------------------------------------------

def normalize_workers(rows):
    workers = []

    for index, row in enumerate(rows, start=1):
        ip = clean_ip(row.get("ip", ""))

        if not ip:
            continue

        workers.append({
            "name": (
                row.get("name")
                or f"Client-{index}"
            ),
            "url": f"http://{ip}:{FLASK_PORT}"
        })

    return workers


def save_config(workers):
    CONFIG_PATH.write_text(
        json.dumps(
            workers,
            indent=4
        ),
        encoding="utf-8"
    )


def load_config():
    if not CONFIG_PATH.exists():
        return []

    try:
        data = json.loads(
            CONFIG_PATH.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(data, list):
            return data

    except Exception:
        pass

    return []


def export_workers_to_scheduler(
    workers,
    target=SCHEDULER_PATH
):
    if not target.exists():
        raise FileNotFoundError(
            f"Scheduler file not found:\n{target}"
        )

    source = target.read_text(
        encoding="utf-8"
    )

    replacement = (
        "# ============================================================\n"
        "# WORKERS\n"
        "# ============================================================\n\n"
        "workers = "
        + repr(workers)
        + "\n"
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
            "Could not find the WORKERS section in scheduler2.py."
        )

    target.write_text(
        updated,
        encoding="utf-8"
    )


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class SchedulerGUI(tk.Tk):

    BG = "#05080d"
    PANEL = "#0b121c"
    PANEL_2 = "#101b29"
    PANEL_3 = "#0a1623"

    TEXT = "#eaf4ff"
    MUTED = "#8095ae"

    BLUE = "#35a7ff"
    BLUE_LIGHT = "#70c9ff"
    BLUE_DARK = "#1674bd"

    GREEN = "#35d399"
    RED = "#ff5d73"
    YELLOW = "#f5c451"

    BORDER = "#1c2d42"

    def __init__(self):
        super().__init__()

        self.title(
            "Dynamic XGBoost Workload Scheduler"
        )

        self.geometry("1450x920")
        self.minsize(1180, 760)

        self.configure(
            bg=self.BG
        )

        self.model = None

        self.client_rows = []

        self.discovered_ips = []

        self.discovery_running = False

        self.refresh_running = False

        self.history = {}

        self.max_history = HISTORY_LENGTH

        self.last_snapshot = []

        self.selected_worker = None

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

        self.protocol(
            "WM_DELETE_WINDOW",
            self.destroy
        )

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

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
            font=("Segoe UI Semibold", 23)
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
            font=("Segoe UI Semibold", 11)
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
            background=[
                ("active", self.BLUE_DARK)
            ],
            foreground=[
                ("active", "#ffffff")
            ]
        )

        style.configure(
            "Accent.TButton",
            background=self.BLUE_DARK,
            foreground="#ffffff",
            bordercolor=self.BLUE_DARK,
            padding=(14, 9),
            font=("Segoe UI Semibold", 9)
        )

        style.map(
            "Accent.TButton",
            background=[
                ("active", self.BLUE)
            ]
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
            "TCombobox",
            fieldbackground=self.PANEL_2,
            background=self.PANEL_2,
            foreground=self.TEXT,
            arrowcolor=self.BLUE,
            bordercolor=self.BORDER,
            padding=6
        )

        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", self.PANEL_2)
            ],
            foreground=[
                ("readonly", self.TEXT)
            ]
        )

        style.configure(
            "Treeview",
            background=self.PANEL_2,
            fieldbackground=self.PANEL_2,
            foreground=self.TEXT,
            rowheight=31,
            bordercolor=self.BORDER,
            font=("Segoe UI", 9)
        )

        style.configure(
            "Treeview.Heading",
            background="#15253a",
            foreground=self.TEXT,
            relief="flat",
            font=("Segoe UI Semibold", 9)
        )

        style.map(
            "Treeview",
            background=[
                ("selected", "#164d80")
            ],
            foreground=[
                ("selected", "#ffffff")
            ]
        )

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _build_header(self):

        header = tk.Frame(
            self,
            bg=self.BG
        )

        header.pack(
            fill="x",
            padx=25,
            pady=(20, 8)
        )

        left = tk.Frame(
            header,
            bg=self.BG
        )

        left.pack(
            side="left"
        )

        ttk.Label(
            left,
            text="DYNAMIC XGBOOST WORKLOAD SCHEDULER",
            style="Title.TLabel"
        ).pack(anchor="w")

        ttk.Label(
            left,
            text=(
                "Resource-aware execution • LAN discovery • "
                "Flask agents • live telemetry"
            ),
            style="Sub.TLabel"
        ).pack(
            anchor="w",
            pady=(3, 0)
        )

        self.status_var = tk.StringVar(
            value="● READY"
        )

        self.status_label = tk.Label(
            header,
            textvariable=self.status_var,
            bg=self.BG,
            fg=self.GREEN,
            font=("Segoe UI Semibold", 10)
        )

        self.status_label.pack(
            side="right",
            pady=12
        )

    # ------------------------------------------------------------------
    # Client configuration
    # ------------------------------------------------------------------

    def _build_config_panel(self):

        panel = ttk.Frame(
            self,
            style="Panel.TFrame"
        )

        panel.pack(
            fill="x",
            padx=25,
            pady=8
        )

        top = tk.Frame(
            panel,
            bg=self.PANEL
        )

        top.pack(
            fill="x",
            padx=16,
            pady=(13, 7)
        )

        ttk.Label(
            top,
            text="CLIENT CONFIGURATION",
            style="PanelTitle.TLabel"
        ).pack(
            side="left"
        )

        self.discovery_var = tk.StringVar(
            value=(
                "LAN discovery not run — "
                "click Scan LAN"
            )
        )

        tk.Label(
            top,
            textvariable=self.discovery_var,
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 8)
        ).pack(
            side="left",
            padx=15
        )

        controls = tk.Frame(
            panel,
            bg=self.PANEL
        )

        controls.pack(
            fill="x",
            padx=16,
            pady=(0, 12)
        )

        ttk.Label(
            controls,
            text="Client count:"
        ).pack(side="left")

        self.count_var = tk.StringVar(
            value="2"
        )

        ttk.Entry(
            controls,
            textvariable=self.count_var,
            width=7
        ).pack(
            side="left",
            padx=(7, 9)
        )

        ttk.Button(
            controls,
            text="Apply Count",
            command=self._rebuild_client_inputs
        ).pack(
            side="left"
        )

        ttk.Button(
            controls,
            text="🔎 Scan LAN",
            style="Accent.TButton",
            command=self.scan_lan
        ).pack(
            side="left",
            padx=7
        )

        ttk.Button(
            controls,
            text="Save + Export",
            command=self._save_and_export
        ).pack(
            side="left",
            padx=4
        )

        ttk.Button(
            controls,
            text="Refresh Now",
            command=self.refresh_now
        ).pack(
            side="right"
        )

        self.client_input_frame = tk.Frame(
            panel,
            bg=self.PANEL
        )

        self.client_input_frame.pack(
            fill="x",
            padx=16,
            pady=(0, 15)
        )

    def _rebuild_client_inputs(self):

        try:
            count = int(
                self.count_var.get()
            )

            if count < 1 or count > MAX_CLIENTS:
                raise ValueError

        except ValueError:

            messagebox.showerror(
                "Invalid client count",
                f"Enter a whole number from 1 to {MAX_CLIENTS}."
            )

            return

        old = [
            (
                row["name_var"].get(),
                row["ip_var"].get()
            )
            for row in self.client_rows
        ]

        for widget in self.client_input_frame.winfo_children():
            widget.destroy()

        self.client_rows = []

        for index in range(count):

            old_name = (
                old[index][0]
                if index < len(old)
                else ""
            )

            old_ip = (
                old[index][1]
                if index < len(old)
                else ""
            )

            name = (
                old_name
                or f"Client-{index + 1}"
            )

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
                padx=(0 if index == 0 else 5, 5)
            )

            tk.Label(
                card,
                text=f"CLIENT {index + 1}",
                bg=self.PANEL_2,
                fg=self.BLUE,
                font=("Segoe UI Semibold", 9)
            ).pack(
                anchor="w",
                padx=10,
                pady=(8, 3)
            )

            name_var = tk.StringVar(
                value=name
            )

            ip_var = tk.StringVar(
                value=old_ip
            )

            ttk.Entry(
                card,
                textvariable=name_var
            ).pack(
                fill="x",
                padx=10,
                pady=(0, 4)
            )

            combo = ttk.Combobox(
                card,
                textvariable=ip_var,
                state="normal"
            )

            combo["values"] = self._available_ip_values(
                old_ip
            )

            combo.pack(
                fill="x",
                padx=10,
                pady=(0, 9)
            )

            combo.bind(
                "<Button-1>",
                lambda event, i=index:
                    self._refresh_combo(i)
            )

            combo.bind(
                "<<ComboboxSelected>>",
                lambda event, i=index:
                    self._ip_selected(i)
            )

            self.client_rows.append({
                "name_var": name_var,
                "ip_var": ip_var,
                "ip_combo": combo
            })

        self._refresh_all_combos()

    def _available_ip_values(self, current=""):

        used = {
            clean_ip(
                row["ip_var"].get()
            )
            for row in self.client_rows
            if row["ip_var"].get().strip()
        }

        current = clean_ip(current)

        values = [
            ip
            for ip in self.discovered_ips
            if ip not in used or ip == current
        ]

        if current and current not in values:
            values.insert(0, current)

        return values

    def _refresh_combo(self, index):

        if index >= len(self.client_rows):
            return

        row = self.client_rows[index]

        current = clean_ip(
            row["ip_var"].get()
        )

        row["ip_combo"]["values"] = (
            self._available_ip_values(current)
        )

    def _refresh_all_combos(self):

        for index in range(
            len(self.client_rows)
        ):
            self._refresh_combo(index)

    def _ip_selected(self, index):

        if index >= len(self.client_rows):
            return

        selected = clean_ip(
            self.client_rows[index]["ip_var"].get()
        )

        if not selected:
            return

        duplicates = [
            row
            for i, row in enumerate(self.client_rows)
            if i != index
            and clean_ip(
                row["ip_var"].get()
            ) == selected
        ]

        if duplicates:

            self.client_rows[index]["ip_var"].set("")

            self._refresh_all_combos()

            messagebox.showwarning(
                "IP already selected",
                f"{selected} is already assigned."
            )

            return

        self._refresh_all_combos()

    # ------------------------------------------------------------------
    # LAN scan
    # ------------------------------------------------------------------

    def scan_lan(self):

        if self.discovery_running:
            return

        self.discovery_running = True

        self.discovery_var.set(
            "Scanning LAN..."
        )

        self._set_status(
            "● SCANNING LAN...",
            self.BLUE
        )

        threading.Thread(
            target=self._scan_thread,
            daemon=True
        ).start()

    def _scan_thread(self):

        try:
            ips, method = discover_lan_ips()

            self.after(
                0,
                lambda result_ips=ips, result_method=method:
                    self._apply_discovery(
                        result_ips,
                        result_method
                    )
            )

        except Exception as exc:

            error_message = str(exc)

            self.after(
                0,
                lambda message=error_message:
                    self._scan_failed(message)
            )

    def _apply_discovery(
        self,
        ips,
        method
    ):

        self.discovery_running = False

        self.discovered_ips = ips

        self._refresh_all_combos()

        self.discovery_var.set(
            f"{len(ips)} device(s) found • {method}"
        )

        self._set_status(
            f"● {len(ips)} LAN DEVICES FOUND",
            self.GREEN if ips else self.YELLOW
        )

        if ips:

            self._write_output(
                "\nLAN DISCOVERY\n"
                + "-" * 60
                + "\n"
                + f"Method: {method}\n"
                + "IPs:\n"
                + "\n".join(
                    f"  • {ip}"
                    for ip in ips
                )
                + "\n"
            )

        else:

            self._write_output(
                "\nLAN discovery found no devices.\n"
                "Make sure the machine is connected to the LAN.\n"
            )

    def _scan_failed(self, message):

        self.discovery_running = False

        self.discovery_var.set(
            "LAN scan failed"
        )

        self._set_status(
            "● LAN SCAN ERROR",
            self.RED
        )

        self._write_output(
            f"\nLAN scan error:\n{message}\n"
        )

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def _build_dashboard(self):

        wrapper = tk.Frame(
            self,
            bg=self.BG
        )

        wrapper.pack(
            fill="both",
            expand=True,
            padx=25,
            pady=(0, 8)
        )

        left = ttk.Frame(
            wrapper,
            style="Panel.TFrame"
        )

        left.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0, 8)
        )

        right = ttk.Frame(
            wrapper,
            style="Panel.TFrame"
        )

        right.pack(
            side="right",
            fill="both",
            expand=True,
            padx=(8, 0)
        )

        ttk.Label(
            left,
            text="CLIENT RESOURCE COMPARISON",
            style="PanelTitle.TLabel"
        ).pack(
            anchor="w",
            padx=14,
            pady=(12, 7)
        )

        columns = (
            "client",
            "ip",
            "os",
            "cpu",
            "ram",
            "net",
            "score",
            "state"
        )

        self.tree = ttk.Treeview(
            left,
            columns=columns,
            show="headings",
            height=9
        )

        headings = {
            "client": "CLIENT",
            "ip": "IP",
            "os": "OS",
            "cpu": "CPU %",
            "ram": "RAM %",
            "net": "NET",
            "score": "XGB SCORE",
            "state": "STATE"
        }

        widths = {
            "client": 100,
            "ip": 120,
            "os": 75,
            "cpu": 60,
            "ram": 60,
            "net": 60,
            "score": 85,
            "state": 75
        }

        for column in columns:

            self.tree.heading(
                column,
                text=headings[column]
            )

            self.tree.column(
                column,
                width=widths[column],
                anchor="center"
            )

        self.tree.pack(
            fill="both",
            expand=True,
            padx=12,
            pady=(0, 10)
        )

        ttk.Label(
            right,
            text="SCHEDULER DECISION",
            style="PanelTitle.TLabel"
        ).pack(
            anchor="w",
            padx=14,
            pady=(12, 7)
        )

        self.selected_var = tk.StringVar(
            value="No client selected"
        )

        tk.Label(
            right,
            textvariable=self.selected_var,
            bg=self.PANEL,
            fg=self.BLUE,
            font=("Segoe UI Semibold", 20)
        ).pack(
            anchor="w",
            padx=16,
            pady=(7, 1)
        )

        self.score_var = tk.StringVar(
            value="XGBoost score: —"
        )

        tk.Label(
            right,
            textvariable=self.score_var,
            bg=self.PANEL,
            fg=self.TEXT,
            justify="left",
            font=("Segoe UI", 9)
        ).pack(
            anchor="w",
            padx=16,
            pady=(0, 8)
        )

        self.metric_frame = tk.Frame(
            right,
            bg=self.PANEL
        )

        self.metric_frame.pack(
            fill="x",
            padx=16,
            pady=(0, 8)
        )

        self.metric_vars = {}

        for title, key in [
            ("CPU", "cpu"),
            ("RAM", "ram"),
            ("NET", "net")
        ]:

            box = tk.Frame(
                self.metric_frame,
                bg=self.PANEL_3,
                highlightbackground=self.BORDER,
                highlightthickness=1
            )

            box.pack(
                side="left",
                fill="x",
                expand=True,
                padx=(0, 5)
            )

            tk.Label(
                box,
                text=title,
                bg=self.PANEL_3,
                fg=self.MUTED,
                font=("Segoe UI", 8)
            ).pack(
                anchor="w",
                padx=8,
                pady=(6, 0)
            )

            var = tk.StringVar(
                value="—"
            )

            self.metric_vars[key] = var

            tk.Label(
                box,
                textvariable=var,
                bg=self.PANEL_3,
                fg=self.BLUE,
                font=("Segoe UI Semibold", 13)
            ).pack(
                anchor="w",
                padx=8,
                pady=(0, 6)
            )

        ttk.Label(
            right,
            text="Task command",
            style="TLabel"
        ).pack(
            anchor="w",
            padx=16
        )

        self.command_var = tk.StringVar(
            value="python3 --version"
        )

        ttk.Entry(
            right,
            textvariable=self.command_var
        ).pack(
            fill="x",
            padx=16,
            pady=(4, 8)
        )

        ttk.Button(
            right,
            text="RUN TASK ON SELECTED CLIENT",
            style="Accent.TButton",
            command=self.run_selected_task
        ).pack(
            fill="x",
            padx=16,
            pady=(2, 7)
        )

        ttk.Button(
            right,
            text="AUTO SELECT + RUN TASK",
            command=self.auto_select_and_run
        ).pack(
            fill="x",
            padx=16,
            pady=(0, 10)
        )

        self.output_text = tk.Text(
            right,
            bg="#06101a",
            fg="#cfe7ff",
            insertbackground=self.TEXT,
            relief="flat",
            wrap="word",
            font=("Consolas", 8),
            height=8
        )

        self.output_text.pack(
            fill="both",
            expand=True,
            padx=16,
            pady=(0, 13)
        )

        self.output_text.insert(
            "end",
            "Scheduler response will appear here...\n"
        )

        self.output_text.configure(
            state="disabled"
        )

        self.tree.bind(
            "<<TreeviewSelect>>",
            self._table_selection_changed
        )

    # ------------------------------------------------------------------
    # Better graph
    # ------------------------------------------------------------------

    def _build_graph(self):

        graph_panel = ttk.Frame(
            self,
            style="Panel.TFrame"
        )

        graph_panel.pack(
            fill="both",
            expand=True,
            padx=25,
            pady=(0, 8)
        )

        header = tk.Frame(
            graph_panel,
            bg=self.PANEL
        )

        header.pack(
            fill="x",
            padx=14,
            pady=(9, 2)
        )

        ttk.Label(
            header,
            text="LIVE RESOURCE TELEMETRY",
            style="PanelTitle.TLabel"
        ).pack(
            side="left"
        )

        tk.Label(
            header,
            text="CPU solid • RAM dashed • transparent blue",
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 8)
        ).pack(
            side="right"
        )

        self.figure = Figure(
            figsize=(10, 3.2),
            dpi=100,
            facecolor=self.PANEL
        )

        self.ax = self.figure.add_subplot(
            111
        )

        self.ax.set_facecolor(
            self.PANEL_3
        )

        self._style_graph_axes()

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

    def _style_graph_axes(self):

        self.ax.set_ylim(
            0,
            100
        )

        self.ax.set_ylabel(
            "Utilization %",
            color=self.MUTED,
            fontsize=8
        )

        self.ax.set_xlabel(
            "Telemetry sample",
            color=self.MUTED,
            fontsize=8
        )

        self.ax.tick_params(
            colors=self.MUTED,
            labelsize=7
        )

        self.ax.grid(
            alpha=0.12,
            color="white",
            linestyle="--",
            linewidth=0.7
        )

        for spine in self.ax.spines.values():
            spine.set_color(
                self.BORDER
            )

    def _redraw_graph(self):

        self.ax.clear()

        self.ax.set_facecolor(
            self.PANEL_3
        )

        self._style_graph_axes()

        for name, data in self.history.items():

            if not data["cpu"]:
                continue

            x = list(
                range(
                    len(data["cpu"])
                )
            )

            cpu = data["cpu"]
            ram = data["ram"]

            self.ax.plot(
                x,
                cpu,
                color=self.BLUE,
                linewidth=2.0,
                alpha=0.95,
                label=f"{name} CPU"
            )

            self.ax.fill_between(
                x,
                cpu,
                alpha=0.10,
                color=self.BLUE
            )

            self.ax.plot(
                x,
                ram,
                color=self.BLUE_LIGHT,
                linewidth=1.25,
                linestyle="--",
                alpha=0.70,
                label=f"{name} RAM"
            )

        if self.history:

            legend = self.ax.legend(
                loc="upper left",
                fontsize=7,
                frameon=False,
                ncol=2
            )

            for text in legend.get_texts():
                text.set_color(
                    self.TEXT
                )

        self.figure.tight_layout(
            pad=1.0
        )

        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------

    def _build_footer(self):

        footer = tk.Frame(
            self,
            bg=self.BG
        )

        footer.pack(
            fill="x",
            padx=25,
            pady=(0, 13)
        )

        self.last_update_var = tk.StringVar(
            value="Last update: —"
        )

        tk.Label(
            footer,
            textvariable=self.last_update_var,
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 8)
        ).pack(
            side="left"
        )

        tk.Label(
            footer,
            text="XGBoost • Flask • psutil • LAN",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 8)
        ).pack(
            side="right"
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _set_from_config(self, workers):

        self.count_var.set(
            str(
                max(
                    1,
                    len(workers)
                )
            )
        )

        self._rebuild_client_inputs()

        for row, worker in zip(
            self.client_rows,
            workers
        ):

            row["name_var"].set(
                worker.get(
                    "name",
                    "Client"
                )
            )

            row["ip_var"].set(
                clean_ip(
                    worker.get(
                        "url",
                        ""
                    )
                )
            )

        self._refresh_all_combos()

    def _current_workers(self):

        rows = []

        seen = set()

        for index, row in enumerate(
            self.client_rows,
            start=1
        ):

            name = (
                row["name_var"].get().strip()
                or f"Client-{index}"
            )

            ip = clean_ip(
                row["ip_var"].get()
            )

            if not ip:
                continue

            if ip in seen:

                messagebox.showwarning(
                    "Duplicate IP",
                    f"{ip} is assigned more than once."
                )

                continue

            seen.add(ip)

            rows.append({
                "name": name,
                "url": f"http://{ip}:{FLASK_PORT}"
            })

        return rows

    def _save_and_export(self):

        workers = self._current_workers()

        if not workers:

            messagebox.showwarning(
                "No clients",
                "Select or enter at least one client IP."
            )

            return

        try:

            save_config(
                workers
            )

            export_workers_to_scheduler(
                workers
            )

        except Exception as exc:

            self._show_error(
                "Save failed",
                str(exc)
            )

            return

        self._set_status(
            "● CONFIGURATION SAVED",
            self.GREEN
        )

        self._write_output(
            "\nConfiguration saved.\n"
            f"Workers exported to:\n{SCHEDULER_PATH}\n"
        )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def refresh_now(self):

        if self.refresh_running:
            return

        workers = self._current_workers()

        if not workers:

            self._write_output(
                "\nEnter/select client IP addresses first.\n"
            )

            return

        self.refresh_running = True

        self._set_status(
            "● CHECKING CLIENTS...",
            self.BLUE
        )

        threading.Thread(
            target=self._refresh_thread,
            args=(workers,),
            daemon=True
        ).start()

    def _refresh_thread(self, workers):

        results = []

        for worker in workers:

            info = get_info(
                worker
            )

            results.append({
                "worker": worker,
                "info": info
            })

        self.after(
            0,
            lambda result=results:
                self._apply_snapshot(result)
        )

    def _apply_snapshot(self, results):

        self.refresh_running = False

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
                        worker["url"].replace(
                            "http://",
                            ""
                        ),
                        "—",
                        "—",
                        "—",
                        "—",
                        "—",
                        "OFFLINE"
                    )
                )

                continue

            cpu = info["cpu"]
            ram = info["memory"]
            net = info["network"]

            self.tree.insert(
                "",
                "end",
                iid=worker["url"],
                values=(
                    worker["name"],
                    worker["url"].replace(
                        "http://",
                        ""
                    ),
                    info["os"],
                    f"{cpu:.1f}",
                    f"{ram:.1f}",
                    f"{net:.1f}",
                    "—",
                    "ONLINE"
                )
            )

            name = worker["name"]

            self.history.setdefault(
                name,
                {
                    "cpu": [],
                    "ram": []
                }
            )

            self.history[name]["cpu"].append(
                cpu
            )

            self.history[name]["ram"].append(
                ram
            )

            self.history[name]["cpu"] = (
                self.history[name]["cpu"][
                    -self.max_history:
                ]
            )

            self.history[name]["ram"] = (
                self.history[name]["ram"][
                    -self.max_history:
                ]
            )

        self._redraw_graph()

        online = sum(
            1
            for item in results
            if "error" not in item["info"]
        )

        self._set_status(
            f"● {online}/{len(results)} CLIENTS ONLINE",
            self.GREEN if online else self.RED
        )

        self.last_update_var.set(
            "Last update: "
            + datetime.now().strftime(
                "%H:%M:%S"
            )
        )

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _table_selection_changed(self, event=None):

        selection = self.tree.selection()

        if not selection:
            return

        url = selection[0]

        item = next(
            (
                x
                for x in self.last_snapshot
                if x["worker"]["url"] == url
            ),
            None
        )

        if not item:
            return

        if "error" in item["info"]:
            return

        self.selected_worker = item["worker"]

        info = item["info"]

        self.selected_var.set(
            f"{item['worker']['name']} • "
            f"{info['os']}"
        )

        self.metric_vars["cpu"].set(
            f"{info['cpu']:.1f}%"
        )

        self.metric_vars["ram"].set(
            f"{info['memory']:.1f}%"
        )

        self.metric_vars["net"].set(
            f"{info['network']:.1f}"
        )

    # ------------------------------------------------------------------
    # XGBoost execution
    # ------------------------------------------------------------------

    def auto_select_and_run(self):

        workers = self._current_workers()

        if not workers:

            messagebox.showwarning(
                "No clients",
                "Select or enter client IP addresses first."
            )

            return

        self._set_status(
            "● XGBOOST SELECTING...",
            self.BLUE
        )

        threading.Thread(
            target=self._auto_select_thread,
            args=(workers,),
            daemon=True
        ).start()

    def _auto_select_thread(self, workers):

        try:

            model = load_model()

        except Exception as exc:

            error_message = str(exc)

            self.after(
                0,
                lambda message=error_message:
                    self._show_error(
                        "XGBoost model error",
                        message
                    )
            )

            return

        available = []

        latest = []

        for worker in workers:

            info = get_info(
                worker
            )

            latest.append({
                "worker": worker,
                "info": info
            })

            if "error" not in info:

                available.append({
                    "worker": worker,
                    "info": info
                })

        if not available:

            self.after(
                0,
                lambda:
                    self._show_error(
                        "No clients available",
                        "No configured Flask agent responded on port 5000."
                    )
            )

            return

        try:

            best = find_best_worker(
                model,
                available
            )

        except Exception as exc:

            error_message = str(exc)

            self.after(
                0,
                lambda message=error_message:
                    self._show_error(
                        "XGBoost prediction error",
                        message
                    )
            )

            return

        self.after(
            0,
            lambda snapshot=latest, selected=best:
                self._apply_model_result(
                    snapshot,
                    selected
                )
        )

    def _apply_model_result(
        self,
        snapshot,
        best
    ):

        self._apply_snapshot(
            snapshot
        )

        if not best:

            self._show_error(
                "Selection failed",
                "XGBoost did not select a client."
            )

            return

        worker = best["worker"]
        info = best["info"]
        probs = best["probabilities"]

        self.selected_worker = worker

        self.selected_var.set(
            f"{worker['name']} • "
            f"{info['os']}"
        )

        self.score_var.set(
            "Selected class: "
            f"{probs['selected_class']}\n"
            f"Selected probability: "
            f"{probs['selected'] * 100:.2f}%   |   "
            f"Windows: "
            f"{probs['windows'] * 100:.2f}%   |   "
            f"Kali/Linux: "
            f"{probs['kali'] * 100:.2f}%"
        )

        self.metric_vars["cpu"].set(
            f"{info['cpu']:.1f}%"
        )

        self.metric_vars["ram"].set(
            f"{info['memory']:.1f}%"
        )

        self.metric_vars["net"].set(
            f"{info['network']:.1f}"
        )

        # Highlight selected worker.
        self.tree.selection_set(
            worker["url"]
        )

        self._write_output(
            "\n"
            + "=" * 72
            + "\n"
            + "XGBOOST SCHEDULER DECISION\n"
            + "=" * 72
            + "\n"
            + f"Selected client : {worker['name']}\n"
            + f"Endpoint        : {worker['url']}\n"
            + f"OS              : {info['os']}\n"
            + f"CPU             : {info['cpu']:.2f}%\n"
            + f"RAM             : {info['memory']:.2f}%\n"
            + f"Network         : {info['network']:.2f}\n"
            + f"Windows prob.   : {probs['windows'] * 100:.2f}%\n"
            + f"Kali/Linux prob.: {probs['kali'] * 100:.2f}%\n"
            + f"Selected prob.  : {probs['selected'] * 100:.2f}%\n"
            + "=" * 72
            + "\n"
        )

        self._set_status(
            "● XGBOOST SELECTED CLIENT",
            self.GREEN
        )

        # Execute the existing project's /run endpoint.
        self._set_status(
            "● RUNNING REMOTE TASK...",
            self.BLUE
        )

        command = self.command_var.get().strip()

        threading.Thread(
            target=self._task_thread,
            args=(worker, command),
            daemon=True
        ).start()

    def run_selected_task(self):

        worker = self.selected_worker

        if not worker:

            selection = self.tree.selection()

            if selection:

                worker = next(
                    (
                        w
                        for w in self._current_workers()
                        if w["url"] == selection[0]
                    ),
                    None
                )

        if not worker:

            messagebox.showinfo(
                "Select a client",
                "Select an online client first."
            )

            return

        self.selected_worker = worker

        command = self.command_var.get().strip()

        self._set_status(
            "● RUNNING REMOTE TASK...",
            self.BLUE
        )

        threading.Thread(
            target=self._task_thread,
            args=(worker, command),
            daemon=True
        ).start()

    def _task_thread(
        self,
        worker,
        command
    ):

        try:

            result = run_remote_task(
                worker,
                command
            )

            self.after(
                0,
                lambda task_result=result,
                       selected_worker=worker:
                    self._show_task_result(
                        selected_worker,
                        task_result
                    )
            )

        except Exception as exc:

            error_message = (
                f"{worker['name']} "
                f"({worker['url']})\n\n"
                f"{exc}"
            )

            self.after(
                0,
                lambda message=error_message:
                    self._show_error(
                        "Task execution failed",
                        message
                    )
            )

    def _show_task_result(
        self,
        worker,
        result
    ):

        success = bool(
            result.get(
                "success",
                False
            )
        )

        output = result.get(
            "output",
            ""
        )

        error = result.get(
            "error",
            ""
        )

        return_code = result.get(
            "return_code",
            "—"
        )

        self._write_output(
            "\n"
            + "=" * 72
            + "\n"
            + f"REMOTE TASK RESULT — {worker['name']}\n"
            + "=" * 72
            + "\n"
            + f"Success: {success}\n"
            + f"Return code: {return_code}\n\n"
            + "OUTPUT:\n"
            + str(output)
            + "\n\n"
            + "ERROR:\n"
            + str(error)
            + "\n"
        )

        self._set_status(
            "● TASK COMPLETED"
            if success
            else "● TASK FAILED",
            self.GREEN
            if success
            else self.RED
        )

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    def _write_output(self, text):

        self.output_text.configure(
            state="normal"
        )

        self.output_text.insert(
            "end",
            text
        )

        self.output_text.see(
            "end"
        )

        self.output_text.configure(
            state="disabled"
        )

    def _show_error(
        self,
        title,
        message
    ):

        self._set_status(
            "● ERROR",
            self.RED
        )

        self._write_output(
            "\nERROR: "
            + message
            + "\n"
        )

        messagebox.showerror(
            title,
            message
        )

    def _set_status(
        self,
        text,
        color
    ):

        self.status_var.set(
            text
        )

        self.status_label.configure(
            fg=color
        )


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():

    app = SchedulerGUI()

    app.mainloop()


if __name__ == "__main__":
    main()
