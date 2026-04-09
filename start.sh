#!/usr/bin/env bash

set -e

# Activate virtual environment
source venv/bin/activate

# Install dependencies (skip if already installed)
pip install -r requirements.txt

# Run the app
python app.py
