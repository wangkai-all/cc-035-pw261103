"""Progress tree and export, built from persisted rows.

Display hierarchy mirrors the real catalogue: directory -> store -> product
-> SKU.  Each level is fed by one paginated branch:

    directory   /stores                            store_page
    store       /stores/{s}/products               product_page
    product     /stores/{s}/products/{p}/skus      sku_page
    sku         (rows, not a branch)               comes from the sku_page

A node's ``status`` is its own page-branch status (``pending`` when the
branch was never created).  Subtree rollups distinguish "store page finished
but one product missed a page" from "store fully collected".

Everything comes from SQLite, so the tree after a crash shows exactly what
was durably collected.
"""

from __future__ import annotations

from typing import Any

from .db import Database

DIRECTORY_PATH = "/stores"


def store_node_path(store_id: str) -> str:
    return f"/stores/{store_id}"


def product_node_path(store_id: str, product_id: str) -> str:
    return f"/stores/{store_id}/products/{product_id}"


def product_branch_path(store_id: str) -> str:
    return f"/stores/{store_id}/products"


def sku_branch_path(store_id: str, product_id: str) -> str:
    return f"/stores/{store_id}/products/{product_id}/skus"


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


class _TreeBuilder:
    def __init__(self, db: Database, batch_id: str):
        self.db = db
        self.batch_id = batch_id
        self.branches = {row["path"]: dict(row) for row in db.branches(batch_id)}
        requests = db.requests(batch_id)
        self.requests: dict[str, list[dict[str, Any]]] = {}
        for req in requests:
            self.requests.setdefault(req["branch_path"], []).append(req)
        self.records = db.records(batch_id)
        self.counts = db.record_counts(batch_id)

    # -- helpers ------------------------------------------------------

    def branch_info(self, path: str) -> dict[str, Any] | None:
        row = self.branches.get(path)
        if row is None:
            return None
        page_size = self.page_size
        total = row["total_count"]
        remaining = None
        if total is not None and total > 0 and page_size:
            expected = (total + page_size - 1) // page_size
            remaining = max(expected - row["pages_done"], 0)
        return {
            "branch_path": path,
            "kind": row["kind"],
            "status": row["status"],
            "pages_done": row["pages_done"],
            "pages_remaining": remaining,
            "total_count": total,
            "has_next": bool(row["has_next"]),
            "cursor": row["cursor"],
            "last_error": row["last_error"],
            "request_count": len(self.requests.get(path, [])),
            "requests": self.requests.get(path, []),
        }

    @property
    def page_size(self) -> int:
        batch = self.db.batch(self.batch_id)
        return int(batch["page_size"]) if batch else 2

    def make_node(
        self, path: str, kind: str, label: str, branch_path: str,
        store_id: str | None, product_id: str | None,
        *,
        present: bool = True,
    ) -> dict[str, Any]:
        info = self.branch_info(branch_path)
        # ``status`` describes this node's own page sequence.  ``subtree_status``
        # (filled in by ``_rollup``) answers the different question "is
        # everything under this node collected?" -- so a store whose products
        # page finished but whose product misses a SKU page is visibly not done.
        status = info["status"] if info else ("complete" if present else "missing")
        node = {
            "path": path,
            "kind": kind,
            "label": label,
            "status": status,
            "subtree_status": status,
            "present": present,
            "store_id": store_id,
            "product_id": product_id,
            # Flattened copies keep the UI simple; ``branch`` keeps the full
            # page journal for the branch inspector.
            "branch_path": info["branch_path"] if info else None,
            "pages_done": info["pages_done"] if info else 0,
            "pages_remaining": info["pages_remaining"] if info else None,
            "total_count": info["total_count"] if info else None,
            "has_next": info["has_next"] if info else False,
            "last_error": info["last_error"] if info else None,
            "request_count": info["request_count"] if info else 0,
            "requests": info["requests"] if info else [],
            "branch": info,
            "children": [],
        }
        return node

    # -- build ---------------------------------------------------------

    def build(self) -> list[dict[str, Any]]:
        root = self.make_node(
            DIRECTORY_PATH, "directory", "Store directory", DIRECTORY_PATH,
            None, None,
        )
        stores: dict[str, dict[str, Any]] = {}
        products: dict[str, dict[str, Any]] = {}

        # Store nodes come from store records.  A store can also appear with
        # no record at all when its product page failed before a single
        # product was written -- that is exactly what "missing data" looks
        # like in the tree.
        for rec in self.records:
            if rec["kind"] == "store":
                sid = rec["store_id"]
                stores[sid] = self.make_node(
                    store_node_path(sid), "store",
                    f"{rec['payload'].get('name') or sid} ({sid})",
                    product_branch_path(sid), sid, None,
                )
                stores[sid]["data"] = rec["payload"]
        for path, row in self.branches.items():
            if row["kind"] == "product_page" and row["store_id"]:
                stores.setdefault(
                    row["store_id"],
                    self.make_node(
                        store_node_path(row["store_id"]), "store", row["label"],
                        path, row["store_id"], None, present=False,
                    ),
                )

        # Product nodes: every product record; a store's product page may
        # fail before all products are known, which leaves partial children.
        for rec in self.records:
            if rec["kind"] != "product":
                continue
            pid = rec["product_id"]
            sid = rec["store_id"]
            key = product_node_path(sid, pid)
            products[key] = self.make_node(
                key, "product",
                f"{rec['payload'].get('title') or pid} ({pid})",
                sku_branch_path(sid, pid), sid, pid, present=True,
            )
            products[key]["data"] = rec["payload"]
        for path, row in self.branches.items():
            if row["kind"] == "sku_page" and row["store_id"] and row["product_id"]:
                products.setdefault(
                    product_node_path(row["store_id"], row["product_id"]),
                    self.make_node(
                        product_node_path(row["store_id"], row["product_id"]),
                        "product", row["label"], path,
                        row["store_id"], row["product_id"], present=False,
                    ),
                )

        # SKU leaves: one per collected SKU, grouped under their product.
        for rec in self.records:
            if rec["kind"] != "sku":
                continue
            key = product_node_path(rec["store_id"], rec["product_id"])
            product = products.get(key)
            if product is None:
                product = self.make_node(
                    key, "product", f"{rec['product_id']}",
                    sku_branch_path(rec["store_id"], rec["product_id"]),
                    rec["store_id"], rec["product_id"], present=False,
                )
                products[key] = product
            sid = rec["payload"].get("id") or rec["sku_id"]
            product["children"].append(
                {
                    "path": rec["key"],
                    "kind": "sku",
                    "label": f"{sid} {rec['payload'].get('code') or ''}".strip(),
                    "status": "complete",
                    "subtree_status": "complete",
                    "present": True,
                    "store_id": rec["store_id"],
                    "product_id": rec["product_id"],
                    "sku_id": rec["sku_id"],
                    "branch_path": None,
                    "pages_done": 1,
                    "pages_remaining": 0,
                    "total_count": 1,
                    "has_next": False,
                    "last_error": None,
                    "request_count": 0,
                    "requests": [],
                    "data": rec["payload"],
                    "branch": None,
                    "children": [],
                }
            )

        for sid, store in stores.items():
            root["children"].append(store)
        for key, product in products.items():
            store = stores.get(product["store_id"])
            if store is not None:
                store["children"].append(product)
            else:
                root["children"].append(product)

        self._sort(root)
        self._rollup(root)
        return [root]

    def _sort(self, node: dict[str, Any]) -> None:
        node["children"].sort(key=lambda n: (n["kind"], n["label"]))
        for child in node["children"]:
            self._sort(child)

    def _rollup(self, node: dict[str, Any]) -> dict[str, int]:
        children = [self._rollup(c) for c in node["children"]]
        roll = {"total": 0, "complete": 0, "incomplete": 0, "failed": 0,
                "missing": 0, "stalled": 0}
        for c in children:
            for key in roll:
                roll[key] += c[key]
        # Present nodes count one; the directory root is structural.
        if node["kind"] != "directory":
            roll["total"] += 1
            if node["status"] == "complete":
                roll["complete"] += 1
            else:
                roll["incomplete"] += 1
                if node["status"] in roll:
                    roll[node["status"]] += 1
        node["rollup"] = roll
        node["subtree_status"] = (
            "complete" if roll["total"] == 0 or roll["incomplete"] == 0
            else "failed" if roll["failed"]
            else "stalled" if roll["stalled"]
            else "partial"
        )
        node["sku_count"] = sum(
            1 for c in node["children"] if c["kind"] == "sku"
        ) + sum(c.get("sku_count", 0) for c in node["children"])
        return roll


