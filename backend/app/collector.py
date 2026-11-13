"""Collector: walks store -> product -> SKU page by page.

Contract implemented here:

* a batch pins one ``snapshot_id`` and every request in it sends that id; a
  cursor minted for another snapshot is rejected by the source;
* ``hasNextPage`` requires a non-null ``endCursor`` that actually advances;
  a repeated/stalled cursor pauses the branch instead of looping;
* HTTP 200 carrying ``errors`` is a failure signal.  If an error names a
  path, only the branch at that path is paused and every other branch's data
  is kept.  A pathless error, a foreign path, or an error on the whole
  operation taints the request: nothing from it is counted as complete;
* a paused branch is retried on its own; sibling results are untouched.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .db import Database

STORE_QUERY = """
query StorePage($snapshotId: String!, $cursor: String, $pageSize: Int) {
  storePage(snapshotId: $snapshotId, cursor: $cursor, pageSize: $pageSize) {
    nodes { id name city productCount }
    endCursor
    hasNextPage
    totalCount
    snapshotId
  }
}
"""

PRODUCT_QUERY = """
query ProductPage($snapshotId: String!, $storeId: String!, $cursor: String, $pageSize: Int) {
  productPage(snapshotId: $snapshotId, storeId: $storeId, cursor: $cursor, pageSize: $pageSize) {
    nodes { id title category skuCount }
    endCursor
    hasNextPage
    totalCount
    snapshotId
  }
}
"""

SKU_QUERY = """
query SkuPage($snapshotId: String!, $storeId: String!, $productId: String!, $cursor: String, $pageSize: Int) {
  skuPage(snapshotId: $snapshotId, storeId: $storeId, productId: $productId, cursor: $cursor, pageSize: $pageSize) {
    nodes { id code price stock }
    endCursor
    hasNextPage
    totalCount
    snapshotId
  }
}
"""

SNAPSHOT_INFO_QUERY = """
query SnapshotInfo($snapshotId: String!) {
  snapshotInfo(snapshotId: $snapshotId) {
    id label createdAt expiresAt expired storeCount productCount skuCount
  }
}
"""

OPERATION_FIELDS = {
    "store_page": "storePage",
    "product_page": "productPage",
    "sku_page": "skuPage",
}

# Error codes the collector treats as data-integrity signals rather than
# branch attribution: the payload is still usable, the cursor contract check
# will catch the actual defect.
PAGINATION_CODES = {"CURSOR_STALLED", "PAGINATION_CONTRACT"}
EXPIRY_CODES = {"SNAPSHOT_EXPIRED", "SNAPSHOT_UNKNOWN", "CURSOR_SNAPSHOT_MISMATCH"}


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class GraphQLResponse:
    http_status: int | None
    data: dict[str, Any] | None
    errors: list[dict[str, Any]]
    raw: str = ""
    transport_error: str | None = None

    def codes(self) -> set[str]:
        return {
            str((e.get("extensions") or {}).get("code") or "")
            for e in self.errors
        }

    def attributable_errors(self) -> list[dict[str, Any]]:
        return [
            e for e in self.errors
            if ((e.get("extensions") or {}).get("code") or "") not in PAGINATION_CODES
        ]


@dataclass
class Verdict:
    """What a single response means for the branch tree."""

    expired: bool = False
    fatal: bool = False
    affected: list[str] = field(default_factory=list)  # branch paths
    summary: str = ""
    error_paths: list[list[Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.expired or self.fatal or self.affected)


@dataclass
class RunReport:
    batch_id: str
    requests: int = 0
    state: str = ""
    note: str = ""
    stopped_because: str = ""
    last_request: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "requests": self.requests,
            "state": self.state,
            "note": self.note,
            "stopped_because": self.stopped_because,
            "last_request": self.last_request,
        }


class GraphQLClient:
    """Minimal GraphQL-over-HTTP client.

    It never raises on GraphQL errors: a 200 that carries ``errors`` is a
    normal response shape interpreted by the caller.
    """

    def __init__(self, endpoint: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        self.endpoint = endpoint
        self.timeout = timeout
        self._transport = transport

    def execute(self, query: str, variables: dict[str, Any]) -> GraphQLResponse:
        try:
            kwargs: dict[str, Any] = {"timeout": self.timeout}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            with httpx.Client(**kwargs) as client:
                resp = client.post(
                    self.endpoint, json={"query": query, "variables": variables}
                )
        except Exception as exc:  # noqa: BLE001 - transport errors are data
            return GraphQLResponse(None, None, [], "", f"{type(exc).__name__}: {exc}")
        if resp.status_code != 200:
            return GraphQLResponse(resp.status_code, None, [], resp.text, f"HTTP {resp.status_code}")
        try:
            body = json.loads(resp.text)
        except json.JSONDecodeError:
            return GraphQLResponse(200, None, [], resp.text, "Response was not JSON")
        if not isinstance(body, dict):
            return GraphQLResponse(200, None, [], resp.text, "Response was not an object")
        errors = body.get("errors") or []
        if not isinstance(errors, list):
            errors = [{"message": str(errors)}]
        return GraphQLResponse(200, body.get("data"), errors, resp.text)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

ROOT_PATH = "/stores"


def product_branch_path(store_id: str) -> str:
    return f"/stores/{store_id}/products"


def sku_branch_path(store_id: str, product_id: str) -> str:
    return f"/stores/{store_id}/products/{product_id}/skus"


def store_record_path(store_id: str) -> str:
    return f"/stores/{store_id}"


def product_record_path(store_id: str, product_id: str) -> str:
    return f"/stores/{store_id}/products/{product_id}"


def sku_record_path(store_id: str, product_id: str, sku_id: str) -> str:
    return f"/stores/{store_id}/products/{product_id}/skus/{sku_id}"


def branch_depth(kind: str) -> int:
    return {"store_page": 0, "product_page": 1, "sku_page": 2}[kind]


def resolve_branch_path(db: Database, batch_id: str, path: str) -> str | None:
    """Accept a branch path or a tree-node path and return the branch path.

    The UI selects tree nodes (``/stores/S-1``, ``/stores/S-1/products/P-2``)
    while the retry/run machinery addresses page sequences
    (``/stores/S-1/products``, ``.../skus``).  This bridge keeps both usable
    without leaking one naming scheme into the other.
    """
    if db.branch(batch_id, path) is not None:
        return path
    for candidate in (path + "/products", path + "/skus"):
        if db.branch(batch_id, candidate) is not None:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class Collector:
    # One lock per batch id, shared by every Collector instance in this
    # process, so two overlapping API calls cannot walk the same batch and
    # reconcile each other's in-flight requests.
    _batch_locks: dict[str, threading.RLock] = {}
    _batch_locks_guard = threading.Lock()

    def __init__(self, db: Database, client: GraphQLClient, source_url: str = ""):
        self.db = db
        self.client = client
        self.source_url = source_url

    @classmethod
    def batch_lock(cls, batch_id: str) -> threading.RLock:
        with cls._batch_locks_guard:
            lock = cls._batch_locks.get(batch_id)
            if lock is None:
                lock = threading.RLock()
                cls._batch_locks[batch_id] = lock
            return lock

    # -- batch lifecycle -------------------------------------------------

    def fetch_snapshot_info(self, snapshot_id: str) -> dict[str, Any] | None:
        resp = self.client.execute(SNAPSHOT_INFO_QUERY, {"snapshotId": snapshot_id})
        if resp.transport_error or resp.errors:
            return None
        info = (resp.data or {}).get("snapshotInfo")
        return info if isinstance(info, dict) else None

    def create_batch(
        self,
        snapshot_id: str,
        source_label: str,
        page_size: int = 2,
        parent_batch_id: str | None = None,
        label: str = "",
    ) -> str:
        now = datetime.now(timezone.utc)
        batch_id = f"batch-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        info = self.fetch_snapshot_info(snapshot_id)
        expires_at = info.get("expiresAt") if info else None
        self.db.create_batch(
            batch_id, snapshot_id, source_label, page_size, expires_at, parent_batch_id
        )
        self.db.upsert_branch(
            batch_id=batch_id, path=ROOT_PATH, kind="store_page", parent_path=None,
            store_id=None, product_id=None, depth=0,
            label=label or "Store directory", status="pending",
        )
        return batch_id

    def _snapshot_expired_locally(self, batch: Any) -> bool:
        expires = batch["snapshot_expires_at"]
        if not expires:
            return False
        try:
            return datetime.fromisoformat(expires) <= datetime.now(timezone.utc)
        except ValueError:
            return False

    # -- driving the batch ----------------------------------------------

    def run(
        self, batch_id: str, max_requests: int = 500, branch_prefix: str | None = None
    ) -> RunReport:
        """Drive the batch until done, paused, expired, or out of budget.

        ``branch_prefix`` restricts the walk to one subtree: that is how a
        single failed branch is repaired without touching other results.
        """
        with self.batch_lock(batch_id):
            return self._run_locked(batch_id, max_requests, branch_prefix)

    def _run_locked(
        self, batch_id: str, max_requests: int, branch_prefix: str | None
    ) -> RunReport:
        batch = self.db.batch(batch_id)
        if not batch:
            raise ValueError(f"Unknown batch {batch_id}")
        self.db.reconcile_in_flight(batch_id)
        if branch_prefix is not None:
            branch_prefix = resolve_branch_path(self.db, batch_id, branch_prefix) or branch_prefix
        report = RunReport(batch_id=batch_id)

        while report.requests < max_requests:
            batch = self.db.batch(batch_id)
            if batch["state"] != "active":
                report.state = batch["state"]
                report.note = batch["note"]
                report.stopped_because = f"batch is {batch['state']}"
                break
            if self._snapshot_expired_locally(batch):
                self._expire(batch_id, "Snapshot expired before the next page could be requested")
                report.state = "expired"
                report.note = "Snapshot expired"
                report.stopped_because = "snapshot expired"
                break

            branch = self._next_branch(batch_id, branch_prefix)
            if branch is None:
                state, note = self._finalize(batch_id)
                report.state = state
                report.note = note
                report.stopped_because = "no runnable branches"
                break

            outcome, detail = self._collect_page(batch_id, branch)
            report.requests += 1
            report.last_request = {"branch": branch["path"], "outcome": outcome, **detail}
            if outcome == "expired":
                report.state = "expired"
                report.note = "Snapshot expired"
                report.stopped_because = "source reported an expired snapshot"
                break

        if report.requests >= max_requests:
            report.stopped_because = "request budget exhausted"

        batch = self.db.batch(batch_id)
        report.state = batch["state"]
        report.note = batch["note"]
        return report

    def _next_branch(self, batch_id: str, prefix: str | None):
        complete_paths = {
            row["path"] for row in self.db.branches(batch_id) if row["status"] == "complete"
        }
        for row in self.db.runnable_branches(batch_id):
            if prefix is not None and not (
                row["path"] == prefix or row["path"].startswith(prefix + "/")
            ):
                continue
            if row["parent_path"] and row["parent_path"] not in complete_paths:
                continue  # finish the parent page sequence first
            return row
        return None

    # -- one page --------------------------------------------------------

    def _collect_page(self, batch_id: str, branch: Any) -> tuple[str, dict[str, Any]]:
        batch = self.db.batch(batch_id)
        snapshot_id = batch["snapshot_id"]
        kind = branch["kind"]
        cursor = branch["cursor"]
        variables: dict[str, Any] = {
            "snapshotId": snapshot_id,
            "cursor": cursor,
            "pageSize": batch["page_size"],
        }
        if kind == "store_page":
            query = STORE_QUERY
        elif kind == "product_page":
            query = PRODUCT_QUERY
            variables["storeId"] = branch["store_id"]
        else:
            query = SKU_QUERY
            variables["storeId"] = branch["store_id"]
            variables["productId"] = branch["product_id"]

        attempt = self.db.attempts(batch_id, branch["path"]) + 1
        request_id = self.db.start_request(batch_id, branch["path"], kind, attempt, cursor)
        resp = self.client.execute(query, variables)

        if resp.transport_error or (resp.http_status is not None and resp.http_status != 200):
            message = resp.transport_error or f"HTTP {resp.http_status}"
            self._fail_request(
                batch_id, branch, request_id, message,
                http_status=resp.http_status, requested_cursor=cursor,
            )
            return "failed", {"error": message}

        payload = (resp.data or {}).get(OPERATION_FIELDS[kind]) if resp.data else None
        data_usable = isinstance(payload, dict) and isinstance(payload.get("nodes"), list)
        nodes: list[dict[str, Any]] = payload["nodes"] if data_usable else []

        verdict = self.analyze(kind, branch, resp, nodes)

        if verdict.expired:
            self.db.finish_request(
                request_id, "expired", http_status=resp.http_status,
                requested_cursor=cursor, error_summary=verdict.summary,
                error_paths=verdict.error_paths,
            )
            self.db.update_branch(
                batch_id, branch["path"],
                last_error="Snapshot expired: " + (verdict.summary or "unknown reason"),
            )
            self._expire(batch_id, "Source reported snapshot expiry")
            return "expired", {"error": verdict.summary}

        if verdict.fatal or not data_usable:
            message = verdict.summary if data_usable else (
                verdict.summary or "Response had no usable page data"
            )
            self._fail_request(
                batch_id, branch, request_id, message,
                http_status=resp.http_status, requested_cursor=cursor,
                error_paths=verdict.error_paths,
            )
            return "failed", {"error": message}

        next_cursor = payload.get("endCursor")
        has_next = bool(payload.get("hasNextPage"))
        total = payload.get("totalCount")
        total = total if isinstance(total, int) else None

        # Which affected path is this branch itself, vs a child branch?
        self_hit = branch["path"] in verdict.affected
        child_hits = [p for p in verdict.affected if p != branch["path"]]

        if not self_hit:
            stall = self._cursor_violation(
                batch_id, branch["path"], cursor, next_cursor, has_next
            )
            if stall is None and "CURSOR_STALLED" in resp.codes():
                # The source declared the cursor does not advance.  On the
                # first page there is no requested cursor to compare against,
                # so trust the explicit signal instead of silently re-reading
                # that page.
                stall = self._pagination_message(resp) or "Source reported a stalled cursor"
            if stall:
                rows = self._save_nodes(batch_id, branch, nodes)
                self.db.finish_request(
                    request_id, "cursor_stalled", http_status=resp.http_status,
                    requested_cursor=cursor, next_cursor=next_cursor,
                    has_next_page=has_next, rows_added=rows,
                    error_summary=stall, error_paths=verdict.error_paths,
                )
                self.db.update_branch(
                    batch_id, branch["path"], status="stalled", last_error=stall,
                    cursor=cursor, set_cursor=True, has_next=True,
                    pages_delta=0, total_count=total,
                )
                return "cursor_stalled", {"error": stall}

        rows = self._save_nodes(batch_id, branch, nodes)
        paused_children = self._mark_children_failed(batch_id, branch, verdict, nodes)

        if self_hit:
            # The page's own branch errored.  Keep the rows we did receive
            # (they de-duplicate on retry) but do not advance the cursor and
            # do not count this page as done -- the next retry re-reads it.
            outcome = "partial" if rows else "failed"
            self.db.finish_request(
                request_id, outcome, http_status=resp.http_status,
                requested_cursor=cursor, next_cursor=cursor, has_next_page=has_next,
                rows_added=rows, error_summary=verdict.summary,
                error_paths=verdict.error_paths,
            )
            self.db.update_branch(
                batch_id, branch["path"], status="failed",
                last_error=verdict.summary or "Branch reported an error",
                cursor=cursor, set_cursor=True,
                pages_delta=0,
            )
            return outcome, {"error": verdict.summary, "rows": rows}

        # The branch itself is healthy.  Child branches named by error paths
        # are paused; the rest of this page's data is kept.
        status = "partial" if has_next else "complete"
        requests_had_error = bool(verdict.affected)
        self.db.finish_request(
            request_id, "partial" if requests_had_error else "complete",
            http_status=resp.http_status, requested_cursor=cursor,
            next_cursor=next_cursor, has_next_page=has_next, rows_added=rows,
            error_summary=verdict.summary or None,
            error_paths=verdict.error_paths,
        )
        self.db.update_branch(
            batch_id, branch["path"], status=status,
            cursor=next_cursor, set_cursor=True, has_next=has_next,
            pages_delta=1, total_count=total,
            clear_error=True,
        )
        outcome = "partial" if requests_had_error else "complete"
        return outcome, {"rows": rows, "paused_children": paused_children}

    # -- error interpretation -------------------------------------------

    def analyze(
        self, kind: str, branch: Any, resp: GraphQLResponse, nodes: list[dict[str, Any]]
    ) -> Verdict:
        """Map the errors array onto branch paths.

        Rules:
        * a path-less error, or a path outside this operation, or an error
          on the operation itself -> request-wide failure;
        * an error path that cannot be resolved against the returned nodes
          -> request-wide failure (we cannot tell what to retry);
        * otherwise the named child branch is the only thing paused.
        """
        verdict = Verdict()
        attributable = resp.attributable_errors()
        if not attributable:
            return verdict

        verdict.summary = "; ".join(
            str(e.get("message") or "GraphQL error") for e in attributable
        )[:2000]
        verdict.error_paths = [
            e["path"] for e in attributable if isinstance(e.get("path"), list)
        ]

        all_codes = resp.codes()
        if all_codes & EXPIRY_CODES:
            verdict.expired = True
            return verdict

        field_name = OPERATION_FIELDS[kind]

        pathless = [e for e in attributable if not isinstance(e.get("path"), list)]
        if pathless:
            verdict.fatal = True
            verdict.summary = "Error without a path: " + verdict.summary
            return verdict

        foreign = [p for p in verdict.error_paths if not p or p[0] != field_name]
        if foreign:
            verdict.fatal = True
            verdict.summary = "Error path does not belong to this request: " + verdict.summary
            return verdict

        for path in verdict.error_paths:
            resolved = self._resolve_path(kind, branch, path, nodes)
            if resolved is None:
                verdict.fatal = True
                verdict.affected = []
                verdict.summary = "Error path cannot be resolved: " + verdict.summary
                return verdict
            if resolved not in verdict.affected:
                verdict.affected.append(resolved)
        verdict.error_paths = [
            path for path in verdict.error_paths
            if self._resolve_path(kind, branch, path, nodes) is not None
        ]
        return verdict

    @staticmethod
    def _resolve_path(
        kind: str, branch: Any, path: list[Any], nodes: list[dict[str, Any]]
    ) -> str | None:
        if len(path) == 1:
            return None  # error on the operation as a whole
        if path[1] != "nodes" or len(path) < 3 or not isinstance(path[2], int):
            return None
        index = path[2]
        if kind == "sku_page":
            # No children to attribute to: the SKU branch itself is affected.
            return branch["path"] if index < len(nodes) else None
        if index >= len(nodes) or "id" not in nodes[index]:
            return None
        child_id = str(nodes[index]["id"])
        if kind == "store_page":
            return product_branch_path(child_id)
        return sku_branch_path(branch["store_id"], child_id)

    def _mark_children_failed(
        self, batch_id: str, branch: Any, verdict: Verdict, nodes: list[dict[str, Any]]
    ) -> list[str]:
        """Create/pause child branches named by an attributed error."""
        paused: list[str] = []
        if branch["kind"] == "sku_page":
            return paused
        child_kind = "product_page" if branch["kind"] == "store_page" else "sku_page"
        for error_path, child_path in zip(verdict.error_paths, verdict.affected):
            if child_path == branch["path"]:
                continue
            index = error_path[2] if len(error_path) >= 3 else len(nodes) - 1
            node = nodes[index] if isinstance(index, int) and index < len(nodes) else {}
            if child_kind == "product_page":
                store_id = str(node.get("id") or child_path.split("/")[2])
                product_id = None
                label = f"{node.get('name') or store_id} ({store_id})"
            else:
                store_id = branch["store_id"]
                product_id = str(node.get("id") or child_path.split("/")[4])
                label = f"{node.get('title') or product_id} ({product_id})"
            existing = self.db.branch(batch_id, child_path)
            if existing is None:
                self.db.upsert_branch(
                    batch_id=batch_id, path=child_path, kind=child_kind,
                    parent_path=branch["path"], store_id=store_id,
                    product_id=product_id, depth=branch_depth(child_kind),
                    label=label, status="failed",
                )
            self.db.update_branch(
                batch_id, child_path, status="failed",
                last_error=verdict.summary or "Partial source error",
            )
            paused.append(child_path)
        return paused

    # -- persistence ----------------------------------------------------

    def _save_nodes(self, batch_id: str, branch: Any, nodes: list[dict[str, Any]]) -> int:
        """Persist page nodes and enqueue their child branches.

        De-duplication is delegated to ``Database.add_record``, whose natural
        keys are scoped by store / product / SKU hierarchy.
        """
        added = 0
        kind = branch["kind"]
        for node in nodes:
            if kind == "store_page":
                store_id = str(node["id"])
                if self.db.add_record(
                    batch_id, store_record_path(store_id), "store", store_id, None, None,
                    {"id": store_id, "name": node.get("name"), "city": node.get("city")},
                ):
                    added += 1
                self.db.upsert_branch(
                    batch_id=batch_id, path=product_branch_path(store_id),
                    kind="product_page", parent_path=branch["path"], store_id=store_id,
                    product_id=None, depth=1,
                    label=f"{node.get('name') or store_id} ({store_id})",
                    status="pending",
                )
            elif kind == "product_page":
                store_id = branch["store_id"]
                product_id = str(node["id"])
                if self.db.add_record(
                    batch_id, product_record_path(store_id, product_id), "product",
                    store_id, product_id, None,
                    {"id": product_id, "title": node.get("title"),
                     "category": node.get("category"), "skuCount": node.get("skuCount")},
                ):
                    added += 1
                self.db.upsert_branch(
                    batch_id=batch_id, path=sku_branch_path(store_id, product_id),
                    kind="sku_page", parent_path=branch["path"], store_id=store_id,
                    product_id=product_id, depth=2,
                    label=f"{node.get('title') or product_id} ({product_id})",
                    status="pending",
                )
            else:
                store_id = branch["store_id"]
                product_id = branch["product_id"]
                sku_id = str(node["id"])
                if self.db.add_record(
                    batch_id, sku_record_path(store_id, product_id, sku_id), "sku",
                    store_id, product_id, sku_id,
                    {"id": sku_id, "code": node.get("code"),
                     "price": node.get("price"), "stock": node.get("stock")},
                ):
                    added += 1
        return added

    def _fail_request(
        self, batch_id: str, branch: Any, request_id: int, message: str,
        *, http_status: int | None, requested_cursor: str | None,
        error_paths: list[list[Any]] | None = None,
    ) -> None:
        """A request-wide failure. This page is NOT recorded as done.

        The cursor stays where it was, so a retry re-requests the same page
        instead of silently skipping data.
        """
        self.db.finish_request(
            request_id, "failed", http_status=http_status,
            requested_cursor=requested_cursor, next_cursor=requested_cursor,
            error_summary=message, error_paths=error_paths,
        )
        self.db.update_branch(
            batch_id, branch["path"], status="failed", last_error=message,
        )

    @staticmethod
    def _pagination_message(resp: GraphQLResponse) -> str:
        """Message of the pagination-contract error, if any.

        Pagination codes are excluded from ``attributable_errors`` (they are
        not branch attribution), so the message has to be read directly.
        """
        for err in resp.errors:
            code = ((err.get("extensions") or {}).get("code") or "")
            if code in PAGINATION_CODES:
                return str(err.get("message") or "")
        return ""

    def _cursor_violation(
        self, batch_id: str, branch_path: str, requested: str | None,
        next_cursor: str | None, has_next: bool,
    ) -> str | None:
        if not has_next:
            return None
        if not next_cursor:
            return "hasNextPage is true but endCursor is null"
        if requested and next_cursor == requested:
            return "endCursor did not advance (the same cursor came back)"
        if next_cursor in self.db.visited_cursors(batch_id, branch_path):
            return "endCursor repeats a cursor already used on this branch"
        return None

    def _expire(self, batch_id: str, note: str) -> None:
        self.db.set_batch_state(batch_id, "expired", note)

    def _finalize(self, batch_id: str) -> tuple[str, str]:
        unfinished = self.db.unfinished_branches(batch_id)
        if not unfinished:
            self.db.set_batch_state(batch_id, "completed", "All branches collected")
            return "completed", "All branches collected"
        paused = [r for r in unfinished if r["status"] in ("failed", "stalled")]
        runnable = [r for r in unfinished if r["status"] in ("pending", "partial")]
        note = f"{len(unfinished)} branch(es) unfinished"
        if paused:
            note += f", {len(paused)} paused for retry"
        if runnable:
            note += f", {len(runnable)} still runnable"
        state = "paused" if paused else "active"
        self.db.set_batch_state(batch_id, state, note)
        return state, note

    # -- repair ----------------------------------------------------------

    def retry_branch(
        self, batch_id: str, path: str, include_descendants: bool = False,
        *, clear_cursor: bool = False,
    ) -> int:
        """Reset a paused branch (or its subtree) so ``run`` picks it up."""
        branch_path = resolve_branch_path(self.db, batch_id, path) or path
        with self.batch_lock(batch_id):
            count = self.db.reset_branches_for_retry(
                batch_id, branch_path, include_descendants, clear_cursor=clear_cursor
            )
        batch = self.db.batch(batch_id)
        if batch and batch["state"] in ("paused", "completed", "expired"):
            self.db.reopen_batch(batch_id)
        return count

    def retry_all_paused(self, batch_id: str) -> int:
        rows = self.db.unfinished_branches(batch_id)
        count = 0
        for row in rows:
            if row["status"] in ("failed", "stalled"):
                count += self.db.reset_branches_for_retry(batch_id, row["path"], False)
        if count:
            self.db.reopen_batch(batch_id)
        return count
