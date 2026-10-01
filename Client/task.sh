#!/bin/bash

echo "================================="
echo "REMOTE TASK EXECUTION"
echo "================================="

echo "Hostname: $(hostname)"
echo "User: $(whoami)"
echo "Display: $DISPLAY"

export DISPLAY=:0
export XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.Q5SAV3

echo "Launching Firefox..."

firefox >/tmp/firefox.log 2>&1 &

echo "Firefox launch command sent."

echo "================================="
echo "TASK COMPLETED"
echo "================================="