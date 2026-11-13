"""SQLite persistence for the collection service.

One database file keeps the batch header, the branch tree, the collected
records and the per-request journal together, so a batch resumes after a
restart and every failed request stays explainable.

The interesting state machine lives in ``collector.py``; this module only
stores and reads rows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

BRANCH_STATUSES = ("pending", "partial", "complete", "failed", "stalled")
BATCH_STATES = ("active", "paused", "completed", "expired", "aborted")
REQUEST_OUTCOMES = (
    "in_flight",
    "complete",
    "partial",
    "failed",
    "cursor_stalled",
    "expired",
    "interrupted",
)
REQUEST_KINDS = ("store_page", "product_page", "sku_page", "snapshot_info")

SCHEMA = f"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS batches (
    id                   TEXT PRIMARY KEY,
    snapshot_id          TEXT NOT NULL,
    source_label         TEXT NOT NULL,
    snapshot_expires_at  TEXT,
    page_size            INTEGER NOT NULL DEFAULT 2,
    parent_batch_id      TEXT,
    superseded_by        TEXT,
    state                TEXT NOT NULL CHECK (state IN ({",".join(repr(s) for s in BATCH_STATES)})),
    note                 TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    closed_at            TEXT
);

CREATE TABLE IF NOT EXISTS branches (
    batch_id      TEXT NOT NULL,
    path          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('store_page','product_page','sku_page')),
    parent_path   TEXT,
    store_id      TEXT,
    product_id    TEXT,
    depth         INTEGER NOT NULL,
    label         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ({",".join(repr(s) for s in BRANCH_STATUSES)})),
    cursor        TEXT,
    has_next      INTEGER NOT NULL DEFAULT 0,
    pages_done    INTEGER NOT NULL DEFAULT 0,
    total_count   INTEGER,
    last_error    TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (batch_id, path),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_branches_batch_status
    ON branches (batch_id, status);

CREATE TABLE IF NOT EXISTS records (
    batch_id         TEXT NOT NULL,
    path             TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('store','product','sku')),
    store_id         TEXT NOT NULL,
    product_id       TEXT,
    sku_id           TEXT,
    payload          TEXT NOT NULL,
    collected_at     TEXT NOT NULL,
    PRIMARY KEY (batch_id, path),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

-- The de-duplication guarantee.  A store code is unique alone, a product
-- code is unique inside its store, a SKU code is unique inside its product.
-- Two stores may both carry "P-1" without colliding.
CREATE TABLE IF NOT EXISTS layer_keys (
    batch_id     TEXT NOT NULL,
    layer        TEXT NOT NULL CHECK (layer IN ('store','product','sku')),
    scope_path   TEXT NOT NULL,
    natural_key  TEXT NOT NULL,
    path         TEXT NOT NULL,
    PRIMARY KEY (batch_id, layer, scope_path, natural_key),
    UNIQUE (batch_id, path)
);

CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          TEXT NOT NULL,
    branch_path       TEXT NOT NULL,
    request_kind      TEXT NOT NULL,
    attempt           INTEGER NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    outcome           TEXT NOT NULL CHECK (outcome IN ({",".join(repr(o) for o in REQUEST_OUTCOMES)})),
    http_status       INTEGER,
    requested_cursor  TEXT,
    next_cursor       TEXT,
    has_next_page     INTEGER,
    rows_added        INTEGER NOT NULL DEFAULT 0,
    error_summary     TEXT,
    error_paths       TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_requests_batch ON requests (batch_id, id);
CREATE INDEX IF NOT EXISTS idx_requests_branch ON requests (batch_id, branch_path, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Synchronous SQLite wrapper guarded by one lock.

    The workload is small and a single connection keeps FastAPI handlers
    thread-safe without a pool.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ----- batches -----------------------------------------------------

    def create_batch(
        self, batch_id: str, snapshot_id: str, source_label: str,
        page_size: int, snapshot_expires_at: str | None, parent_batch_id: str | None = None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO batches (id, snapshot_id, source_label, snapshot_expires_at,"
                " page_size, parent_batch_id, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                (batch_id, snapshot_id, source_label, snapshot_expires_at,
                 page_size, parent_batch_id, utcnow()),
            )

    def set_batch_state(self, batch_id: str, state: str, note: str = "") -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE batches SET state=?, note=?, closed_at=? WHERE id=?",
                (state, note, utcnow(), batch_id),
            )

    def reopen_batch(self, batch_id: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE batches SET state='active', note='', closed_at=NULL WHERE id=?",
                (batch_id,),
            )

    def mark_superseded(self, batch_id: str, new_batch_id: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE batches SET superseded_by=? WHERE id = ?",
                (new_batch_id, batch_id),
            )

    def batch(self, batch_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM batches WHERE id=?", (batch_id,))

    def batches(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM batches ORDER BY created_at DESC, id DESC")

    def active_batch(self, source_label: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM batches WHERE source_label=? AND state='active'"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (source_label,),
        )

    # ----- branches ----------------------------------------------------

    def upsert_branch(
        self, batch_id: str, path: str, kind: str, parent_path: str | None,
        store_id: str | None, product_id: str | None, depth: int, label: str,
        status: str = "pending",
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO branches
                    (batch_id, path, kind, parent_path, store_id, product_id, depth,
                     label, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id, path) DO UPDATE SET
                    label=excluded.label, updated_at=excluded.updated_at
                """,
                (batch_id, path, kind, parent_path, store_id, product_id,
                 depth, label, status, utcnow()),
            )

    def update_branch(
        self, batch_id: str, path: str, *,
        status: str | None = None,
        cursor: str | None = None, set_cursor: bool = False,
        has_next: bool | None = None,
        pages_delta: int = 0,
        total_count: int | None = None,
        last_error: str | None = None, clear_error: bool = False,
    ) -> None:
        sets: list[str] = ["updated_at=?"]
        params: list[Any] = [utcnow()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if set_cursor:
            sets.append("cursor=?")
            params.append(cursor)
        if has_next is not None:
            sets.append("has_next=?")
            params.append(1 if has_next else 0)
        if pages_delta:
            sets.append("pages_done=pages_done+?")
            params.append(pages_delta)
        if total_count is not None:
            sets.append("total_count=?")
            params.append(total_count)
        if clear_error:
            sets.append("last_error=NULL")
        elif last_error is not None:
            sets.append("last_error=?")
            params.append(last_error)
        params.extend([batch_id, path])
        with self.tx() as conn:
            conn.execute(
                f"UPDATE branches SET {', '.join(sets)} WHERE batch_id=? AND path=?",
                params,
            )

    def branches(self, batch_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM branches WHERE batch_id=? ORDER BY depth, path", (batch_id,)
        )

    def branch(self, batch_id: str, path: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM branches WHERE batch_id=? AND path=?", (batch_id, path)
        )

    def branches_under(self, batch_id: str, prefix: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM branches WHERE batch_id=? AND path LIKE ?"
            " ORDER BY depth, path",
            (batch_id, prefix + "%"),
        )

    def runnable_branches(self, batch_id: str) -> list[sqlite3.Row]:
        """Branches the automatic runner may touch.

        ``failed`` and ``stalled`` branches are paused: the user must retry
        them explicitly.  A ``partial`` branch with a clean page cursor is
        resumed automatically.
        """
        return self.query(
            "SELECT * FROM branches WHERE batch_id=? AND"
            " (status='pending' OR (status='partial' AND last_error IS NULL))"
            " ORDER BY depth, path",
            (batch_id,),
        )

    def unfinished_branches(self, batch_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM branches WHERE batch_id=? AND status!='complete' ORDER BY path",
            (batch_id,),
        )

    def reset_branches_for_retry(
        self, batch_id: str, path: str, include_descendants: bool,
        *, clear_cursor: bool = False,
    ) -> int:
        """Reset a paused branch (optionally its subtree) to pending.

        Records are untouched, so retrying one SKU branch keeps all other
        branches' results.  ``clear_cursor`` restarts a branch from page one
        (the manual escape hatch when the source cursor itself is unusable).
        """
        with self.tx() as conn:
            if include_descendants:
                rows = conn.execute(
                    "SELECT path FROM branches WHERE batch_id=? AND path LIKE ?",
                    (batch_id, path + "%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT path FROM branches WHERE batch_id=? AND path=?",
                    (batch_id, path),
                ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE branches SET"
                    " status=CASE WHEN status='complete' THEN 'complete' ELSE 'pending' END,"
                    " last_error=NULL, updated_at=?,"
                    " cursor=CASE WHEN ? THEN NULL ELSE cursor END,"
                    " pages_done=CASE WHEN ? THEN 0 ELSE pages_done END,"
                    " has_next=CASE WHEN ? THEN 0 ELSE has_next END"
                    " WHERE batch_id=? AND path=?",
                    (utcnow(), int(clear_cursor), int(clear_cursor),
                     int(clear_cursor), batch_id, row["path"]),
                )
            return len(rows)

    # ----- records -----------------------------------------------------

    def add_record(
        self, batch_id: str, path: str, kind: str, store_id: str,
        product_id: str | None, sku_id: str | None, payload: dict[str, Any],
    ) -> bool:
        """Insert a record.  False means the natural key already exists."""
        if kind == "store":
            scope, natural = "", store_id
        elif kind == "product":
            scope, natural = f"/stores/{store_id}", product_id or ""
        else:
            scope, natural = f"/stores/{store_id}/products/{product_id}", sku_id or ""
        with self.tx() as conn:
            try:
                conn.execute(
                    "INSERT INTO layer_keys (batch_id, layer, scope_path, natural_key, path)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (batch_id, kind, scope, natural, path),
                )
            except sqlite3.IntegrityError:
                return False
            conn.execute(
                "INSERT INTO records"
                " (batch_id, path, kind, store_id, product_id, sku_id, payload, collected_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, path, kind, store_id, product_id, sku_id,
                 json.dumps(payload, ensure_ascii=False), utcnow()),
            )
        return True

    def records(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM records WHERE batch_id=? ORDER BY path", (batch_id,)
        )
        return [
            {
                "key": row["path"],
                "kind": row["kind"],
                "store_id": row["store_id"],
                "product_id": row["product_id"],
                "sku_id": row["sku_id"],
                "payload": json.loads(row["payload"]),
                "collected_at": row["collected_at"],
            }
            for row in rows
        ]

    def record_counts(self, batch_id: str) -> dict[str, int]:
        rows = self.query(
            "SELECT kind, COUNT(*) AS n FROM records WHERE batch_id=? GROUP BY kind",
            (batch_id,),
        )
        return {row["kind"]: row["n"] for row in rows}

    # ----- requests ----------------------------------------------------

    def start_request(
        self, batch_id: str, branch_path: str, request_kind: str,
        attempt: int, requested_cursor: str | None,
    ) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO requests (batch_id, branch_path, request_kind, attempt,"
                " started_at, outcome, requested_cursor)"
                " VALUES (?, ?, ?, ?, ?, 'in_flight', ?)",
                (batch_id, branch_path, request_kind, attempt, utcnow(), requested_cursor),
            )
            return int(cur.lastrowid)

    def finish_request(
        self, request_id: int, outcome: str, *,
        http_status: int | None = None,
        requested_cursor: str | None = None,
        next_cursor: str | None = None,
        has_next_page: bool | None = None,
        rows_added: int = 0,
        error_summary: str | None = None,
        error_paths: list[list[Any]] | None = None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE requests SET finished_at=?, outcome=?, http_status=?,"
                " requested_cursor=?, next_cursor=?, has_next_page=?, rows_added=?,"
                " error_summary=?, error_paths=?"
                " WHERE id=?",
                (
                    utcnow(), outcome, http_status, requested_cursor, next_cursor,
                    None if has_next_page is None else (1 if has_next_page else 0),
                    rows_added, error_summary, json.dumps(error_paths or []), request_id,
                ),
            )

    def requests(self, batch_id: str, branch_path: str | None = None) -> list[dict[str, Any]]:
        if branch_path is None:
            rows = self.query(
                "SELECT * FROM requests WHERE batch_id=? ORDER BY id", (batch_id,)
            )
        else:
            rows = self.query(
                "SELECT * FROM requests WHERE batch_id=? AND branch_path=? ORDER BY id",
                (batch_id, branch_path),
            )
        out = []
        for row in rows:
            item = dict(row)
            item["error_paths"] = json.loads(item.get("error_paths") or "[]")
            out.append(item)
        return out

    def attempts(self, batch_id: str, branch_path: str) -> int:
        row = self.one(
            "SELECT COUNT(*) AS n FROM requests WHERE batch_id=? AND branch_path=?",
            (batch_id, branch_path),
        )
        return int(row["n"]) if row else 0

    def visited_cursors(self, batch_id: str, branch_path: str) -> set[str]:
        """Cursors already used or produced on this branch."""
        rows = self.query(
            "SELECT requested_cursor, next_cursor FROM requests"
            " WHERE batch_id=? AND branch_path=? AND outcome IN"
            " ('complete','partial','cursor_stalled')",
            (batch_id, branch_path),
        )
        seen: set[str] = set()
        for row in rows:
            if row["requested_cursor"]:
                seen.add(row["requested_cursor"])
            if row["next_cursor"]:
                seen.add(row["next_cursor"])
        return seen

    def reconcile_in_flight(self, batch_id: str) -> int:
        """Mark requests left in flight by a crash as interrupted."""
        rows = self.query(
            "SELECT id, branch_path FROM requests WHERE batch_id=? AND outcome='in_flight'",
            (batch_id,),
        )
        with self.tx() as conn:
            for row in rows:
                conn.execute(
                    "UPDATE requests SET finished_at=?, outcome='interrupted',"
                    " error_summary='Collector stopped before the response was recorded'"
                    " WHERE id=?",
                    (utcnow(), row["id"]),
                )
                conn.execute(
                    "UPDATE branches SET last_error=?, updated_at=? WHERE batch_id=? AND path=?"
                    " AND status IN ('pending','partial')",
                    ("Interrupted mid-request; resume to retry this page",
                     utcnow(), batch_id, row["branch_path"]),
                )
        return len(rows)
