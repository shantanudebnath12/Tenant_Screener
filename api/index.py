"""Vercel Python serverless entry point."""

from __future__ import annotations

import sys
from pathlib import Path

# Vercel unpacks the project at /var/task; make the parent importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: F401  (exported for @vercel/python)
