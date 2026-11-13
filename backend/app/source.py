"""Local GraphQL source: versioned snapshots plus injectable faults.

Design notes:

* Each snapshot deep-copies the base catalogue.  Mutating the base data
  afterwards never leaks into an existing snapshot, which is the whole point
  of the ``snapshot_id`` contract.
* Cursors encode the offset *and* the snapshot id.  A cursor minted for one
  snapshot is rejected by another, so two versions can never be spliced
  together by accident.
* All three levels are independently paginated (``storePage``,
  ``productPage(storeId)``, ``skuPage(storeId, productId)``) so the collector
  can walk each branch with its own cursor.
* Fault injection is applied at the GraphQL envelope level: either inject
  ``errors`` next to intact ``data`` (partial failure), strip data and return
  errors only (whole-request failure), or corrupt pagination metadata.

The collector never imports this module; it speaks GraphQL over HTTP.
"""

from __future__ import annotations

import base64
import copy
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import strawberry
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from graphql import GraphQLError

# Snapshot expiry is a modelled domain outcome -- the collector is supposed to
# close the batch -- not a defect in this service.  Strawberry's execution
# logger would otherwise print a full traceback for every expired-snapshot
# request even though the HTTP envelope already carries the error.
DOMAIN_ERROR_CODES = frozenset({"SNAPSHOT_EXPIRED", "SNAPSHOT_UNKNOWN"})


class _DomainErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # StrawberryLogger passes the GraphQLError as the log *message*, not
        # as a format argument.
        code = (getattr(record.msg, "extensions", None) or {}).get("code")
        return code not in DOMAIN_ERROR_CODES


logging.getLogger("strawberry.execution").addFilter(_DomainErrorFilter())

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


def seed_data() -> dict[str, Any]:
    stores = []
    for s in range(1, 5):
        products = []
        for p in range(1, 4):
            skus = [
                {
                    "id": f"SKU-{k}",
                    "code": f"S{s}-P{p}-K{k}",
                    "price": float(10 * p + k),
                    "stock": 4 * k + s,
                }
                for k in range(1, 5)
            ]
            products.append(
                {
                    "id": f"P-{p}",
                    "title": f"Store {s} product {p}",
                    "category": ["outerwear", "footwear", "accessories"][p - 1],
                    "skus": skus,
                }
            )
        stores.append(
            {
                "id": f"S-{s}",
                "name": f"Store {s}",
                "city": ["Shanghai", "Hangzhou", "Nanjing", "Suzhou"][s - 1],
                "products": products,
            }
        )
    return {"stores": stores}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------


def encode_cursor(offset: int, snapshot_id: str) -> str:
    return base64.urlsafe_b64encode(f"v1|{offset}|{snapshot_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        version, offset_s, snap = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        if version != "v1":
            raise ValueError
        return int(offset_s), snap
    except Exception as exc:  # noqa: BLE001
        raise GraphQLError("Invalid cursor", extensions={"code": "CURSOR_INVALID"}) from exc


# ---------------------------------------------------------------------------
# Fault injection rules
# ---------------------------------------------------------------------------


FAULT_MODES = ("partial_error", "request_error", "stalled_cursor", "missing_end_cursor")


@dataclass
class FaultRule:
    id: str
    mode: str
    scope: str = "/store"  # prefix over "/store", "/product/S-1", "/sku/S-1/P-2"
    at_offset: int | None = None  # only this page offset (None = any)
    times: int | None = None  # fire at most N times; None = forever
    message: str = "Injected source fault"
    pathless: bool = False  # emit an error with no path (affects the request)
    remaining: int | None = None
    fired: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "scope": self.scope,
            "at_offset": self.at_offset,
            "times": self.times,
            "remaining": self.remaining,
            "fired": self.fired,
            "message": self.message,
            "pathless": self.pathless,
        }

    def matches(self, scope: str, offset: int) -> bool:
        if self.remaining == 0:
            return False
        if self.scope not in ("", "/") and not scope.startswith(self.scope):
            return False
        if self.at_offset is not None and self.at_offset != offset:
            return False
        return True

    def consume(self) -> None:
        self.fired += 1
        if self.remaining is not None:
            self.remaining -= 1


# ---------------------------------------------------------------------------
# Source state
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    id: str
    label: str
    created_at: str
    expires_at: str | None
    data: dict[str, Any] = field(repr=False)


