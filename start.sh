#!/bin/bash
# Start both the Flask bot and the Telethon userbot together
set -e
echo "Starting Flask bot..."
python bot_script.py &
FLASK_PID=$!

echo "Starting Telethon userbot..."
python userbot.py &
USERBOT_PID=$!

# If either dies, kill both and exit so Render restarts the service
wait -n $FLASK_PID $USERBOT_PID
echo "A process exited. Shutting down..."
kill $FLASK_PID $USERBOT_PID 2>/dev/null || true
