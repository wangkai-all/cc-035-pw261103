"""FastAPI application: batch control, progress tree, export.

Routes are grouped under ``/api``.  The progress tree is always rebuilt from
SQLite so the UI reflects durable state, not a process-local cache.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .collector import Collector, GraphQLClient
from .db import Database
from .tree import build_tree, export_batch

SOURCE_URL = os.environ.get("COLLECTOR_SOURCE_URL", "http://127.0.0.1:8001").rstrip("/")
GRAPHQL_URL = f"{SOURCE_URL}/graphql"
DB_PATH = os.environ.get(
    "COLLECTOR_DB", str(Path(__file__).resolve().parents[2] / "data" / "collector.sqlite3")
)

_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(DB_PATH)
    return _db


def get_collector(db: Database = Depends(get_db)) -> Collector:
    return Collector(db, GraphQLClient(GRAPHQL_URL), SOURCE_URL)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateBatchRequest(BaseModel):
    source_label: str = Field(default="demo-source", min_length=1)
    snapshot_id: str | None = None
    page_size: int = Field(default=2, ge=1, le=50)
    auto_run: bool = True
    max_requests: int = Field(default=500, ge=1, le=5000)
    label: str = ""


class RunRequest(BaseModel):
    max_requests: int = Field(default=500, ge=1, le=5000)
    branch_path: str | None = None


class RetryRequest(BaseModel):
    branch_path: str
    include_descendants: bool = False
    run_after: bool = True
    # Restrict the run to this subtree; by default the run also finishes
    # other runnable branches, which is safe because complete branches are
    # never re-collected.
    scope_to_branch: bool = False
    clear_cursor: bool = False
    max_requests: int = Field(default=500, ge=1, le=5000)


class SuccessorRequest(BaseModel):
    page_size: int | None = None
    label: str = ""
    auto_run: bool = False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title="Catalog collection service")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health(db: Database = Depends(get_db)) -> dict[str, Any]:
        source_ok = False
        try:
            source_ok = httpx.get(f"{SOURCE_URL}/admin/summary", timeout=2).status_code == 200
        except Exception:  # noqa: BLE001
            source_ok = False
        return {
            "ok": True,
            "source_url": SOURCE_URL,
            "source_reachable": source_ok,
            "db_path": db.path,
        }

    # -- source passthrough -------------------------------------------------

    @app.get("/api/source")
    def source_summary() -> dict[str, Any]:
        try:
            return httpx.get(f"{SOURCE_URL}/admin/summary", timeout=5).json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Local source unreachable at {SOURCE_URL}: {exc}") from exc

    @app.post("/api/source/rules")
    def add_rule(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = httpx.post(f"{SOURCE_URL}/admin/rules", json=payload, timeout=5)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Local source unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, resp.text)
        return resp.json()

    @app.post("/api/source/rules/clear")
    def clear_rules() -> dict[str, Any]:
        try:
            return httpx.post(f"{SOURCE_URL}/admin/rules/clear", timeout=5).json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Local source unreachable: {exc}") from exc

    @app.post("/api/source/snapshots/{snapshot_id}/expire")
    def expire_snapshot(snapshot_id: str) -> dict[str, Any]:
        try:
            resp = httpx.post(
                f"{SOURCE_URL}/admin/snapshots/{snapshot_id}/expire", timeout=5
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Local source unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, resp.text)
        return resp.json()

    @app.post("/api/source/mutate")
    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        """Mutate the *base* dataset; existing snapshots must not change."""
        action = payload.get("action")
        target = {
            "sku-price": "sku-price",
            "store-name": "store-name",
            "add-product": "add-product",
        }.get(str(action))
        if not target:
            raise HTTPException(400, "unknown mutation")
        body = {k: v for k, v in payload.items() if k != "action"}
        try:
            resp = httpx.post(
                f"{SOURCE_URL}/admin/mutate/{target}", json=body, timeout=5
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Local source unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, resp.text)
        return resp.json()

    # -- batches ------------------------------------------------------------

    @app.post("/api/batches")
    def create_batch(
        req: CreateBatchRequest, col: Collector = Depends(get_collector),
    ) -> dict[str, Any]:
        snapshot_id = req.snapshot_id
        if not snapshot_id:
            try:
                resp = httpx.post(
                    f"{SOURCE_URL}/admin/snapshots",
                    json={"label": req.label or req.source_label},
                    timeout=5,
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(503, f"Local source unreachable: {exc}") from exc
            if resp.status_code >= 400:
                raise HTTPException(502, resp.text)
            snapshot_id = resp.json()["id"]

        info = col.fetch_snapshot_info(snapshot_id)
        if info is None:
            raise HTTPException(400, f"Snapshot {snapshot_id} is unknown or expired")
        if info.get("expired"):
            raise HTTPException(409, f"Snapshot {snapshot_id} is already expired")

        batch_id = col.create_batch(
            snapshot_id=snapshot_id,
            source_label=req.source_label,
            page_size=req.page_size,
            label=req.label,
        )
        result: dict[str, Any] = {"batch_id": batch_id, "snapshot_id": snapshot_id}
        if req.auto_run:
            report = col.run(batch_id, max_requests=req.max_requests)
            result["run"] = report.as_dict()
        return result

    @app.get("/api/batches")
    def list_batches(db: Database = Depends(get_db)) -> dict[str, Any]:
        return {"batches": [_batch_summary(db, b) for b in db.batches()]}

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
        try:
            return build_tree(db, batch_id)
        except KeyError as exc:
            raise HTTPException(404, f"Unknown batch {batch_id}") from exc

    @app.post("/api/batches/{batch_id}/run")
    def run_batch(
        batch_id: str, req: RunRequest, col: Collector = Depends(get_collector),
    ) -> dict[str, Any]:
        try:
            report = col.run(
                batch_id, max_requests=req.max_requests, branch_prefix=req.branch_path
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return report.as_dict()

    @app.post("/api/batches/{batch_id}/retry")
    def retry_branch(
        batch_id: str, req: RetryRequest, col: Collector = Depends(get_collector),
    ) -> dict[str, Any]:
        db = col.db
        if db.batch(batch_id) is None:
            raise HTTPException(404, f"Unknown batch {batch_id}")
        from .collector import resolve_branch_path

        if resolve_branch_path(db, batch_id, req.branch_path) is None:
            raise HTTPException(404, f"Unknown branch {req.branch_path}")
        count = col.retry_branch(
            batch_id, req.branch_path, include_descendants=req.include_descendants,
            clear_cursor=req.clear_cursor,
        )
        out: dict[str, Any] = {"reset_branches": count}
        if req.run_after:
            report = col.run(
                batch_id,
                max_requests=req.max_requests,
                branch_prefix=req.branch_path if req.scope_to_branch else None,
            )
            out["run"] = report.as_dict()
        return out

    @app.post("/api/batches/{batch_id}/resume")
    def resume_batch(
        batch_id: str, req: RunRequest, col: Collector = Depends(get_collector),
    ) -> dict[str, Any]:
        """Resume the *same* batch and the same snapshot after an interruption."""
        db = col.db
        batch = db.batch(batch_id)
        if batch is None:
            raise HTTPException(404, f"Unknown batch {batch_id}")
        if batch["state"] == "expired":
            raise HTTPException(
                409,
                "Batch snapshot expired; open a successor batch with a new snapshot",
            )
        if batch["state"] in ("completed", "aborted"):
            db.reopen_batch(batch_id)
        report = col.run(batch_id, max_requests=req.max_requests)
        return report.as_dict()

    @app.post("/api/batches/{batch_id}/successor")
    def create_successor(
        batch_id: str, req: SuccessorRequest, col: Collector = Depends(get_collector),
    ) -> dict[str, Any]:
        """End a batch whose snapshot expired and start a fresh one.

        The new batch gets a *new* snapshot id; the old batch is marked
        superseded and keeps its partial data for audit.
        """
        db = col.db
        old = db.batch(batch_id)
        if old is None:
            raise HTTPException(404, f"Unknown batch {batch_id}")
        try:
            resp = httpx.post(
                f"{SOURCE_URL}/admin/snapshots",
                json={"label": req.label or old["source_label"]}, timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Local source unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise HTTPException(502, resp.text)
        snapshot_id = resp.json()["id"]
        new_batch = col.create_batch(
            snapshot_id=snapshot_id,
            source_label=old["source_label"],
            page_size=req.page_size or old["page_size"],
            parent_batch_id=batch_id,
            label=req.label,
        )
        db.mark_superseded(batch_id, new_batch)
        db.set_batch_state(
            batch_id, "expired" if old["state"] == "expired" else "aborted",
            f"Superseded by {new_batch}",
        )
        out: dict[str, Any] = {"batch_id": new_batch, "snapshot_id": snapshot_id}
        if req.auto_run:
            out["run"] = col.run(new_batch).as_dict()
        return out

    @app.get("/api/batches/{batch_id}/export")
    def export(batch_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
        try:
            return export_batch(db, batch_id)
        except KeyError as exc:
            raise HTTPException(404, f"Unknown batch {batch_id}") from exc

    @app.get("/api/batches/{batch_id}/requests")
    def request_journal(
        batch_id: str, branch_path: str | None = None, db: Database = Depends(get_db),
    ) -> dict[str, Any]:
        if db.batch(batch_id) is None:
            raise HTTPException(404, f"Unknown batch {batch_id}")
        return {"requests": db.requests(batch_id, branch_path)}

    return app


def _batch_summary(db: Database, batch: Any) -> dict[str, Any]:
    rows = db.branches(batch["id"])
    unfinished = [r for r in rows if r["status"] != "complete"]
    counts = db.record_counts(batch["id"])
    return {
        "id": batch["id"],
        "snapshot_id": batch["snapshot_id"],
        "source_label": batch["source_label"],
        "state": batch["state"],
        "note": batch["note"],
        "created_at": batch["created_at"],
        "closed_at": batch["closed_at"],
        "snapshot_expires_at": batch["snapshot_expires_at"],
        "parent_batch_id": batch["parent_batch_id"],
        "superseded_by": batch["superseded_by"],
        "branches_total": len(rows),
        "branches_complete": len(rows) - len(unfinished),
        "branches_unfinished": len(unfinished),
        "records": counts,
    }


app = create_app()
