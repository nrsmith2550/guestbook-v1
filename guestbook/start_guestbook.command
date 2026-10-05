#!/bin/bash
# Double-click to start the guestbook. Restarts automatically if it ever crashes,
# and keeps the Mac awake while running.
cd "$(dirname "$0")"
source .venv/bin/activate
while true; do
  caffeinate -dimsu python guestbook.py
  echo "Guestbook stopped - restarting in 3 seconds (close this window to quit)..."
  sleep 3
done
