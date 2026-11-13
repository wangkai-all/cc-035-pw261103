"""End-to-end tests for the HTTP API and the live source service.

The collector app is exercised through FastAPI's TestClient, but it still
talks to the source over real HTTP on a temporary port -- the same code path
as production, not a patched-out shortcut.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_source():
    """Run the standalone source app on a real port for API-level tests."""
    from app.source import app as source_app

    port = _free_port()
    config = uvicorn.Config(source_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "source server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def api(tmp_path, monkeypatch, live_source):
    import app.main as main_module

    monkeypatch.setattr(main_module, "SOURCE_URL", live_source)
    monkeypatch.setattr(main_module, "GRAPHQL_URL", f"{live_source}/graphql")
    monkeypatch.setattr(main_module, "_db", main_module.Database(tmp_path / "api.sqlite3"))
    from fastapi.testclient import TestClient

    with TestClient(main_module.create_app()) as client:
        yield client
    main_module._db.close()
    main_module._db = None


def test_health_reports_source(api):
    body = api.get("/api/health").json()
    assert body["ok"] is True
    assert body["source_reachable"] is True


def test_create_batch_collects_everything(api):
    created = api.post("/api/batches", json={"source_label": "e2e", "page_size": 2}).json()
    assert created["run"]["state"] == "completed"
    tree = api.get(f"/api/batches/{created['batch_id']}").json()
    assert tree["complete"] is True
    assert tree["counts"] == {"store": 4, "product": 12, "sku": 48}
    root = tree["roots"][0]
    assert root["subtree_status"] == "complete"
    assert len(root["children"]) == 4
    assert len(root["children"][0]["children"]) == 3
    assert tree["batch"]["snapshot_id"] == created["snapshot_id"]


def test_fault_injection_via_api_pauses_one_branch_then_retries(api):
    created = api.post("/api/batches", json={"source_label": "e2e", "page_size": 2}).json()
    batch_id = created["batch_id"]
    assert created["run"]["state"] == "completed"

    # Start a second batch and make one SKU branch fail for one page.
    second = api.post("/api/batches", json={"source_label": "e2e", "page_size": 2, "auto_run": False}).json()
    api.post("/api/source/rules", json={
        "mode": "stalled_cursor",
        "scope": "/sku/S-2/P-3",
        "times": 2,
    })
    run = api.post(f"/api/batches/{second['batch_id']}/run", json={}).json()
    assert run["state"] == "paused"
    tree = api.get(f"/api/batches/{second['batch_id']}").json()
    stalled = [
        p for s in tree["roots"][0]["children"] for p in s["children"]
        if p["status"] == "stalled"
    ]
    assert len(stalled) == 1
    assert "does not advance" or "did not advance" in stalled[0]["last_error"]

    # Clear the fault and retry only this branch.
    api.post("/api/source/rules/clear")
    retried = api.post(f"/api/batches/{second['batch_id']}/retry", json={
        "branch_path": stalled[0]["path"],
        "run_after": True,
    }).json()
    assert retried["run"]["state"] == "completed"
    tree_after = api.get(f"/api/batches/{second['batch_id']}").json()
    assert tree_after["complete"] is True
    assert tree_after["counts"]["sku"] == 48


def test_partial_error_keeps_other_branches(api):
    created = api.post(
        "/api/batches",
        json={"source_label": "e2e", "page_size": 3, "auto_run": False},
    ).json()
    api.post("/api/source/rules", json={
        "mode": "partial_error",
        "scope": "/store",
        "at_offset": 0,
        "times": 1,
        "message": "one store's products are unavailable",
    })
    api.post(f"/api/batches/{created['batch_id']}/run", json={})
    tree = api.get(f"/api/batches/{created['batch_id']}").json()
    store_children = tree["roots"][0]["children"]
    failed = [s for s in store_children if s["status"] == "failed"]
    assert len(failed) == 1
    # Other stores still collected their data.
    assert tree["counts"]["sku"] > 0
    export = api.get(f"/api/batches/{created['batch_id']}/export").json()
    assert export["complete"] is False
    assert len(export["incomplete_branches"]) >= 1


def test_snapshot_expiry_and_successor(api):
    created = api.post("/api/batches", json={"source_label": "e2e", "page_size": 3, "auto_run": False}).json()
    batch_id = created["batch_id"]
    api.post(f"/api/batches/{batch_id}/run", json={"max_requests": 1})
    api.post(f"/api/source/snapshots/{created['snapshot_id']}/expire")
    report = api.post(f"/api/batches/{batch_id}/resume", json={}).json()
    assert report["state"] == "expired"
    refused = api.post(f"/api/batches/{batch_id}/resume", json={})
    assert refused.status_code == 409

    successor = api.post(f"/api/batches/{batch_id}/successor", json={"auto_run": True})
    assert successor.status_code == 200
    body = successor.json()
    assert body["snapshot_id"] != created["snapshot_id"]
    tree_new = api.get(f"/api/batches/{body['batch_id']}").json()
    assert tree_new["complete"] is True
    old_tree = api.get(f"/api/batches/{batch_id}").json()
    assert old_tree["batch"]["superseded_by"] == body["batch_id"]


def test_export_lists_completed_branches(api):
    created = api.post("/api/batches", json={"source_label": "e2e", "page_size": 2}).json()
    export = api.get(f"/api/batches/{created['batch_id']}/export").json()
    assert export["complete"] is True
    assert export["branches"]["unfinished"] == 0
    kinds = {b["kind"] for b in export["complete_branches"]}
    assert kinds == {"store_page", "product_page", "sku_page"}
    assert export["data"]["skus"]
    assert export["counts"]["stores"] == 4


def test_resume_endpoint_reuses_batch(api):
    created = api.post("/api/batches", json={"source_label": "e2e", "page_size": 2, "auto_run": False}).json()
    api.post(f"/api/batches/{created['batch_id']}/run", json={"max_requests": 3})
    resume = api.post(f"/api/batches/{created['batch_id']}/resume", json={}).json()
    assert resume["state"] == "completed"
    tree = api.get(f"/api/batches/{created['batch_id']}").json()
    assert tree["batch"]["id"] == created["batch_id"]
    assert tree["batch"]["snapshot_id"] == created["snapshot_id"]
    assert tree["complete"] is True
