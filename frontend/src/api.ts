const BASE = import.meta.env.VITE_API_BASE ?? "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* keep status text */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ ok: boolean; source_url: string; source_reachable: boolean }>("/health"),
  source: () => request<import("./types").SourceSummary>("/source"),
  listBatches: () => request<{ batches: import("./types").BatchSummary[] }>("/batches"),
  getBatch: (batchId: string) => request<import("./types").TreeResponse>(`/batches/${batchId}`),
  createBatch: (body: { source_label: string; page_size: number; auto_run: boolean }) =>
    request<{ batch_id: string; snapshot_id: string; run?: import("./types").RunReport }>(
      "/batches",
      { method: "POST", body: JSON.stringify(body) },
    ),
  runBatch: (batchId: string, body: { max_requests?: number; branch_path?: string | null } = {}) =>
    request<import("./types").RunReport>(`/batches/${batchId}/run`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  resumeBatch: (batchId: string, body: { max_requests?: number } = {}) =>
    request<import("./types").RunReport>(`/batches/${batchId}/resume`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  retryBranch: (
    batchId: string,
    body: {
      branch_path: string;
      include_descendants?: boolean;
      run_after?: boolean;
      scope_to_branch?: boolean;
    },
  ) =>
    request<{
      reset_branches: number;
      run?: import("./types").RunReport;
    }>(`/batches/${batchId}/retry`, { method: "POST", body: JSON.stringify(body) }),
  successor: (batchId: string, body: { auto_run?: boolean } = {}) =>
    request<{ batch_id: string; snapshot_id: string; run?: import("./types").RunReport }>(
      `/batches/${batchId}/successor`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  exportBatch: (batchId: string) =>
    request<import("./types").ExportResponse>(`/batches/${batchId}/export`),
  addRule: (rule: Record<string, unknown>) =>
    request<import("./types").FaultRule>("/source/rules", {
      method: "POST",
      body: JSON.stringify(rule),
    }),
  clearRules: () => request<{ cleared: boolean }>("/source/rules/clear", { method: "POST" }),
  expireSnapshot: (snapshotId: string) =>
    request<{ expired: string }>(`/source/snapshots/${snapshotId}/expire`, { method: "POST" }),
  mutate: (body: Record<string, unknown>) =>
    request<{ version: number }>("/source/mutate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
