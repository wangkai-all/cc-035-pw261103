import { useCallback, useMemo, useState } from "react";
import type { PageRequest, TreeNode } from "../types";
import { StatusChip, nodePages } from "../components";

interface Props {
  node: TreeNode;
  depth: number;
  onRetry: (node: TreeNode) => void;
  busyBranch: string | null;
}

function isRetryable(node: TreeNode): boolean {
  return (
    node.branch_path != null &&
    ["failed", "stalled", "pending", "partial"].includes(node.status)
  );
}

export function TreeNodeView({ node, depth, onRetry, busyBranch }: Props) {
  // The directory and every store/product starts expanded; SKU leaves have
  // no children.  Large trees are collapsed at SKU level only.
  const [open, setOpen] = useState(depth < 2);
  const [showJournal, setShowJournal] = useState(false);
  const hasChildren = node.children.length > 0;
  const busy = busyBranch === node.branch_path;
  const retryable = isRetryable(node) && node.status !== "pending";

  return (
    <div className={`depth-${depth}`}>
      <div className="tree-row">
        <div className="name" style={{ paddingLeft: depth * 2 }}>
          {hasChildren ? (
            <button
              type="button"
              className="subtle twisty"
              aria-expanded={open}
              aria-label={open ? "Collapse" : "Expand"}
              onClick={() => setOpen((v) => !v)}
            >
              {open ? "▾" : "▸"}
            </button>
          ) : (
            <span className="twisty" aria-hidden="true" />
          )}
          <span className="text" title={node.path}>
            {node.label}
          </span>
          <span className="meta">
            {node.kind === "sku" ? "" : `/${node.path.split("/").slice(-1)[0]}`}
          </span>
          {node.present === false ? (
            <span className="meta" title="Known from an error path; no data collected">
              no data
            </span>
          ) : null}
        </div>
        <div className="chips">
          <StatusChip status={node.status as never} />
          {node.children.length && node.subtree_status !== node.status ? (
            <StatusChip
              status={(node.subtree_status ?? node.status) as never}
            />
          ) : null}
        </div>
        <div className="pages">{nodePages(node)}</div>
        <div className="pages">{node.request_count} req</div>
      </div>

      {(node.last_error || retryable || showJournal) && (
        <div className="tree-row" style={{ paddingLeft: depth * 2 + 28 }}>
          {node.last_error ? (
            <div className="error" title={node.last_error}>
              {node.last_error}
            </div>
          ) : (
            <div className="error" />
          )}
          <div />
          <div />
          <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
            {node.request_count > 0 ? (
              <button
                type="button"
                className="subtle"
                onClick={() => setShowJournal((v) => !v)}
                aria-expanded={showJournal}
              >
                {showJournal ? "hide log" : "log"}
              </button>
            ) : null}
            {retryable ? (
              <button
                type="button"
                className="subtle"
                disabled={busy}
                onClick={() => onRetry(node)}
                title="Retry only this branch; other results are kept"
              >
                {busy ? "retrying…" : "retry"}
              </button>
            ) : null}
          </div>
        </div>
      )}

      {showJournal && node.branch ? (
        <div className="branch-detail">
          <Journal branchPath={node.branch_path ?? ""} requests={node.requests} />
        </div>
      ) : null}

      {open && hasChildren ? (
        <div className="tree-children">
          {node.children.map((child) => (
            <TreeNodeView
              key={child.path}
              node={child}
              depth={depth + 1}
              onRetry={onRetry}
              busyBranch={busyBranch}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Journal({ branchPath, requests }: { branchPath: string; requests: PageRequest[] }) {
  const rows = useMemo(
    () => requests.slice().sort((a, b) => a.id - b.id),
    [requests],
  );
  const [copied, setCopied] = useState(false);
  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(branchPath);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard may be unavailable; the path is visible below anyway */
    }
  }, [branchPath]);

  if (rows.length === 0) {
    return <div className="empty">No requests recorded for this branch yet.</div>;
  }
  return (
    <table className="journal">
      <thead>
        <tr>
          <th>#</th>
          <th>Outcome</th>
          <th>HTTP</th>
          <th>Cursor in → out</th>
          <th>Rows</th>
          <th>
            Error{" "}
            <button type="button" className="subtle" onClick={copy}>
              {copied ? "copied" : "copy path"}
            </button>
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>{r.attempt}</td>
            <td>
              <StatusChip status={outcomeStatus(r.outcome)} />
            </td>
            <td>{r.http_status ?? "—"}</td>
            <td title={`${r.requested_cursor ?? "start"} → ${r.next_cursor ?? "end"}`}>
              {short(r.requested_cursor)} → {short(r.next_cursor)}
            </td>
            <td>{r.rows_added}</td>
            <td className="err" title={r.error_summary ?? ""}>
              {r.error_summary ?? "—"}
              {r.error_paths.length ? (
                <div className="meta">{JSON.stringify(r.error_paths)}</div>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function outcomeStatus(outcome: string) {
  switch (outcome) {
    case "complete":
      return "complete";
    case "partial":
      return "partial";
    case "cursor_stalled":
      return "stalled";
    case "failed":
    case "expired":
      return "failed";
    default:
      return "pending";
  }
}

function short(cursor: string | null): string {
  if (!cursor) return "start";
  return `${cursor.slice(0, 6)}…`;
}
