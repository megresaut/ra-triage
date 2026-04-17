// Typed fetch client built on top of the OpenAPI-generated types in
// `./types.ts`. Intentionally thin — no axios, no SWR, just a fetch wrapper
// and a small shape-preserving `apiFetch` helper. TanStack Query handles
// caching, polling, and revalidation.

import type { components } from "./types";

export const BACKEND_BASE_URL = "http://127.0.0.1:5000";

// Re-export the types we care about as convenient aliases.
export type Bug = components["schemas"]["BugResponse"];
export type AgentRun = components["schemas"]["AgentRunResponse"];
export type Project = components["schemas"]["ProjectResponse"];
export type Repo = components["schemas"]["RepoResponse"];
export type BugState = components["schemas"]["BugState"];
export type PollerEnqueueRequest =
  components["schemas"]["PollerEnqueueRequest"];
export type PullRequest = components["schemas"]["PullRequestResponse"];
export type ConfirmResponse = components["schemas"]["ConfirmResponse"];
export type RejectRequest = components["schemas"]["RejectRequest"];
export type RejectResponse = components["schemas"]["RejectResponse"];
export type CancelResponse = components["schemas"]["CancelResponse"];
export type RetryResponse = components["schemas"]["RetryResponse"];
export type TakeOverResponse = components["schemas"]["TakeOverResponse"];

export interface HealthResponse {
  status: string;
}

interface FetchOptions {
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | null | undefined>;
}

function buildQuery(
  query: FetchOptions["query"],
): string {
  if (!query) {
    return "";
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) {
      continue;
    }
    params.append(key, String(value));
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

async function apiGet<T>(path: string, opts: FetchOptions = {}): Promise<T> {
  const url = `${BACKEND_BASE_URL}${path}${buildQuery(opts.query)}`;
  const response = await fetch(url, { signal: opts.signal });
  if (!response.ok) {
    throw new Error(`GET ${path} failed: HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

/** Thrown by apiPost/apiGet when the backend returns a non-2xx. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(path: string, status: number, detail: string) {
    super(`POST ${path} failed: HTTP ${status}${detail ? ` — ${detail}` : ""}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function apiPost<T, B>(
  path: string,
  body: B,
  opts: FetchOptions = {},
): Promise<T> {
  const url = `${BACKEND_BASE_URL}${path}${buildQuery(opts.query)}`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: opts.signal,
  });
  if (!response.ok) {
    // Try to extract FastAPI's `detail` for a better message; fall back
    // to the raw status.
    let detail = "";
    try {
      const parsed = (await response.json()) as { detail?: string };
      if (typeof parsed.detail === "string") {
        detail = parsed.detail;
      }
    } catch {
      // Ignore — non-JSON body.
    }
    throw new ApiError(path, response.status, detail);
  }
  return (await response.json()) as T;
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return apiGet<HealthResponse>("/health", { signal });
}

export async function fetchBugs(
  params: { projectId?: string; state?: BugState; includeTerminal?: boolean },
  signal?: AbortSignal,
): Promise<Bug[]> {
  return apiGet<Bug[]>("/bugs", {
    signal,
    query: {
      project_id: params.projectId,
      state: params.state,
      include_terminal: params.includeTerminal,
    },
  });
}

export async function fetchBug(id: number, signal?: AbortSignal): Promise<Bug> {
  return apiGet<Bug>(`/bugs/${id}`, { signal });
}

export async function fetchAgentRuns(
  bugId: number,
  signal?: AbortSignal,
): Promise<AgentRun[]> {
  return apiGet<AgentRun[]>(`/bugs/${bugId}/agent-runs`, { signal });
}

export async function fetchProjects(signal?: AbortSignal): Promise<Project[]> {
  return apiGet<Project[]>("/projects", { signal });
}

export async function fetchProject(
  id: string,
  signal?: AbortSignal,
): Promise<Project> {
  return apiGet<Project>(`/projects/${id}`, { signal });
}

export async function enqueueBug(
  body: PollerEnqueueRequest,
  signal?: AbortSignal,
): Promise<Bug> {
  return apiPost<Bug, PollerEnqueueRequest>("/poller/enqueue", body, { signal });
}

// --- Agent 5 additions ---------------------------------------------------

/** Parsed TRIAGE.md payload. Shape mirrors TriageResponse on the backend. */
export interface TriageSections {
  sections: Record<string, string>;
  raw: string;
  path: string;
}

export async function fetchTriage(
  bugId: number,
  signal?: AbortSignal,
): Promise<TriageSections> {
  return apiGet<TriageSections>(`/bugs/${bugId}/triage`, { signal });
}

/** URL to the rendered prompt file for a specific agent run. */
export function promptUrl(
  bugId: number,
  role: string,
  attempt: number,
): string {
  const params = new URLSearchParams({ role, attempt: String(attempt) });
  return `${BACKEND_BASE_URL}/bugs/${bugId}/prompt?${params.toString()}`;
}

/** WebSocket URL for per-bug live log frames. */
export function bugLogsWsUrl(bugId: number): string {
  // Same host as backend REST; ``ws://`` scheme for the local dev.
  return `ws://127.0.0.1:5000/ws/bugs/${bugId}/logs`;
}

// --- Agent 6 additions ---------------------------------------------------

/** Response from GET /bugs/:id/diff. */
export interface DiffResponse {
  repo: string;
  base_ref: string;
  head_ref: string;
  diff: string;
}

/** Response from GET /bugs/:id/diff/summary. */
export interface DiffSummaryResponse {
  repo: string;
  files_changed: number;
  insertions: number;
  deletions: number;
}

export async function fetchDiff(
  bugId: number,
  repo: string,
  signal?: AbortSignal,
): Promise<DiffResponse> {
  return apiGet<DiffResponse>(`/bugs/${bugId}/diff`, {
    signal,
    query: { repo },
  });
}

export async function fetchDiffSummary(
  bugId: number,
  repo: string,
  signal?: AbortSignal,
): Promise<DiffSummaryResponse> {
  return apiGet<DiffSummaryResponse>(`/bugs/${bugId}/diff/summary`, {
    signal,
    query: { repo },
  });
}

// --- Agent 7 additions: Launch for Testing -------------------------------

/** Live (or "starting") test instance snapshot — matches /test-instances/current. */
export interface TestInstance {
  bug_id: number;
  status: "starting" | "running" | "stopping" | "stopped" | "crashed" | "none";
  ports: Record<string, number>;
  pids: Record<string, number>;
  db_snapshot_hash: string | null;
  launched_at: string;
  stopped_at?: string | null;
  browser_port?: number | null;
}

/** POST /test-instances/launch. */
export async function launchTestInstance(
  bugId: number,
  signal?: AbortSignal,
): Promise<TestInstance> {
  return apiPost<TestInstance, { bug_id: number }>(
    "/test-instances/launch",
    { bug_id: bugId },
    { signal },
  );
}

/** POST /test-instances/stop. */
export async function stopTestInstance(
  signal?: AbortSignal,
): Promise<{ status: "stopped" | "none" }> {
  return apiPost<{ status: "stopped" | "none" }, Record<string, never>>(
    "/test-instances/stop",
    {},
    { signal },
  );
}

/** GET /test-instances/current — 204 resolves to null. */
export async function fetchCurrentTestInstance(
  signal?: AbortSignal,
): Promise<TestInstance | null> {
  const url = `${BACKEND_BASE_URL}/test-instances/current`;
  const response = await fetch(url, { signal });
  if (response.status === 204) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`GET /test-instances/current failed: HTTP ${response.status}`);
  }
  return (await response.json()) as TestInstance;
}

