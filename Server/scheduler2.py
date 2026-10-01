import requests
import xgboost as xgb
import os


# ============================================================
# WORKERS
# ============================================================

workers = [

    {
        "name": "Kali",
        "url": "http://172.20.10.2:5000"
    },

    {
        "name": "Windows",
        "url": "http://10.96.208.144:5000"
    }

]


# ============================================================
# XGBOOST MODEL
# ============================================================

MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "xgboost_model.json"
)


def load_model():

    try:

        model = xgb.XGBClassifier()

        model.load_model(MODEL_PATH)

        print("XGBoost model loaded successfully.")

        return model

    except Exception as e:

        print("XGBoost model loading failed:")
        print(e)

        return None


# ============================================================
# GET WORKER INFORMATION
# ============================================================

def get_info(worker):

    try:

        response = requests.get(
            worker["url"] + "/info",
            timeout=3
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        return None


# ============================================================
# DISPLAY WORKERS
# ============================================================

def get_workers_info():

    available_workers = []

    print("\nChecking workers...\n")

    for worker in workers:

        info = get_info(worker)

        if info is None:

            print(
                worker["name"],
                "OFFLINE"
            )

            continue

        print(
            worker["name"],
            "| OS:",
            info["os"],
            "| CPU:",
            info["cpu"],
            "%",
            "| RAM:",
            info["memory"],
            "%",
            "| Network:",
            info.get("network", 0),
            "MB"
        )

        available_workers.append(
            (worker, info)
        )

    return available_workers


# ============================================================
# PREPARE FEATURES
# ============================================================

def prepare_features(info):

    cpu = info["cpu"]

    memory = info["memory"]

    network = info.get(
        "network",
        0
    )

    return [[
        cpu,
        memory,
        network
    ]]


# ============================================================
# XGBOOST WORKER SELECTION
# ============================================================

def find_best_worker(model, available_workers):

    if not available_workers:

        return None

    print("\nXGBoost Worker Selection")
    print("------------------------")

    best_worker = None
    best_probability = -1

    for worker, info in available_workers:

        features = prepare_features(info)

        # XGBoost probability
        probability = model.predict_proba(
            features
        )[0]

        # ----------------------------------------------------
        # Our model:
        #
        # Class 0 = Windows
        # Class 1 = Kali
        # ----------------------------------------------------

        kali_probability = float(
            probability[1]
        )

        windows_probability = float(
            probability[0]
        )

        print(
            worker["name"],
            "| Windows:",
            round(windows_probability, 3),
            "| Kali:",
            round(kali_probability, 3)
        )

        # ----------------------------------------------------
        # For Kali
        # ----------------------------------------------------

        if worker["name"] == "Kali":

            worker_probability = kali_probability

        # ----------------------------------------------------
        # For Windows
        # ----------------------------------------------------

        elif worker["name"] == "Windows":

            worker_probability = windows_probability

        else:

            worker_probability = 0

        # ----------------------------------------------------
        # Find highest probability
        # ----------------------------------------------------

        if worker_probability > best_probability:

            best_probability = worker_probability

            best_worker = worker

    return best_worker


# ============================================================
# RUN TASK
# ============================================================

def run_task(worker, command):

    print(
        "\nRunning task on:",
        worker["name"]
    )

    print(
        "Command:",
        command
    )

    try:

        response = requests.post(

            worker["url"] + "/run",

            json={
                "command": command
            },

            timeout=60

        )

        response.raise_for_status()

        result = response.json()

        print("\nOUTPUT:")
        print(
            result.get(
                "output",
                ""
            )
        )

        if result.get("error"):

            print("\nERROR:")
            print(
                result["error"]
            )

    except Exception as e:

        print(
            "\nTask execution failed:"
        )

        print(e)


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("DYNAMIC XGBOOST TASK SCHEDULER")
    print("=" * 60)

    # --------------------------------------------------------
    # Load XGBoost
    # --------------------------------------------------------

    model = load_model()

    if model is None:

        return

    # --------------------------------------------------------
    # Get worker information
    # --------------------------------------------------------

    available_workers = get_workers_info()

    if not available_workers:

        print(
            "\nNo worker available."
        )

        return

    # --------------------------------------------------------
    # Select worker using XGBoost
    # --------------------------------------------------------

    worker = find_best_worker(
        model,
        available_workers
    )

    if worker is None:

        print(
            "\nNo suitable worker found."
        )

        return

    # --------------------------------------------------------
    # Display selected worker
    # --------------------------------------------------------

    print(
        "\n========================================"
    )

    print(
        "Selected:",
        worker["name"]
    )

    print(
        "URL:",
        worker["url"]
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # Run task
    # --------------------------------------------------------

    run_task(
        worker,
        "python3 --version"
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()