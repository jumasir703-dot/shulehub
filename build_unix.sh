#!/bin/bash
# Run this ON macOS or Linux (PyInstaller cannot cross-compile).
# Produces dist/GTimetable
set -e

python3 -m venv venv
source venv/bin/activate
pip install -r requirements-offline.txt
pyinstaller GTimetable.spec

echo
echo "Done. Your offline app is at dist/GTimetable"
echo "Run it with: ./dist/GTimetable"
echo "It opens your browser automatically."
