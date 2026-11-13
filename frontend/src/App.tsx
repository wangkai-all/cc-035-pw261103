import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { StatTile, StatusChip } from "./components";
import { TreeNodeView } from "./components/Tree";
import type {
  BatchSummary,
  ExportResponse,
  RunReport,
  SourceSummary,
  TreeResponse,
  TreeNode,
} from "./types";
import "./styles.css";

type Mode = "idle" | "running" | "error";

const SCOPE_PRESETS = [
  { label: "Store directory (all stores)", value: "/store" },
  { label: "Products of a store", value: "/product/S-1" },
  { label: "SKUs of a product", value: "/sku/S-1/P-2" },
];

export default function App() {
  const [mode, setMode] = useState<Mode>("idle");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tree, setTree] = useState<TreeResponse | null>(null);
  const [source, setSource] = useState<SourceSummary | null>(null);
  const [exported, setExported] = useState<ExportResponse | null>(null);
  const [busyBranch, setBusyBranch] = useState<string | null>(null);
  const [runReport, setRunReport] = useState<RunReport | null>(null);
  const [pageSize, setPageSize] = useState(2);
  const [autoRun, setAutoRun] = useState(true);
  const [faultMode, setFaultMode] = useState("partial_error");
  const [faultScope, setFaultScope] = useState("/store");
  const [faultOffset, setFaultOffset] = useState<string>("0");
  const [faultTimes, setFaultTimes] = useState<string>("1");
  const [faultPathless, setFaultPathless] = useState(false);
  const selectedRef = useRef<string | null>(null);

  selectedRef.current = selected;

  const refreshBatches = useCallback(async () => {
    const res = await api.listBatches();
    setBatches(res.batches);
    return res.batches;
  }, []);

  const loadTree = useCallback(async (batchId: string) => {
    const res = await api.getBatch(batchId);
    setTree(res);
    return res;
  }, []);

  const loadSource = useCallback(async () => {
    try {
      setSource(await api.source());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const refreshAll = useCallback(
    async (batchId?: string | null) => {
      await Promise.all([refreshBatches(), loadSource()]);
      const target = batchId ?? selectedRef.current;
      if (target) {
        await loadTree(target);
      }
    },
    [refreshBatches, loadSource, loadTree],
  );

  useEffect(() => {
    (async () => {
      try {
        const list = await refreshBatches();
        await loadSource();
        if (list.length > 0) {
          setSelected(list[0].id);
          await loadTree(list[0].id);
        }
      } catch (e) {
        setError(String(e));
      }
    })();
  }, [refreshBatches, loadSource, loadTree]);

  const selectBatch = useCallback(
    async (id: string) => {
      setSelected(id);
      setExported(null);
      setRunReport(null);
      try {
        await loadTree(id);
      } catch (e) {
        setError(String(e));
      }
    },
    [loadTree],
  );

  const guard = useCallback(
    async (fn: () => Promise<void>) => {
      setMode("running");
      setError(null);
      try {
        await fn();
      } catch (e) {
        setError(String(e));
      } finally {
        setMode("idle");
        setBusyBranch(null);
      }
    },
    [],
  );

  const startBatch = useCallback(
    () =>
      guard(async () => {
        const res = await api.createBatch({
          source_label: "demo-source",
          page_size: pageSize,
          auto_run: autoRun,
        });
        setSelected(res.batch_id);
        setExported(null);
        setRunReport(res.run ?? null);
        await refreshAll(res.batch_id);
        setNotice(
          res.run
            ? `Batch ${res.batch_id} finished with state "${res.run.state}" (${res.run.requests} requests).`
            : `Batch ${res.batch_id} created with snapshot ${res.snapshot_id}.`,
        );
      }),
    [guard, pageSize, autoRun, refreshAll],
  );

  const runBatch = useCallback(
    (resume: boolean) =>
      guard(async () => {
        if (!selected) return;
        const report = resume
          ? await api.resumeBatch(selected)
          : await api.runBatch(selected);
        setRunReport(report);
        await refreshAll(selected);
        setNotice(
          `Run finished: ${report.state} (${report.requests} request(s), ${report.stopped_because}).`,
        );
      }),
    [guard, selected, refreshAll],
  );

  const retryBranch = useCallback(
    (node: TreeNode) =>
      guard(async () => {
        if (!selected || !node.branch_path) return;
        setBusyBranch(node.branch_path);
        const res = await api.retryBranch(selected, {
          branch_path: node.branch_path,
          include_descendants: false,
          run_after: true,
        });
        setRunReport(res.run ?? null);
        await refreshAll(selected);
        setNotice(
          `Retried ${node.branch_path} (${res.reset_branches} branch reset). Other results kept.`,
        );
      }),
    [guard, selected, refreshAll],
  );

  const expireSnapshot = useCallback(
    () =>
      guard(async () => {
        if (!tree) return;
        await api.expireSnapshot(tree.batch.snapshot_id);
        const report = await api.runBatch(tree.batch.id, { max_requests: 1 }).catch(() => null);
        if (report) setRunReport(report);
        await refreshAll(tree.batch.id);
        setNotice(
          "Snapshot expired at the source. The batch is now closed; open a successor to continue on a new snapshot.",
        );
      }),
    [guard, tree, refreshAll],
  );

  const openSuccessor = useCallback(
    () =>
      guard(async () => {
        if (!selected) return;
        const res = await api.successor(selected, { auto_run: true });
        setSelected(res.batch_id);
        setExported(null);
        await refreshAll(res.batch_id);
        setNotice(
          `New batch ${res.batch_id} on snapshot ${res.snapshot_id}; the old batch keeps its partial data.`,
        );
      }),
    [guard, selected, refreshAll],
  );

  const exportBatch = useCallback(
    () =>
      guard(async () => {
        if (!selected) return;
        const res = await api.exportBatch(selected);
        setExported(res);
      }),
    [guard, selected],
  );

  const addFault = useCallback(
    () =>
      guard(async () => {
        await api.addRule({
          mode: faultMode,
          scope: faultScope,
          at_offset: faultOffset === "" ? null : Number(faultOffset),
          times: faultTimes === "" ? null : Number(faultTimes),
          pathless: faultPathless,
          message: `Injected ${faultMode}`,
        });
        await loadSource();
        setNotice("Fault rule installed at the source.");
      }),
    [guard, faultMode, faultScope, faultOffset, faultTimes, faultPathless, loadSource],
  );

  const clearFaults = useCallback(
    () =>
      guard(async () => {
        await api.clearRules();
        await loadSource();
        setNotice("All fault rules cleared.");
      }),
    [guard, loadSource],
  );

  const mutateData = useCallback(
    () =>
      guard(async () => {
        const res = await api.mutate({
          action: "sku-price",
          store_id: "S-1",
          product_id: "P-1",
          sku_id: "SKU-1",
          price: 999,
        });
        await loadSource();
        setNotice(
          `Base data changed (now version ${res.version}). Existing snapshots are unaffected.`,
        );
      }),
    [guard, loadSource],
  );

  const totals = tree?.totals;
  const branchPct = totals && totals.branches > 0 ? totals.complete / totals.branches : 0;
  const counts = tree?.counts ?? {};
  const root = tree?.roots[0];

  const facts = useMemo(() => {
    if (!tree) return null;
    const skuNodes = countKind(root, "sku");
    return {
      stores: counts.store ?? 0,
      products: counts.product ?? 0,
      skus: counts.sku ?? 0,
      skuNodes,
    };
  }, [tree, counts, root]);

  return (
    <div className="app">
      <div className="col-main">
        <div className="topbar">
          <div className="title">
            <h1>Catalog collection</h1>
            <span>
              store → product → SKU · snapshot-pinned batches
              {tree ? ` · snapshot ${tree.batch.snapshot_id}` : ""}
            </span>
          </div>
          <div className="toolbar">
            <button
              type="button"
              className="primary"
              onClick={startBatch}
              disabled={mode === "running"}
            >
              New batch
            </button>
            <button
              type="button"
              onClick={() => runBatch(false)}
              disabled={!selected || mode === "running"}
            >
              Run next page
            </button>
            <button
              type="button"
              onClick={() => runBatch(true)}
              disabled={!selected || mode === "running"}
            >
              Resume
            </button>
            <button
              type="button"
              onClick={exportBatch}
              disabled={!selected || mode === "running"}
            >
              Export
            </button>
          </div>
        </div>

        {mode === "running" ? <div className="notice">Working…</div> : null}
        {error ? (
          <div className="notice error" role="alert">
            {error}
          </div>
        ) : null}
        {notice ? (
          <div className="notice" role="status">
            {notice}
          </div>
        ) : null}
        {tree && tree.batch.state === "expired" ? (
          <div className="notice warning">
            This batch's snapshot expired. No further pages are requested on it;
            create a successor batch to finish the catalogue on a new snapshot.
            <div style={{ marginTop: 8 }}>
              <button type="button" onClick={openSuccessor}>
                Open successor batch
              </button>
            </div>
          </div>
        ) : null}

        {tree ? (
          <>
            <div className="kpi-row">
              <StatTile
                label="Stores"
                value={facts?.stores ?? 0}
                sub={`${countKind(root, "store")} store branches`}
              />
              <StatTile
                label="Products"
                value={facts?.products ?? 0}
                sub={`${countKind(root, "product")} product branches`}
              />
              <StatTile
                label="SKUs"
                value={facts?.skus ?? 0}
                sub={`${facts?.skuNodes ?? 0} SKU leaves`}
              />
              <StatTile
                label="Branches collected"
                value={`${totals?.complete ?? 0}/${totals?.branches ?? 0}`}
                sub={`${Math.round(branchPct * 100)}% · ${totals?.failed ?? 0} failed, ${totals?.stalled ?? 0} stalled`}
                meter={{
                  value: totals?.complete ?? 0,
                  max: totals?.branches ?? 0,
                  caption: `${totals?.unfinished ?? 0} branch(es) unfinished`,
                }}
              />
            </div>

            <div className="card">
              <div
                className="toolbar"
                style={{ justifyContent: "space-between", marginBottom: 8 }}
              >
                <div>
                  <h2>Progress tree</h2>
                  <p className="hint">
                    Expand a store or product to see which page is missing. Use
                    “log” for the request journal and “retry” to repair one branch.
                  </p>
                </div>
                <div className="toolbar">
                  <button type="button" onClick={expireSnapshot} disabled={mode === "running"} className="danger">
                    Expire snapshot
                  </button>
                </div>
              </div>
              {root ? (
                <div className="tree-wrap">
                  <TreeNodeView
                    node={root}
                    depth={0}
                    onRetry={retryBranch}
                    busyBranch={busyBranch}
                  />
                </div>
              ) : (
                <div className="empty">This batch has no branches yet.</div>
              )}
            </div>

            {exported ? (
              <div className="card">
                <h2>Export</h2>
                <p className="hint">
                  {exported.complete
                    ? "All branches collected. This export is a complete dataset."
                    : exported.warning}
                </p>
                <div className="kpi-row" style={{ marginBottom: 12 }}>
                  <StatTile label="Stores" value={exported.counts.stores} />
                  <StatTile label="Products" value={exported.counts.products} />
                  <StatTile label="SKUs" value={exported.counts.skus} />
                  <StatTile
                    label="Branches"
                    value={`${exported.branches.complete}/${exported.branches.total}`}
                    sub={`${exported.branches.unfinished} unfinished`}
                  />
                </div>
                {exported.incomplete_branches.length ? (
                  <>
                    <h2>Not collected</h2>
                    <table className="journal">
                      <thead>
                        <tr>
                          <th>Branch</th>
                          <th>Status</th>
                          <th>Pages</th>
                          <th>Error</th>
                        </tr>
                      </thead>
                      <tbody>
                        {exported.incomplete_branches.map((b) => (
                          <tr key={b.path}>
                            <td>{b.label}</td>
                            <td>{b.status}</td>
                            <td>{b.pages_done}</td>
                            <td className="err">{b.last_error ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </>
                ) : null}
                <div className="export-box" style={{ marginTop: 12 }}>
                  {JSON.stringify(
                    {
                      complete: exported.complete,
                      warning: exported.warning,
                      counts: exported.counts,
                      branches: exported.branches,
                      data: exported.data,
                    },
                    null,
                    2,
                  ).slice(0, 4000)}
                </div>
              </div>
            ) : null}
          </>
        ) : (
          <div className="card empty">
            No batch selected. Create one to start collecting this snapshot.
          </div>
        )}
      </div>

      <div className="col-side">
        <div className="card">
          <h2>Batch</h2>
          <p className="hint">
            One snapshot id per batch. A new batch means a new version.
          </p>
          <div className="field-row">
            <label className="field">
              Page size
              <input
                type="number"
                min={1}
                max={50}
                value={pageSize}
                onChange={(e) => setPageSize(Number(e.target.value) || 1)}
              />
            </label>
            <label className="field">
              Mode
              <select
                value={autoRun ? "auto" : "manual"}
                onChange={(e) => setAutoRun(e.target.value === "auto")}
              >
                <option value="auto">Collect now</option>
                <option value="manual">Create only</option>
              </select>
            </label>
          </div>
          {tree ? (
            <div style={{ marginTop: 12, fontSize: 12, color: "var(--text-secondary)" }}>
              <div>
                State: <strong>{tree.batch.state}</strong>
              </div>
              <div>Note: {tree.batch.note || "—"}</div>
              <div>
                Snapshot expires: {tree.batch.snapshot_expires_at ?? "not set"}
              </div>
              {tree.batch.parent_batch_id ? (
                <div>Continues: {tree.batch.parent_batch_id}</div>
              ) : null}
              {tree.batch.superseded_by ? (
                <div>Superseded by: {tree.batch.superseded_by}</div>
              ) : null}
              {runReport ? (
                <div style={{ marginTop: 8 }}>
                  Last run: {runReport.requests} requests ·{" "}
                  {runReport.stopped_because || "—"}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="card">
          <h2>Batches</h2>
          <p className="hint">Pick a batch to inspect its tree. Data survives restarts.</p>
          <div className="batch-list">
            {batches.map((b) => (
              <button
                key={b.id}
                type="button"
                className="batch-item"
                aria-current={selected === b.id}
                onClick={() => selectBatch(b.id)}
              >
                <div className="who">
                  <strong style={{ fontSize: 12 }}>{b.id}</strong>
                  <span>{b.snapshot_id}</span>
                  <span>
                    {b.branches_complete}/{b.branches_total} branches · {b.state}
                  </span>
                </div>
                <StatusChip status={batchChip(b.state)} />
              </button>
            ))}
            {batches.length === 0 ? <div className="empty">No batches yet.</div> : null}
          </div>
        </div>

        <div className="card">
          <h2>Source faults</h2>
          <p className="hint">
            Inject failures at the GraphQL source; expire a snapshot or mutate the
            base data (existing snapshots stay put).
          </p>
          <div className="field-row">
            <label className="field">
              Mode
              <select value={faultMode} onChange={(e) => setFaultMode(e.target.value)}>
                <option value="partial_error">Partial error (HTTP 200 + errors)</option>
                <option value="request_error">Whole request error</option>
                <option value="stalled_cursor">Stalled cursor</option>
                <option value="missing_end_cursor">Missing endCursor</option>
              </select>
            </label>
            <label className="field">
              Scope
              <select value={faultScope} onChange={(e) => setFaultScope(e.target.value)}>
                {SCOPE_PRESETS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="field-row" style={{ marginTop: 8 }}>
            <label className="field">
              Page offset
              <input
                value={faultOffset}
                onChange={(e) => setFaultOffset(e.target.value)}
                placeholder="any"
              />
            </label>
            <label className="field">
              Times
              <input
                value={faultTimes}
                onChange={(e) => setFaultTimes(e.target.value)}
                placeholder="∞"
              />
            </label>
          </div>
          <label className="checkline" style={{ marginTop: 8 }}>
            <input
              type="checkbox"
              checked={faultPathless}
              onChange={(e) => setFaultPathless(e.target.checked)}
            />
            Pathless error (taints the whole request)
          </label>
          <div className="toolbar" style={{ marginTop: 10 }}>
            <button type="button" onClick={addFault} disabled={mode === "running"}>
              Add rule
            </button>
            <button type="button" onClick={clearFaults} disabled={mode === "running"}>
              Clear rules
            </button>
            <button type="button" onClick={mutateData} disabled={mode === "running"}>
              Mutate base data
            </button>
          </div>
          {source ? (
            <div className="source-list" style={{ marginTop: 10 }}>
              {source.rules.map((r) => (
                <div className="item" key={r.id}>
                  <div>
                    <strong>{r.mode}</strong>
                    <div className="mono">{r.scope}</div>
                  </div>
                  <div className="mono">
                    {r.remaining === null ? "always" : `${r.remaining} left`}
                  </div>
                </div>
              ))}
              {source.rules.length === 0 ? (
                <div className="empty" style={{ padding: 8 }}>
                  No active rules.
                </div>
              ) : null}
            </div>
          ) : null}
        </div>

        {source ? (
          <div className="card">
            <h2>Local source</h2>
            <p className="hint">
              Base data v{source.version}. Snapshot contents are frozen at creation.
            </p>
            <div className="source-list">
              {source.snapshots.slice(-6).reverse().map((s) => (
                <div className="item" key={s.id}>
                  <div>
                    <div className="mono">{s.id}</div>
                    <div style={{ color: "var(--text-muted)" }}>
                      {s.expired ? "expired" : s.expires_at ? `TTL ${s.expires_at}` : "no expiry"}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="subtle"
                    onClick={() => guard(async () => {
                      await api.expireSnapshot(s.id);
                      await loadSource();
                    })}
                  >
                    expire
                  </button>
                </div>
              ))}
              {source.snapshots.length === 0 ? (
                <div className="empty" style={{ padding: 8 }}>
                  No snapshots yet.
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function countKind(node: TreeNode | undefined, kind: TreeNode["kind"]): number {
  if (!node) return 0;
  let n = node.kind === kind ? 1 : 0;
  for (const child of node.children) n += countKind(child, kind);
  return n;
}

function batchChip(state: string) {
  switch (state) {
    case "completed":
      return "complete";
    case "active":
      return "pending";
    case "paused":
      return "partial";
    case "expired":
    case "aborted":
      return "failed";
    default:
      return "pending";
  }
}
