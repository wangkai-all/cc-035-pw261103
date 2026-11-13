# Catalog collection system

Three-level catalogue collection (store → product → SKU), driven by a local
GraphQL source, persisted to SQLite, with a React dashboard that shows the
progress as a tree and exports an honest completeness report.

```
backend/    FastAPI + Strawberry GraphQL source + collector + SQLite
frontend/   React + Vite dashboard (collection tree, journal, export)
data/       SQLite database (created on first run)
```

## Run it

Python 3.11+ and Node 20+.

```bash
# backend deps (from the repo root)
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# frontend deps
cd frontend && npm install && cd ..
```

Start the backend. `dev.py` runs both services in one process: the GraphQL
source is mounted at `/source`, the collector API at `/api`. The collector
still talks to the source over real HTTP, so the GraphQL boundary is genuine.

```bash
cd backend
../.venv/bin/uvicorn dev:app --port 8000
```

Or run them separately (source on 8001, collector on 8000):

```bash
cd backend
../.venv/bin/uvicorn source_app:app --port 8001
COLLECTOR_SOURCE_URL=http://127.0.0.1:8001 ../.venv/bin/uvicorn app.main:app --port 8000
```

Then the UI:

```bash
cd frontend
npm run dev          # http://localhost:5173, proxies /api to :8000
```

Environment variables: `COLLECTOR_SOURCE_URL` (default
`http://127.0.0.1:8001`), `COLLECTOR_DB` (default `data/collector.sqlite3`).

## What a batch is

Creating a batch pins one `snapshot_id` — the data version of that collection
run. Every GraphQL page request sends that id, and the source rejects cursors
minted for a different snapshot. Base-data mutations create a *new* version
for new snapshots and are invisible to snapshots already created.

```bash
curl -X POST localhost:8000/api/batches \
  -H 'content-type: application/json' \
  -d '{"source_label":"demo","page_size":2,"auto_run":false}'
# -> {"batch_id": "...", "snapshot_id": "snap-..."}
```

If the snapshot expires mid-run, the batch closes as `expired`. Continuing in
it is refused (`409`) because that would splice two data versions together;
create a successor instead:

```bash
curl -X POST localhost:8000/api/batches/<batch_id>/successor \
  -H 'content-type: application/json' -d '{"auto_run":true}'
```

The successor gets a fresh snapshot; the old batch is marked superseded and
its already-collected rows are kept for audit.

## Collection rules implemented

| Rule | Behaviour |
|---|---|
| `hasNextPage` true | `endCursor` must be present and must advance. |
| Repeated cursor | the branch pauses as `stalled`; its rows are kept. |
| HTTP 200 + `errors` with a path | only the branch named by the path pauses; sibling data from the same response is saved, and the request is journalled `partial`. |
| Path-less / foreign / whole-operation error | the request is not counted complete, its cursor does not advance, and the branch is `failed`. |
| Unresolvable error path | treated as whole-request: we cannot tell what to retry. |
| Retry | resets only the requested branch (and optionally its descendants) and re-runs runnable branches; complete branches are never re-requested. |
| Resume after a crash | in-flight requests are reconciled on restart; the same batch and snapshot continue. |
| De-duplication | keys are scoped by the actual hierarchy (`batch/layer/scope/natural_key`), so the same product or SKU id in two stores never overwrites. |
| Export | `complete` is true only when the batch completed and every branch is complete; otherwise the export lists the unfinished branches and their errors. |

## Triggering the failure modes

The source has an admin API and the UI has a **Source faults** panel for the
same thing.

```bash
# partial error on the first store page: pauses one product branch only
curl -X POST localhost:8000/api/source/rules -H 'content-type: application/json' -d '{
  "mode":"partial_error","scope":"/store","at_offset":0,"times":1,
  "message":"products unavailable for this store"}'

# cursor that cannot advance on one SKU branch
curl -X POST localhost:8000/api/source/rules -H 'content-type: application/json' -d '{
  "mode":"stalled_cursor","scope":"/sku/S-2/P-1","at_offset":0,"times":1}'

# hasNextPage=true with endCursor=null
curl -X POST localhost:8000/api/source/rules -H 'content-type: application/json' -d '{
  "mode":"missing_end_cursor","scope":"/store","at_offset":0,"times":1}'

# whole-request error (data:null + errors)
curl -X POST localhost:8000/api/source/rules -H 'content-type: application/json' -d '{
  "mode":"request_error","scope":"/sku/S-3/P-2","times":1,"pathless":true}'

# expire a snapshot mid-run
curl -X POST localhost:8000/api/source/snapshots/<snapshot_id>/expire

# mutate base data; existing snapshots stay unchanged
curl -X POST localhost:8000/api/source/mutate -H 'content-type: application/json' -d '{
  "action":"sku-price","store_id":"S-1","product_id":"P-1","sku_id":"SKU-1","price":999}'
```

Scopes: `/store`, `/product/<storeId>`, `/sku/<storeId>/<productId>`.
`at_offset` targets a page index; `times` limits how often the rule fires.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | collector + source reachability |
| `POST` | `/api/batches` | create a batch (creates/validates the snapshot) |
| `GET` | `/api/batches` | batch list with counts |
| `GET` | `/api/batches/{id}` | progress tree (`status`, `subtree_status`, rollups, page journal) |
| `POST` | `/api/batches/{id}/run` | collect runnable branches |
| `POST` | `/api/batches/{id}/retry` | retry one branch, optionally run afterwards |
| `POST` | `/api/batches/{id}/resume` | continue the same batch/snapshot after interruption |
| `POST` | `/api/batches/{id}/successor` | close an expired batch and open a new snapshot |
| `GET` | `/api/batches/{id}/export` | data + completeness report |
| `GET` | `/api/batches/{id}/requests` | per-branch request journal |
| `GET`/`POST` | `/api/source/...` | source summary, fault rules, snapshot expiry, base mutations |

In the tree, `status` is the node's own page sequence; `subtree_status`
answers "is everything under this node collected?", so a store whose product
page finished but whose product missed a SKU page is visibly incomplete.

## Tests

```bash
cd backend
../.venv/bin/python -m pytest -q
```

26 tests cover the full walk, snapshot pinning and base-data isolation,
partial/pathless/whole-request errors, stalled and missing cursors,
first-page stall detection, expiry and successor batches, resume with
reconciliation, hierarchy-scoped de-duplication, per-branch journal, and
honest export. `tests/test_api.py` runs a real HTTP source on a free port
and exercises the FastAPI routes end to end.

Frontend checks:

```bash
cd frontend
npm run typecheck
npm run build
```
