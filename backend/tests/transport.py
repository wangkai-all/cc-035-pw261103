"""In-process ASGI transport so the collector speaks to the source app."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient


class ASGITransport(httpx.BaseTransport):
    def __init__(self, app):
        self._client = TestClient(app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={k: v for k, v in request.headers.items() if k != "host"},
        )
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )
