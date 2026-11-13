"""Standalone GraphQL source service: ``uvicorn source_app:app --port 8001``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.source import app  # noqa: E402,F401
