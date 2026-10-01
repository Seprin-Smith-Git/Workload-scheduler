# Dynamic XGBoost Workload Scheduler

A lightweight LAN-based workload scheduler that discovers machines on a local network, monitors their resources, and uses an XGBoost model to select a suitable worker for task execution.

The project combines **Python, Flask, Tkinter, psutil, XGBoost, and Nmap** to create a simple distributed execution environment.

## Features

- LAN client discovery using Nmap
- Python TCP/5000 fallback when Nmap is unavailable
- Flask-based resource agents
- CPU, RAM, and network telemetry
- XGBoost-based worker selection
- Windows and Linux/Kali worker support
- Manual task execution
- Automatic worker selection and execution
- Live resource monitoring
- CPU/RAM telemetry graph
- Persistent worker configuration
- Scheduler worker-list export
- Dark-mode Tkinter dashboard

The scheduler can discover configured Flask agents and query their `/info` endpoint for system information before making a scheduling decision.

---

## Architecture

```text
                    ┌─────────────────────────┐
                    │       Scheduler         │
                    │   Tkinter GUI + XGBoost │
                    └────────────┬────────────┘
                                 │
                    LAN Discovery / HTTP
                                 │
             ┌──────────────────┼──────────────────┐
             │                  │                  │
             ▼                  ▼                  ▼
      ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
      │   Client 1  │    │   Client 2  │    │   Client N  │
      │ Flask Agent │    │ Flask Agent │    │ Flask Agent │
      └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
             │                  │                  │
             ▼                  ▼                  ▼
          CPU/RAM            CPU/RAM            CPU/RAM
          Network            Network            Network
```

Each client runs a small Flask resource agent on **port 5000**. The scheduler queries `/info` for resource information and uses `/run` to request task execution.

---

## Project Structure

```text
project/
│
├── scheduler_nmap_gui.py
├── resource_agent.py
├── xgboost_model.json
├── scheduler_workers.json
├── scheduler2.py
├── task.sh
└── README.md
```

### Scheduler

`scheduler_nmap_gui.py` provides the main dashboard.

It handles:

- LAN discovery
- Worker configuration
- Client health checks
- Resource monitoring
- XGBoost predictions
- Worker selection
- Remote task execution
- Configuration persistence

The GUI also prevents duplicate worker IP assignments and supports dynamic client counts.

### Resource Agent

`resource_agent.py` runs on each worker machine.

It exposes:

```text
GET  /info
POST /run
```

`/info` reports the worker's operating system, CPU usage, and memory usage.

`/run` executes the worker's configured task script and returns its output, error stream, and return code.

---

## Requirements

### Scheduler

Python 3.x with:

```bash
pip install requests xgboost matplotlib
```

Tkinter is also required for the GUI.

### Worker

Install:

```bash
pip install flask psutil
```

Linux/Kali workers also require the tools used by `task.sh`.

### Optional

Install Nmap for faster LAN discovery.

Linux:

```bash
sudo apt install nmap
```

Windows:

Install Nmap and make sure it is available in the system `PATH`.

If Nmap is unavailable, the scheduler automatically falls back to scanning TCP port `5000` for Flask agents.

---

## Setup

### 1. Start the resource agent

On each worker:

```bash
python resource_agent.py
```

The Flask agent listens on:

```text
0.0.0.0:5000
```

as configured in the resource agent.

You can verify it from another machine with:

```bash
curl http://<CLIENT-IP>:5000/info
```

Example response:

```json
{
    "os": "Linux",
    "cpu": 23.4,
    "memory": 41.8
}
```

---

### 2. Start the scheduler

On the scheduler machine:

```bash
python scheduler_nmap_gui.py
```

The GUI provides controls for:

1. Selecting the number of clients
2. Scanning the LAN
3. Selecting discovered IP addresses
4. Checking client resources
5. Running tasks manually
6. Automatically selecting a worker
7. Saving the worker configuration

---

## Worker Selection

When automatic scheduling is enabled, the scheduler collects resource information from available workers.

The current feature set uses:

```text
CPU
RAM
Network
```

as model inputs.

The XGBoost model then produces class probabilities for the worker.

The project's current class convention is:

```text
Class 0 → Windows
Class 1 → Kali/Linux
```

The scheduler uses the probability associated with the worker's operating-system class when comparing available workers.

The worker with the highest selected probability is returned as the scheduler's choice.

---

## Running a Task

The GUI supports two execution modes.

### Manual

Select an online client and enter a command:

```text
python3 --version
```

Then press:

```text
RUN TASK ON SELECTED CLIENT
```

### Automatic

Use:

```text
AUTO SELECT + RUN TASK
```

The scheduler:

```text
Collect resources
       ↓
Check available workers
       ↓
Run XGBoost prediction
       ↓
Select worker
       ↓
Send task
       ↓
Display result
```

---

## Distributed Execution

The resource agent currently executes the project's `task.sh` script when a task is received.

```text
Scheduler
    │
    │ POST /run
    ▼
Flask Agent
    │
    │ execute
    ▼
task.sh
    │
    ▼
Task / Workload
```

The execution response contains:

- Success status
- Standard output
- Standard error
- Return code

This makes the agent suitable as a simple execution layer for distributed workloads.

---

## LAN Discovery

The scheduler first attempts:

```bash
nmap -sn -n <subnet>
```

to discover machines on the local network.

If Nmap is unavailable or does not return usable hosts, it falls back to checking TCP port `5000`.

This allows the project to work in environments where Nmap is not installed.

---

## Configuration

Worker configurations are stored in:

```text
scheduler_workers.json
```

Example:

```json
[
    {
        "name": "Client-1",
        "url": "http://192.168.1.101:5000"
    },
    {
        "name": "Client-2",
        "url": "http://192.168.1.102:5000"
    }
]
```

The GUI can save the configuration and export the worker list into the scheduler configuration.

---

## XGBoost Model

The scheduler expects:

```text
xgboost_model.json
```

in the same directory as the GUI.

The model is loaded using the XGBoost classifier interface.

The current feature vector is:

```text
[CPU, Memory, Network]
```

The model must therefore be trained with a compatible feature order.

---

## Security Note

This project is intended for **trusted LAN environments and development/testing**.

The current Flask agent exposes a remote execution endpoint on port `5000`. It does not implement authentication, authorization, encryption, or command sandboxing.

Do **not** expose the agent directly to the public internet.

For production use, consider adding:

- Authentication
- TLS/HTTPS
- Request signing
- Worker authorization
- Command allowlists
- Sandboxed execution
- Firewall rules
- Job IDs and execution tracking

---

## Roadmap

Planned improvements include:

- Distributed Blender rendering
- Partial frame execution
- Dynamic workload splitting
- Worker failure recovery
- Job queues
- Progress reporting
- Automatic worker rebalancing
- Centralized result collection
- Render-result merging
- Better cross-platform task execution
- Secure worker authentication

The architecture is intended to evolve from simple remote task execution toward a dynamic distributed workload scheduler.

---

## Tech Stack

| Component | Technology |
|---|---|
| Scheduler GUI | Tkinter |
| ML Scheduler | XGBoost |
| API | Flask |
| System Metrics | psutil |
| Network Discovery | Nmap |
| HTTP Communication | Requests |
| Telemetry Graph | Matplotlib |
| Language | Python |

---

## Status

**Active development**

This project is currently focused on building the scheduling and distributed-execution foundation for resource-aware workloads.
