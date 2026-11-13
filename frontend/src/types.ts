export type BranchKind = "store_page" | "product_page" | "sku_page";
export type BranchStatus = "pending" | "partial" | "complete" | "failed" | "stalled";
export type BatchState = "active" | "paused" | "completed" | "expired" | "aborted";
export type RequestOutcome =
  | "in_flight"
  | "complete"
  | "partial"
  | "failed"
  | "cursor_stalled"
  | "expired"
  | "interrupted";

export interface PageRequest {
  id: number;
  batch_id: string;
  branch_path: string;
  request_kind: BranchKind | "snapshot_info";
  attempt: number;
  started_at: string;
  finished_at: string | null;
  outcome: RequestOutcome;
  http_status: number | null;
  requested_cursor: string | null;
  next_cursor: string | null;
  has_next_page: number | null;
  rows_added: number;
  error_summary: string | null;
  error_paths: (string | number)[][];
}

export interface BranchInfo {
  branch_path: string;
  kind: BranchKind;
  status: BranchStatus;
  pages_done: number;
  pages_remaining: number | null;
  total_count: number | null;
  has_next: boolean;
  cursor: string | null;
  last_error: string | null;
  request_count: number;
  requests: PageRequest[];
}

export interface TreeNode {
  path: string;
  kind: "directory" | "store" | "product" | "sku";
  label: string;
  status: BranchStatus | "missing" | "complete";
  present: boolean;
  store_id: string | null;
  product_id: string | null;
  branch_path: string | null;
  pages_done: number;
  pages_remaining: number | null;
  total_count: number | null;
  has_next: boolean;
  last_error: string | null;
  request_count: number;
  requests: PageRequest[];
  branch: BranchInfo | null;
  data?: Record<string, unknown>;
  children: TreeNode[];
  rollup?: Rollup;
  subtree_status?: "complete" | "failed" | "stalled" | "partial";
  sku_count?: number;
}

export interface Rollup {
  total: number;
  complete: number;
  incomplete: number;
  failed: number;
  missing: number;
  stalled: number;
}

export interface BatchInfo {
  id: string;
  snapshot_id: string;
  source_label: string;
  state: BatchState;
  note: string;
  created_at: string;
  closed_at: string | null;
  page_size: number;
  snapshot_expires_at: string | null;
  parent_batch_id: string | null;
  superseded_by: string | null;
}

export interface TreeResponse {
  batch: BatchInfo;
  roots: TreeNode[];
  counts: Record<string, number>;
  totals: {
    branches: number;
    complete: number;
    unfinished: number;
    failed: number;
    stalled: number;
    pending: number;
    partial: number;
  };
  complete: boolean;
}

export interface ExportBranch {
  path: string;
  kind: BranchKind;
  label: string;
  status?: BranchStatus;
  pages_done: number;
  last_error?: string | null;
}

export interface ExportResponse {
  batch_id: string;
  snapshot_id: string;
  source_label: string;
  batch_state: BatchState;
  complete: boolean;
  warning: string | null;
  snapshot_expires_at: string | null;
  branches: { total: number; complete: number; unfinished: number };
  complete_branches: ExportBranch[];
  incomplete_branches: ExportBranch[];
  counts: { stores: number; products: number; skus: number };
  data: {
    stores: Record<string, unknown>[];
    products: Record<string, unknown>[];
    skus: Record<string, unknown>[];
  };
}

export interface BatchSummary {
  id: string;
  snapshot_id: string;
  source_label: string;
  state: BatchState;
  note: string;
  created_at: string;
  closed_at: string | null;
  snapshot_expires_at: string | null;
  parent_batch_id: string | null;
  superseded_by: string | null;
  branches_total: number;
  branches_complete: number;
  branches_unfinished: number;
  records: Record<string, number>;
}

export interface FaultRule {
  id: string;
  mode: "partial_error" | "request_error" | "stalled_cursor" | "missing_end_cursor";
  scope: string;
  at_offset: number | null;
  times: number | null;
  remaining: number | null;
  fired: number;
  message: string;
  pathless: boolean;
}

export interface SourceSummary {
  version: number;
  default_page_size: number;
  stores: { store_id: string; name: string; products: number; skus: number }[];
  snapshots: {
    id: string;
    label: string;
    created_at: string;
    expires_at: string | null;
    expired: boolean;
  }[];
  rules: FaultRule[];
}

export interface RunReport {
  batch_id: string;
  requests: number;
  state: BatchState;
  note: string;
  stopped_because: string;
  last_request: {
    branch: string;
    outcome: RequestOutcome;
    error?: string;
    rows?: number;
    paused_children?: string[];
  } | null;
}
