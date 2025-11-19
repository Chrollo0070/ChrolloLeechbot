#!/bin/bash

# 1. Start Aria2 in the background
# We use & to detach it so the script continues
aria2c --conf-path=aria2.conf &

# Wait 2 seconds to ensure Aria2 is ready
sleep 2

# 2. Start the Python Bot
python bot.py