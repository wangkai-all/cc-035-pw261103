"""Single-process demo server: GraphQL source + collection API.

    uvicorn dev:app --port 8000

The source is mounted at ``/source`` and the collection API at ``/api``.
The collector still talks to the source over real HTTP through the base
URL, so this is only a deployment convenience, not a shortcut around the
GraphQL boundary.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("COLLECTOR_SOURCE_URL", "http://127.0.0.1:8000/source")

from fastapi import FastAPI  # noqa: E402

from app.main import create_app as create_collector_app  # noqa: E402
from app.source import create_source_app  # noqa: E402

app = FastAPI(title="Catalog collection demo")
# Order matters: the more specific mount wins, the collector app answers at "/"
# so its own routes keep their "/api/..." prefixes.
app.mount("/source", create_source_app())
app.mount("/", create_collector_app())
