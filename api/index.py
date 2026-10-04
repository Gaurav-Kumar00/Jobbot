"""Vercel entry point: exposes the FastAPI app (see src/jobbot/bot/app.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobbot.bot.app import app  # noqa: E402,F401
