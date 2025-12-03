#!/bin/bash
while true; do
    echo "🤖 Starting Bot..."
    python main.py
    echo "⚠️ Bot crashed! Restarting in 10 seconds..."
    sleep 10
done
