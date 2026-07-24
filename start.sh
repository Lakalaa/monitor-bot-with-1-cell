#!/bin/bash
echo "Starting Telethon userbot..."
python userbot.py &

echo "Starting Flask bot (main service)..."
exec python bot_script.py
