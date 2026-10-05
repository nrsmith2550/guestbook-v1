#!/bin/bash
# Double-click to start the guestbook. Restarts automatically if it ever crashes,
# and keeps the Mac awake while running.

# How the handset pickup is detected:
#   "--keyboard"  the Enter key toggles lifted / hung up (current setup)
#   ""            use the USB game-controller / arcade-encoder hook switch
MODE="--keyboard"

cd "$(dirname "$0")"
source .venv/bin/activate
while true; do
  caffeinate -dimsu python guestbook.py $MODE
  echo "Guestbook stopped - restarting in 3 seconds (close this window to quit)..."
  sleep 3
done
