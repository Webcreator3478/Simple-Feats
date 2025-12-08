#!/bin/bash

# This script is the entry point for the Render service.

# Exit immediately if a command exits with a non-zero status.
set -e

echo "Starting Discord bot and web server..."
# Run the main Python script. 
# main.py will start the bot and the dummy server concurrently using threads.
python main.py