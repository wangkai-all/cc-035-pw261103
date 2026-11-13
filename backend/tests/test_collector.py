"""Integration tests for the collector state machine."""

from __future__ import annotations

import json

import pytest

from app.collector import (
    Collector,
    GraphQLClient,
    product_branch_path,
    sku_branch_path,
)
from app.tree import build_tree, export_batch
from tests.transport import ASGITransport


def make_collector(db, state):
    return Collector(
        db,
        GraphQLClient("http://source/graphql", transport=ASGITransport(_app(state))),
        "http://source",
    )


def _app(state):
    from app.source import create_source_app
    return create_source_app(state)


def test_full_walk_collects_every_level(collector, snapshot):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    report = collector.run(batch_id)

    assert report.state == "completed", report.as_dict()
    records = collector.db.records(batch_id)
    kinds = {}
    for r in records:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    assert kinds == {"store": 4, "product": 12, "sku": 48}
    tree = build_tree(collector.db, batch_id)
    assert tree["batch"]["state"] == "completed"
    assert len(tree["roots"]) == 1
    assert tree["roots"][0]["subtree_status"] == "complete"


def test_every_request_carries_the_batch_snapshot(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=3)
    collector.run(batch_id)
    sent = source_state.request_log()
    assert sent
    assert {entry["snapshot_id"] for entry in sent} == {snapshot.id}


