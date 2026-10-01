import xgboost as xgb
import numpy as np


# ============================================================
# TRAINING DATA
# ============================================================
#
# Features:
# 1. CPU usage (%)
# 2. Memory usage (%)
# 3. Network usage (MB)
#
# Target:
# 1 = Execute on Kali
# 0 = Execute on Windows
#

X = np.array([
    [20, 30, 100],
    [25, 35, 120],
    [30, 40, 150],
    [35, 45, 180],
    [40, 50, 200],

    [60, 65, 300],
    [70, 75, 400],
    [80, 80, 500],
    [85, 90, 600],
    [90, 95, 700]
])

y = np.array([
    1,
    1,
    1,
    1,
    1,

    0,
    0,
    0,
    0,
    0
])


# ============================================================
# CREATE XGBOOST MODEL
# ============================================================

model = xgb.XGBClassifier(
    n_estimators=100,
    max_depth=3,
    learning_rate=0.1,
    objective="binary:logistic",
    eval_metric="logloss"
)


# ============================================================
# TRAIN
# ============================================================

model.fit(X, y)


# ============================================================
# SAVE MODEL
# ============================================================

model.save_model("xgboost_model.json")

print("========================================")
print("XGBoost model trained successfully")
print("========================================")

print("Model saved as:")
print("xgboost_model.json")