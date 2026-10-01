from flask import Flask, request, jsonify
import psutil
import platform
import subprocess

app = Flask(__name__)


# =========================
# SYSTEM INFORMATION
# =========================

@app.route("/info")
def info():

    return jsonify({
        "os": platform.system(),
        "cpu": psutil.cpu_percent(interval=1),
        "memory": psutil.virtual_memory().percent
    })


# =========================
# EXECUTE TASK.PS1
# =========================

@app.route("/run", methods=["POST"])
def run_task():

    print("[+] Task received")

    try:

        result = subprocess.run(
            [
                "powershell.exe",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                r"C:\Project\task.ps1"
            ],
            capture_output=True,
            text=True
        )

        print("[+] task.ps1 executed")

        print("[+] Output:")
        print(result.stdout)

        print("[+] Error:")
        print(result.stderr)

        return jsonify({

            "success": result.returncode == 0,

            "output": result.stdout,

            "error": result.stderr,

            "return_code": result.returncode

        })

    except Exception as e:

        print("[!] Error:", e)

        return jsonify({

            "success": False,

            "error": str(e)

        }), 500


# =========================
# SERVER
# =========================

app.run(
    host="0.0.0.0",
    port=5000
)
