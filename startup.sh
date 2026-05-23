#!/bin/bash
# Azure App Service startup script for Sprint Health MCP.
# Configure in: App Service → Configuration → General settings → Startup Command → bash startup.sh
set -e

cd /home/site/wwwroot

# Oryx auto-installs requirements.txt during deployment; this is a safety net.
if [ -f requirements.txt ]; then
    pip install -r requirements.txt --quiet
fi

python main.py
