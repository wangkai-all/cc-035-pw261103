import type { BranchStatus, TreeNode } from "./types";

const LABELS: Record<string, string> = {
  complete: "Complete",
  partial: "In progress",
  failed: "Failed",
  stalled: "Cursor stalled",
  pending: "Waiting",
  missing: "Missing",
};

const ICONS: Record<string, string> = {
  complete: "✓",
  partial: "◐",
  failed: "✕",
  stalled: "!",
  pending: "○",
  missing: "?",
};

/** Status is always carried by icon + label, never by color alone. */
export function StatusChip({ status }: { status: BranchStatus | "missing" }) {
  return (
    <span className="chip" data-status={status}>
      <span className="dot" aria-hidden="true" />
      <span className="icon" aria-hidden="true">
        {ICONS[status] ?? "•"}
      </span>
      {LABELS[status] ?? status}
    </span>
  );
}

export interface MeterProps {
  value: number;
  max: number;
  caption?: string;
}

/** A single ratio against a limit. The number is in the caption, not on the bar. */
export function Meter({ value, max, caption }: MeterProps) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="meter">
      <div
        className="track"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
        aria-valuetext={caption ?? `${value} of ${max}`}
      >
        <div className="fill" style={{ width: `${pct}%` }} />
      </div>
      {caption ? <div className="caption">{caption}</div> : null}
    </div>
  );
}

export interface StatTileProps {
  label: string;
  value: string | number;
  sub?: string;
  meter?: { value: number; max: number; caption: string };
}

export function StatTile({ label, value, sub, meter }: StatTileProps) {
  return (
    <div className="stat">
      <span className="label">{label}</span>
      <span className="value">{value}</span>
      {sub ? <span className="sub">{sub}</span> : null}
      {meter ? (
        <div className="meter">
          <div
            className="track"
            role="meter"
            aria-valuemin={0}
            aria-valuemax={meter.max}
            aria-valuenow={meter.value}
            aria-valuetext={meter.caption}
          >
            <div
              className="fill"
              style={{
                width: `${meter.max > 0 ? Math.min(100, (meter.value / meter.max) * 100) : 0}%`,
              }}
            />
          </div>
          <div className="caption">{meter.caption}</div>
        </div>
      ) : null}
    </div>
  );
}

export function nodePages(node: TreeNode): string {
  if (node.kind === "sku") return "1 record";
  if (!node.branch) return "no branch";
  const done = node.pages_done;
  const remaining = node.pages_remaining;
  if (node.status === "complete") return `${done} page${done === 1 ? "" : "s"}`;
  if (remaining && remaining > 0) return `${done} done · ${remaining} left`;
  return `${done} page${done === 1 ? "" : "s"}`;
}