def test_base_data_change_does_not_touch_existing_snapshot(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    collector.run(batch_id, max_requests=2)
    source_state.mutate_sku_price("S-1", "P-1", "SKU-1", 999.0)
    collector.run(batch_id)

    skus = [r for r in collector.db.records(batch_id) if r["kind"] == "sku"]
    s1 = [r for r in skus if r["store_id"] == "S-1" and r["product_id"] == "P-1"]
    assert s1, "S-1/P-1 skus should be collected"
    assert all(r["payload"]["price"] != 999.0 for r in s1)


def test_partial_error_pauses_only_that_branch(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    source_state.add_rule(
        {"mode": "partial_error", "scope": "/store", "at_offset": 0, "times": 1,
         "message": "products unavailable for this store"}
    )
    report = collector.run(batch_id)
    tree = build_tree(collector.db, batch_id)
    failed = [n for n in tree["roots"][0]["children"] if n["status"] == "failed"]

    # The store directory page itself completed; exactly the product branch
    # named by the error path is paused, all other stores progressed.
    root = tree["roots"][0]
    assert root["status"] == "complete"
    assert len(failed) == 1, "exactly one child branch should be paused"
    assert report.state in ("paused", "completed")

    retried = collector.retry_branch(batch_id, failed[0]["path"], include_descendants=True)
    assert retried >= 1
    report = collector.run(batch_id)
    assert report.state == "completed"
    records = collector.db.records(batch_id)
    assert len([r for r in records if r["kind"] == "sku"]) == 48


def test_pathless_error_marks_request_not_complete(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    source_state.add_rule(
        {"mode": "request_error", "scope": "/store", "at_offset": 0, "times": 1,
         "pathless": True}
    )
    report = collector.run(batch_id, max_requests=1)
    assert report.last_request["outcome"] == "failed"
    requests = collector.db.requests(batch_id)
    assert requests[-1]["outcome"] == "failed"
    assert requests[-1]["http_status"] == 200
    assert requests[-1]["error_paths"] == []
    branch = collector.db.branch(batch_id, "/stores")
    assert branch["status"] == "failed"
    assert branch["pages_done"] == 0
    assert branch["cursor"] is None


def test_whole_request_error_is_retried_and_recovers(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    source_state.add_rule(
        {"mode": "request_error", "scope": "/store", "at_offset": 0, "times": 1}
    )
    collector.run(batch_id, max_requests=1)
    assert collector.db.branch(batch_id, "/stores")["status"] == "failed"
    collector.retry_all_paused(batch_id)
    report = collector.run(batch_id)
    assert report.state == "completed"
    assert len([r for r in collector.db.records(batch_id) if r["kind"] == "store"]) == 4


def test_stalled_cursor_pauses_branch_and_keeps_rows(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    # The first page comes back with a cursor that cannot advance; the
    # collector must pause this branch instead of re-reading the page.
    source_state.add_rule(
        {"mode": "stalled_cursor", "scope": "/sku/S-1/P-2", "at_offset": 0,
         "times": 1, "message": "cursor did not advance"}
    )
    report = collector.run(batch_id)
    tree = build_tree(collector.db, batch_id)
    stalled = [n for n in tree["roots"][0]["children"][0]["children"]
               if n["status"] == "stalled"]
    assert len(stalled) == 1
    assert "did not advance" in stalled[0]["last_error"]
    assert stalled[0]["pages_done"] == 0
    # Other branches kept their results.
    assert tree["roots"][0]["children"][0]["status"] == "complete"

    collector.retry_branch(batch_id, stalled[0]["path"])
    collector.run(batch_id)
    skus = [r for r in collector.db.records(batch_id)
            if r["kind"] == "sku" and r["product_id"] == "P-2" and r["store_id"] == "S-1"]
    assert len(skus) == 4


def test_has_next_without_end_cursor_pauses_branch(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    source_state.add_rule(
        {"mode": "missing_end_cursor", "scope": "/store", "at_offset": 0, "times": 1}
    )
    report = collector.run(batch_id)
    assert report.state == "paused"
    root = collector.db.branch(batch_id, "/stores")
    assert root["status"] == "stalled"
    assert "endCursor is null" in root["last_error"]


def test_snapshot_expiry_ends_batch_and_successor_uses_new_snapshot(
    collector, snapshot, source_state
):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=1)
    collector.run(batch_id, max_requests=1)
    source_state.expire_snapshot(snapshot.id)
    report = collector.run(batch_id)
    assert report.state == "expired"

    # An expired batch cannot be restarted; the run reports the terminal state.
    again = collector.run(batch_id)
    assert again.state == "expired"
    assert again.requests == 0

    new_snapshot = source_state.create_snapshot()
    new_batch = collector.create_batch(
        new_snapshot.id, "src", page_size=2, parent_batch_id=batch_id
    )
    collector.db.mark_superseded(batch_id, new_batch)
    report = collector.run(new_batch)
    assert report.state == "completed"
    assert collector.db.batch(new_batch)["snapshot_id"] != snapshot.id


def test_resume_after_interruption_reuses_batch_and_cursor(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    collector.run(batch_id, max_requests=2)
    cursor_before = collector.db.branch(batch_id, "/stores")["cursor"]

    # Simulate a crash: a request row stays in_flight, the process stops.
    request_id = collector.db.start_request(batch_id, "/stores", "store_page", 9, cursor_before)
    assert request_id
    batch_before = collector.db.batch(batch_id)
    assert batch_before["state"] == "active"

    # A fresh collector instance (new process) resumes the same batch.
    resumed = make_collector(collector.db, source_state)
    report = resumed.run(batch_id)
    assert report.state == "completed"
    assert collector.db.batch(batch_id)["id"] == batch_id
    assert collector.db.batch(batch_id)["snapshot_id"] == snapshot.id
    interrupted = [r for r in collector.db.requests(batch_id) if r["outcome"] == "interrupted"]
    assert len(interrupted) == 1
    # No duplicate records from re-walking the resumed page.
    assert len([r for r in collector.db.records(batch_id) if r["kind"] == "store"]) == 4


def test_dedupe_keys_are_scoped_by_hierarchy(collector, db, source_state):
    snap = source_state.create_snapshot()
    batch_id = collector.create_batch(snap.id, "src", page_size=4)
    collector.run(batch_id)
    rows = db.query(
        "SELECT layer, scope_path, natural_key FROM layer_keys WHERE batch_id=?",
        (batch_id,),
    )
    keys = {(r["layer"], r["scope_path"], r["natural_key"]) for r in rows}
    assert ("product", "/stores/S-1", "P-1") in keys
    assert ("product", "/stores/S-2", "P-1") in keys
    assert ("sku", "/stores/S-2/products/P-2", "SKU-1") in keys
    assert ("sku", "/stores/S-3/products/P-2", "SKU-1") in keys
    # The natural triple truly distinguishes the same bare ids in two stores.
    assert len(keys) == len(rows)


def test_export_refuses_to_pretend_complete(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    source_state.add_rule({"mode": "request_error", "scope": "/sku/S-2/P-1", "times": 999})
    report = collector.run(batch_id)
    assert report.state == "paused"
    exported = export_batch(collector.db, batch_id)
    assert exported["complete"] is False
    assert exported["warning"]
    assert exported["branches"]["unfinished"] >= 1
    assert any(
        b["path"] == sku_branch_path("S-2", "P-1")
        for b in exported["incomplete_branches"]
    )
    # Data collected elsewhere is still present.
    assert exported["counts"]["skus"] > 0


def test_completed_export_is_marked_complete(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    collector.run(batch_id)
    exported = export_batch(collector.db, batch_id)
    assert exported["complete"] is True
    assert exported["warning"] is None
    assert exported["branches"]["unfinished"] == 0
    assert exported["counts"] == {"stores": 4, "products": 12, "skus": 48}


def test_tree_exposes_request_journal_per_branch(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    source_state.add_rule(
        {"mode": "request_error", "scope": "/product/S-3", "times": 1, "pathless": True}
    )
    collector.run(batch_id, max_requests=50)
    paused = build_tree(collector.db, batch_id)
    store3 = next(c for c in paused["roots"][0]["children"] if c["store_id"] == "S-3")
    # The store's own status and its subtree status are separate: its product
    # page failed, and its subtree shows the unfinished work below.
    assert store3["status"] == "failed"
    assert store3["subtree_status"] != "complete"

    collector.retry_all_paused(batch_id)
    collector.run(batch_id)
    tree = build_tree(collector.db, batch_id)
    store3 = next(c for c in tree["roots"][0]["children"] if c["store_id"] == "S-3")
    assert store3["request_count"] >= 2
    failed_reqs = [r for r in store3["requests"] if r["outcome"] == "failed"]
    assert failed_reqs
    assert "Injected source fault" in failed_reqs[0]["error_summary"]
    assert store3["status"] == "complete"
    assert store3["subtree_status"] == "complete"


def test_partial_error_on_sku_page_keeps_page_rows_but_not_complete(
    collector, snapshot, source_state
):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    source_state.add_rule(
        {"mode": "partial_error", "scope": "/sku/S-1/P-1", "at_offset": 0, "times": 1}
    )
    collector.run(batch_id)
    branch = collector.db.branch(batch_id, sku_branch_path("S-1", "P-1"))
    assert branch["status"] == "failed"
    # Rows that did arrive are kept, de-duplicated on retry.
    kept = [
        r for r in collector.db.records(batch_id)
        if r["kind"] == "sku" and r["store_id"] == "S-1" and r["product_id"] == "P-1"
    ]
    assert kept, "the page's rows must still be saved"
    # The branch did not advance and this page is not counted as done, so
    # the retry re-reads it.
    assert branch["cursor"] is None
    assert branch["pages_done"] == 0

    collector.retry_branch(batch_id, branch["path"])
    collector.run(batch_id)
    branch = collector.db.branch(batch_id, branch["path"])
    assert branch["status"] == "complete"
    # 4 SKUs at page size 2: two real pages; the failed attempt is not one.
    assert branch["pages_done"] == 2
    all_skus = [
        r for r in collector.db.records(batch_id)
        if r["kind"] == "sku" and r["store_id"] == "S-1" and r["product_id"] == "P-1"
    ]
    assert len(all_skus) == 4, "retry must not duplicate rows"


def test_completed_branches_are_not_re_requested(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    collector.run(batch_id)
    before = len(collector.db.requests(batch_id))
    collector.run(batch_id)
    after = len(collector.db.requests(batch_id))
    assert after == before
    assert collector.db.batch(batch_id)["state"] == "completed"


def test_cursor_repeated_from_an_older_page_is_caught(collector, snapshot, source_state):
    batch_id = collector.create_batch(snapshot.id, "src", page_size=1)
    source_state.add_rule(
        {"mode": "stalled_cursor", "scope": "/store", "at_offset": 1, "times": 1}
    )
    report = collector.run(batch_id)
    assert report.state == "paused"
    root = collector.db.branch(batch_id, "/stores")
    assert root["status"] == "stalled"
    assert root["pages_done"] == 1
    assert "already used" in root["last_error"] or "did not advance" in root["last_error"]


def test_page_fault_scope_does_not_poison_snapshot_info(
    collector, snapshot, source_state
):
    """A page-scoped fault rule must not fire on a snapshotInfo metadata probe.

    The probe is not a page request; if the rule leaked into it, the error
    would make the snapshot look unusable and batch creation would fail.
    """
    source_state.add_rule(
        {"mode": "partial_error", "scope": "/store", "at_offset": 0, "times": 1,
         "message": "products unavailable"}
    )
    info = collector.fetch_snapshot_info(snapshot.id)
    assert info is not None and info["id"] == snapshot.id
    assert source_state.rules()[0].fired == 0
    assert all(entry["scope"] == "/snapshot" for entry in source_state.request_log())


def test_first_page_stalled_cursor_pauses_branch(collector, snapshot, source_state):
    """A stall declared on page 0 has no previous cursor to compare against.

    The explicit CURSOR_STALLED code must pause the branch instead of the
    collector silently re-reading the same page and calling it complete.
    """
    batch_id = collector.create_batch(snapshot.id, "src", page_size=2)
    source_state.add_rule(
        {"mode": "stalled_cursor", "scope": "/sku/S-2/P-1", "at_offset": 0,
         "times": 1, "message": "cursor did not advance"}
    )
    report = collector.run(batch_id)
    branch = collector.db.branch(
        batch_id, sku_branch_path("S-2", "P-1")
    )
    assert branch["status"] == "stalled"
    assert branch["pages_done"] == 0
    assert "did not advance" in branch["last_error"]
    assert report.state == "paused"

    # Rows from the refused page are kept and no duplicates appear on retry.
    skus = [r for r in collector.db.records(batch_id)
            if r["kind"] == "sku" and r["store_id"] == "S-2" and r["product_id"] == "P-1"]
    assert len(skus) == 2
    collector.retry_branch(batch_id, sku_branch_path("S-2", "P-1"))
    report = collector.run(batch_id)
    assert report.state == "completed"
    skus = [r for r in collector.db.records(batch_id)
            if r["kind"] == "sku" and r["store_id"] == "S-2" and r["product_id"] == "P-1"]
    assert len(skus) == 4
    assert export_batch(collector.db, batch_id)["complete"] is True
