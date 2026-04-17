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
    throw new Error(`POST ${path} failed: HTTP ${response.status}`);
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
