"""
config.py
=========
Single place that loads configuration from the environment / a .env file.

The Gemini API key is NEVER hard-coded. Put it in a file called `.env` next to
this file (copy `.env.example`), and it is read here at startup:

    GEMINI_API_KEY=your_key_here
    GEMINI_MODEL=gemini-2.5-flash      # optional, this is the default
"""

import os
import sys

# Load a .env file if python-dotenv is installed (optional convenience).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
PORT = int(os.environ.get("PORT", "5000"))
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"


def log(msg):
    """Lightweight stderr logger so fallbacks are visible during a demo."""
    print("[parsely] {}".format(msg), file=sys.stderr)