def build_tree(db: Database, batch_id: str) -> dict[str, Any]:
    batch = db.batch(batch_id)
    if not batch:
        raise KeyError(batch_id)
    builder = _TreeBuilder(db, batch_id)
    roots = builder.build()
    return {
        "batch": batch_dict(batch),
        "roots": roots,
        "counts": builder.counts,
        "totals": _totals(builder.branches),
        "complete": all(
            row["status"] == "complete" for row in builder.branches.values()
        ),
    }


def _totals(branches: dict[str, dict[str, Any]]) -> dict[str, int]:
    out = {"branches": len(branches), "complete": 0, "unfinished": 0,
           "failed": 0, "stalled": 0, "pending": 0, "partial": 0}
    for row in branches.values():
        if row["status"] == "complete":
            out["complete"] += 1
        else:
            out["unfinished"] += 1
            out[row["status"]] = out.get(row["status"], 0) + 1
    return out


def batch_dict(batch: Any) -> dict[str, Any]:
    return {
        "id": batch["id"],
        "snapshot_id": batch["snapshot_id"],
        "source_label": batch["source_label"],
        "state": batch["state"],
        "note": batch["note"],
        "created_at": batch["created_at"],
        "closed_at": batch["closed_at"],
        "page_size": batch["page_size"],
        "snapshot_expires_at": batch["snapshot_expires_at"],
        "parent_batch_id": batch["parent_batch_id"],
        "superseded_by": batch["superseded_by"],
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_batch(db: Database, batch_id: str) -> dict[str, Any]:
    """Export collected data with an honest completeness report.

    An unfinished batch is exported as ``complete: false`` plus the explicit
    list of branches that were not collected -- it is never presented as a
    finished dataset.
    """
    batch = db.batch(batch_id)
    if not batch:
        raise KeyError(batch_id)

    rows = db.branches(batch_id)
    unfinished = [r for r in rows if r["status"] != "complete"]
    finished = [r for r in rows if r["status"] == "complete"]
    collected = db.records(batch_id)
    stores = [r for r in collected if r["kind"] == "store"]
    products = [r for r in collected if r["kind"] == "product"]
    skus = [r for r in collected if r["kind"] == "sku"]

    complete = batch["state"] == "completed" and not unfinished
    return {
        "batch_id": batch_id,
        "snapshot_id": batch["snapshot_id"],
        "source_label": batch["source_label"],
        "batch_state": batch["state"],
        "complete": complete,
        "warning": None if complete else (
            f"Batch is {batch['state']}; {len(unfinished)} branch(es) were not"
            " fully collected."
        ),
        "snapshot_expires_at": batch["snapshot_expires_at"],
        "branches": {
            "total": len(rows),
            "complete": len(finished),
            "unfinished": len(unfinished),
        },
        "complete_branches": [
            {"path": r["path"], "kind": r["kind"], "label": r["label"],
             "pages_done": r["pages_done"]}
            for r in finished
        ],
        "incomplete_branches": [
            {"path": r["path"], "kind": r["kind"], "label": r["label"],
             "status": r["status"], "pages_done": r["pages_done"],
             "last_error": r["last_error"]}
            for r in unfinished
        ],
        "counts": {"stores": len(stores), "products": len(products), "skus": len(skus)},
        "data": {
            "stores": [{**r["payload"], "key": r["key"]} for r in stores],
            "products": [
                {**r["payload"], "key": r["key"], "store_id": r["store_id"]}
                for r in products
            ],
            "skus": [
                {**r["payload"], "key": r["key"], "store_id": r["store_id"],
                 "product_id": r["product_id"]}
                for r in skus
            ],
        },
    }