/** WebSocket URL for /ws/test-instance. */
export function testInstanceWsUrl(): string {
  return "ws://127.0.0.1:5000/ws/test-instance";
}

// --- Agent 8 additions: Confirm / Reject / Cancel / Retry / Take Over ----

/** POST /bugs/:id/confirm — starts the merge flow. */
export async function confirmBug(
  bugId: number,
  signal?: AbortSignal,
): Promise<ConfirmResponse> {
  return apiPost<ConfirmResponse, Record<string, never>>(
    `/bugs/${bugId}/confirm`,
    {},
    { signal },
  );
}

/**
 * POST /bugs/:id/reject — records a rejection reason and either re-runs
 * the Investigator (count < 3) or pins the bug to BLOCKED (count == 3).
 */
export async function rejectBug(
  bugId: number,
  reason: string,
  signal?: AbortSignal,
): Promise<RejectResponse> {
  return apiPost<RejectResponse, { reason: string }>(
    `/bugs/${bugId}/reject`,
    { reason },
    { signal },
  );
}

/** POST /bugs/:id/cancel — transitions the bug to CANCELLED + cleans up. */
export async function cancelBug(
  bugId: number,
  signal?: AbortSignal,
): Promise<CancelResponse> {
  return apiPost<CancelResponse, Record<string, never>>(
    `/bugs/${bugId}/cancel`,
    {},
    { signal },
  );
}

/** POST /bugs/:id/retry — re-runs the most recent agent with attempt+1. */
export async function retryBug(
  bugId: number,
  signal?: AbortSignal,
): Promise<RetryResponse> {
  return apiPost<RetryResponse, Record<string, never>>(
    `/bugs/${bugId}/retry`,
    {},
    { signal },
  );
}

/**
 * POST /bugs/:id/take-over — marks the bug as taken over manually.
 * Transitions to BLOCKED; the worktree is retained.
 */
export async function takeOverBug(
  bugId: number,
  signal?: AbortSignal,
): Promise<TakeOverResponse> {
  return apiPost<TakeOverResponse, Record<string, never>>(
    `/bugs/${bugId}/take-over`,
    {},
    { signal },
  );
}
