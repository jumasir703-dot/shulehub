#!/bin/bash
# Run this ON macOS or Linux (PyInstaller cannot cross-compile).
# Produces dist/ShuleHub
set -e

python3 -m venv venv
source venv/bin/activate
pip install -r requirements-offline.txt
pyinstaller --clean ShuleHub.spec

echo
echo "Done. Your offline app is at dist/ShuleHub"
echo "Run it with: ./dist/ShuleHub"
echo "It opens in its own ShuleHub window."