class SourceState:
    """Mutable source state guarded by one lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._base: dict[str, Any] = seed_data()
        self._snapshots: dict[str, Snapshot] = {}
        self._rules: dict[str, FaultRule] = {}
        self._request_log: list[dict[str, Any]] = []
        self.version = 1
        self.default_page_size = 2

    # -- snapshots -----------------------------------------------------

    def create_snapshot(self, label: str = "", ttl_seconds: int | None = None) -> Snapshot:
        with self._lock:
            snap_id = f"snap-{uuid.uuid4().hex[:10]}"
            now = datetime.now(timezone.utc)
            snap = Snapshot(
                id=snap_id,
                label=label,
                created_at=now.isoformat(timespec="seconds"),
                expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
                if ttl_seconds
                else None,
                data=copy.deepcopy(self._base),
            )
            self._snapshots[snap_id] = snap
            return snap

    def snapshot(self, snap_id: str | None) -> Snapshot | None:
        with self._lock:
            return self._snapshots.get(snap_id or "")

    def is_expired(self, snap: Snapshot) -> bool:
        if not snap.expires_at:
            return False
        return datetime.fromisoformat(snap.expires_at) <= datetime.now(timezone.utc)

    def expire_snapshot(self, snap_id: str) -> bool:
        with self._lock:
            snap = self._snapshots.get(snap_id)
            if not snap:
                return False
            snap.expires_at = now_iso()
            return True

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": s.id,
                    "label": s.label,
                    "created_at": s.created_at,
                    "expires_at": s.expires_at,
                    "expired": self.is_expired(s),
                }
                for s in self._snapshots.values()
            ]

    # -- base data -----------------------------------------------------

    def mutate_sku_price(self, store_id: str, product_id: str, sku_id: str, price: float) -> bool:
        with self._lock:
            for store in self._base["stores"]:
                if store["id"] != store_id:
                    continue
                for product in store["products"]:
                    if product["id"] != product_id:
                        continue
                    for sku in product["skus"]:
                        if sku["id"] == sku_id:
                            sku["price"] = price
                            self.version += 1
                            return True
            return False

    def rename_store(self, store_id: str, name: str) -> bool:
        with self._lock:
            for store in self._base["stores"]:
                if store["id"] == store_id:
                    store["name"] = name
                    self.version += 1
                    return True
            return False

    def add_product(
        self, store_id: str, title: str | None = None, product_id: str | None = None
    ) -> str:
        with self._lock:
            for store in self._base["stores"]:
                if store["id"] != store_id:
                    continue
                number = len(store["products"]) + 1
                # Honour a caller-supplied id so a demo can target a stable
                # name; otherwise fall back to the next free P-<n>.
                if not product_id:
                    existing = {p["id"] for p in store["products"]}
                    product_id = f"P-{number}"
                    while product_id in existing:
                        number += 1
                        product_id = f"P-{number}"
                store["products"].append(
                    {
                        "id": product_id,
                        "title": title or f"Store {store_id} product {number}",
                        "category": "accessories",
                        "skus": [
                            {
                                "id": f"SKU-{k}",
                                "code": f"{store_id}-{product_id}-K{k}",
                                "price": float(k),
                                "stock": k,
                            }
                            for k in range(1, 4)
                        ],
                    }
                )
                self.version += 1
                return product_id
            raise KeyError(store_id)

    # -- faults --------------------------------------------------------

    def add_rule(self, payload: dict[str, Any]) -> FaultRule:
        mode = payload.get("mode")
        if mode not in FAULT_MODES:
            raise ValueError(f"mode must be one of {list(FAULT_MODES)}")
        with self._lock:
            times = payload.get("times")
            rule = FaultRule(
                id=str(payload.get("id") or f"rule-{uuid.uuid4().hex[:8]}"),
                mode=mode,
                scope=str(payload.get("scope") or "/store"),
                at_offset=payload.get("at_offset"),
                times=times,
                remaining=times,
                message=str(payload.get("message") or "Injected source fault"),
                pathless=bool(payload.get("pathless")),
            )
            self._rules[rule.id] = rule
            return rule

    def rules(self) -> list[FaultRule]:
        with self._lock:
            return list(self._rules.values())

    def clear_rules(self) -> None:
        with self._lock:
            self._rules.clear()

    def take_rule(self, scope: str, offset: int, only: tuple[str, ...] | None = None) -> FaultRule | None:
        """Return and consume the first matching rule."""
        with self._lock:
            for rule in self._rules.values():
                if only is not None and rule.mode not in only:
                    continue
                if rule.matches(scope, offset):
                    rule.consume()
                    return rule
            return None

    # -- observability -------------------------------------------------

    def remember_request(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._request_log.append(entry)
            if len(self._request_log) > 300:
                del self._request_log[:-300]

    def request_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._request_log)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self.version,
                "default_page_size": self.default_page_size,
                "stores": [
                    {
                        "store_id": s["id"],
                        "name": s["name"],
                        "products": len(s["products"]),
                        "skus": sum(len(p["skus"]) for p in s["products"]),
                    }
                    for s in self._base["stores"]
                ],
                "snapshots": self.list_snapshots(),
                "rules": [r.to_dict() for r in self._rules.values()],
            }


SOURCE = SourceState()


# ---------------------------------------------------------------------------
# GraphQL schema
# ---------------------------------------------------------------------------


@strawberry.type
class Sku:
    id: strawberry.ID
    code: str
    price: float
    stock: int


@strawberry.type
class SkuPage:
    nodes: list[Sku]
    end_cursor: str | None
    has_next_page: bool
    total_count: int
    snapshot_id: str


@strawberry.type
class Product:
    id: strawberry.ID
    title: str
    category: str
    sku_count: int


@strawberry.type
class ProductPage:
    nodes: list[Product]
    end_cursor: str | None
    has_next_page: bool
    total_count: int
    snapshot_id: str


@strawberry.type
class Store:
    id: strawberry.ID
    name: str
    city: str
    product_count: int


@strawberry.type
class StorePage:
    nodes: list[Store]
    end_cursor: str | None
    has_next_page: bool
    total_count: int
    snapshot_id: str


@strawberry.type
class SnapshotInfo:
    id: str
    label: str
    created_at: str
    expires_at: str | None
    expired: bool
    store_count: int
    product_count: int
    sku_count: int


@strawberry.type
class Query:
    @strawberry.field
    def store_page(
        self,
        snapshot_id: str,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> StorePage:
        snap = _require_snapshot(snapshot_id)
        nodes, end, has_next, total = _slice(
            snap.data["stores"], cursor, page_size, snapshot_id, "/store"
        )
        return StorePage(
            nodes=[
                Store(
                    id=s["id"],
                    name=s["name"],
                    city=s["city"],
                    product_count=len(s["products"]),
                )
                for s in nodes
            ],
            end_cursor=end,
            has_next_page=has_next,
            total_count=total,
            snapshot_id=snapshot_id,
        )

    @strawberry.field
    def product_page(
        self,
        snapshot_id: str,
        store_id: str,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> ProductPage:
        snap = _require_snapshot(snapshot_id)
        store = _find(snap.data["stores"], store_id)
        if not store:
            raise GraphQLError(
                f"Store {store_id} not found",
                path=["productPage"],
                extensions={"code": "NOT_FOUND"},
            )
        nodes, end, has_next, total = _slice(
            store["products"], cursor, page_size, snapshot_id, f"/product/{store_id}"
        )
        return ProductPage(
            nodes=[
                Product(
                    id=p["id"],
                    title=p["title"],
                    category=p["category"],
                    sku_count=len(p["skus"]),
                )
                for p in nodes
            ],
            end_cursor=end,
            has_next_page=has_next,
            total_count=total,
            snapshot_id=snapshot_id,
        )

    @strawberry.field
    def sku_page(
        self,
        snapshot_id: str,
        store_id: str,
        product_id: str,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> SkuPage:
        snap = _require_snapshot(snapshot_id)
        store = _find(snap.data["stores"], store_id)
        product = _find(store["products"], product_id) if store else None
        if not product:
            raise GraphQLError(
                f"Product {product_id} not found in {store_id}",
                path=["skuPage"],
                extensions={"code": "NOT_FOUND"},
            )
        nodes, end, has_next, total = _slice(
            product["skus"], cursor, page_size, snapshot_id,
            f"/sku/{store_id}/{product_id}",
        )
        return SkuPage(
            nodes=[Sku(**n) for n in nodes],
            end_cursor=end,
            has_next_page=has_next,
            total_count=total,
            snapshot_id=snapshot_id,
        )

    @strawberry.field
    def snapshot_info(self, snapshot_id: str) -> SnapshotInfo:
        snap = SOURCE.snapshot(snapshot_id)
        if not snap:
            raise GraphQLError("Unknown snapshot", extensions={"code": "SNAPSHOT_UNKNOWN"})
        return SnapshotInfo(
            id=snap.id,
            label=snap.label,
            created_at=snap.created_at,
            expires_at=snap.expires_at,
            expired=SOURCE.is_expired(snap),
            store_count=len(snap.data["stores"]),
            product_count=sum(len(s["products"]) for s in snap.data["stores"]),
            sku_count=sum(
                len(p["skus"]) for s in snap.data["stores"] for p in s["products"]
            ),
        )


def _find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return next((item for item in items if item["id"] == item_id), None)


def _require_snapshot(snapshot_id: str) -> Snapshot:
    snap = SOURCE.snapshot(snapshot_id)
    if not snap:
        raise GraphQLError("Unknown snapshot", extensions={"code": "SNAPSHOT_UNKNOWN"})
    if SOURCE.is_expired(snap):
        raise GraphQLError("Snapshot expired", extensions={"code": "SNAPSHOT_EXPIRED"})
    return snap


def _slice(
    items: list[Any],
    cursor: str | None,
    page_size: int | None,
    snapshot_id: str,
    scope: str,
) -> tuple[list[Any], str | None, bool, int]:
    """Slice one page, enforcing the cursor/snapshot contract.

    Fault injection for this page is handled by the envelope layer, which
    knows whether the request as a whole should fail.
    """
    size = page_size or SOURCE.default_page_size
    offset = 0
    if cursor:
        offset, cursor_snap = decode_cursor(cursor)
        if cursor_snap != snapshot_id:
            raise GraphQLError(
                "Cursor belongs to a different snapshot",
                extensions={"code": "CURSOR_SNAPSHOT_MISMATCH"},
            )
    nodes = items[offset : offset + size]
    has_next = offset + size < len(items)
    end = encode_cursor(offset + size, snapshot_id) if has_next else None
    return nodes, end, has_next, len(items)


schema = strawberry.Schema(query=Query)


# ---------------------------------------------------------------------------
# HTTP app: fault injection at the response-envelope level
# ---------------------------------------------------------------------------


def op_name(query: str) -> str:
    """GraphQL response key for the requested operation."""
    for name in ("storePage", "productPage", "skuPage", "snapshotInfo"):
        if name in query:
            return name
    return "unknown"


def request_scope(query: str, variables: dict[str, Any]) -> tuple[str, int]:
    # Metadata probes are not page requests: keep them in their own scope so a
    # page-level fault rule cannot fire on (and poison) a snapshotInfo lookup.
    if "snapshotInfo" in query:
        return "/snapshot", 0
    cursor = variables.get("cursor")
    offset = 0
    if cursor:
        try:
            offset, _ = decode_cursor(str(cursor))
        except GraphQLError:
            offset = 0
    store_id = variables.get("storeId")
    product_id = variables.get("productId")
    if "skuPage" in query and store_id and product_id:
        return f"/sku/{store_id}/{product_id}", offset
    if "productPage" in query and store_id:
        return f"/product/{store_id}", offset
    return "/store", offset


def _error_json(message: str, path: list[Any] | None, code: str) -> dict[str, Any]:
    body: dict[str, Any] = {"message": message, "extensions": {"code": code}}
    if path is not None:
        body["path"] = path
    return body


def create_source_app(state: SourceState | None = None) -> FastAPI:
    global SOURCE
    if state is not None:
        SOURCE = state

    app = FastAPI(title="Local catalog GraphQL source")

    @app.post("/graphql")
    async def graphql_endpoint(payload: dict[str, Any]) -> JSONResponse:
        query = payload.get("query") or ""
        variables = payload.get("variables") or {}
        operation = payload.get("operationName")
        scope, offset = request_scope(query, variables)

        # Whole-request failure rules are consumed before execution: no data
        # page is even attempted.
        rule = SOURCE.take_rule(scope, offset, only=("request_error",))
        if rule is not None:
            path = None if rule.pathless else [op_name(query)]
            body = {"data": None, "errors": [_error_json(rule.message, path, "INJECTED_REQUEST_ERROR")]}
            _remember(query, variables, body, scope)
            return JSONResponse(body)

        result = schema.execute_sync(
            query, variable_values=variables, operation_name=operation
        )
        body: dict[str, Any] = {}
        if result.errors:
            body["errors"] = [
                _from_graphql_error(e) for e in result.errors
            ]
        if result.data is not None:
            body["data"] = result.data

        data_ok = (
            isinstance(body.get("data"), dict)
            and op_name(query) in body["data"]
            and body["data"][op_name(query)] is not None
        )

        if data_ok:
            partial = SOURCE.take_rule(scope, offset, only=("partial_error",))
            if partial is not None:
                # Data is intact; an errors entry marks the failing sub-branch.
                field = op_name(query)
                path = None if partial.pathless else [field, "nodes", 0]
                body["errors"] = [
                    _error_json(partial.message, path, "INJECTED_PARTIAL_ERROR")
                ]

            payload_obj = body["data"][op_name(query)]
            if payload_obj.get("hasNextPage"):
                missing = SOURCE.take_rule(scope, offset, only=("missing_end_cursor",))
                if missing is not None:
                    # hasNextPage stays true while the cursor is removed; the
                    # client must refuse to guess where the next page starts.
                    payload_obj["endCursor"] = None
                    body["errors"] = [
                        _error_json(missing.message, [op_name(query)], "PAGINATION_CONTRACT")
                    ]
                else:
                    stalled = SOURCE.take_rule(scope, offset, only=("stalled_cursor",))
                    if stalled is not None:
                        # Return a cursor that decodes to the page just served,
                        # or the caller's own cursor: it cannot advance.
                        payload_obj["endCursor"] = (
                            variables.get("cursor")
                            or encode_cursor(offset, variables["snapshotId"])
                        )
                        body["errors"] = [
                            _error_json(stalled.message, [op_name(query)], "CURSOR_STALLED")
                        ]

        _remember(query, variables, body, scope)
        return JSONResponse(body)

    @app.get("/admin/summary")
    async def summary() -> dict[str, Any]:
        return SOURCE.summary()

    @app.post("/admin/snapshots")
    async def create_snapshot(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        snap = SOURCE.create_snapshot(
            label=str(payload.get("label") or ""),
            ttl_seconds=payload.get("ttl_seconds"),
        )
        return {
            "id": snap.id,
            "label": snap.label,
            "created_at": snap.created_at,
            "expires_at": snap.expires_at,
        }

    @app.get("/admin/snapshots")
    async def list_snapshots() -> dict[str, Any]:
        return {"snapshots": SOURCE.list_snapshots()}

    @app.post("/admin/snapshots/{snap_id}/expire")
    async def expire_snapshot(snap_id: str) -> dict[str, Any]:
        if not SOURCE.expire_snapshot(snap_id):
            raise HTTPException(status_code=404, detail="snapshot not found")
        return {"expired": snap_id}

    @app.post("/admin/rules")
    async def add_rule(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return SOURCE.add_rule(payload).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/admin/rules")
    async def get_rules() -> dict[str, Any]:
        return {"rules": [r.to_dict() for r in SOURCE.rules()]}

    @app.post("/admin/rules/clear")
    async def clear_rules() -> dict[str, Any]:
        SOURCE.clear_rules()
        return {"cleared": True}

    @app.post("/admin/mutate/sku-price")
    async def mutate_sku(payload: dict[str, Any]) -> dict[str, Any]:
        ok = SOURCE.mutate_sku_price(
            payload["store_id"], payload["product_id"], payload["sku_id"],
            float(payload["price"]),
        )
        if not ok:
            raise HTTPException(status_code=404, detail="sku not found")
        return {"version": SOURCE.version}

    @app.post("/admin/mutate/store-name")
    async def mutate_store(payload: dict[str, Any]) -> dict[str, Any]:
        ok = SOURCE.rename_store(payload["store_id"], payload["name"])
        if not ok:
            raise HTTPException(status_code=404, detail="store not found")
        return {"version": SOURCE.version}

    @app.post("/admin/mutate/add-product")
    async def add_product(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            product_id = SOURCE.add_product(
                payload["store_id"], payload.get("title"), payload.get("product_id")
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="store not found") from exc
        return {"product_id": product_id, "version": SOURCE.version}

    @app.get("/admin/requests")
    async def request_log() -> dict[str, Any]:
        return {"requests": SOURCE.request_log()}

    return app


def _from_graphql_error(err: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"message": err.message}
    if err.path is not None:
        out["path"] = list(err.path)
    if err.extensions:
        out["extensions"] = dict(err.extensions)
    return out


def _remember(query: str, variables: dict[str, Any], body: dict[str, Any], scope: str) -> None:
    SOURCE.remember_request(
        {
            "at": now_iso(),
            "operation": op_name(query),
            "snapshot_id": variables.get("snapshotId"),
            "scope": scope,
            "cursor": variables.get("cursor"),
            "errors": len(body.get("errors", [])),
            "has_data": isinstance(body.get("data"), dict),
        }
    )


def _warn_unused() -> None:  # pragma: no cover - documented helper alias
    _ = HTTPException


app = create_source_app()
