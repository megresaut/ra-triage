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

// --- Agent 9 additions: Escalation Brief actions -------------------------

/** One option the reviewer can pick on an escalated bug. */
export interface ParsedBriefOption {
  id: string;
  label: string;
  kind: "approve" | "alternative" | "take_over";
}

/** Structured view of an Escalation Brief, matching backend `ParsedBriefResponse`. */
export interface ParsedBrief {
  tldr: string | null;
  why_blocked: string | null;
  the_bug: string | null;
  why_change_necessary: string | null;
  proposed_approach: string | null;
  ranked_alternatives: string[];
  files_that_would_be_touched: string[];
  test_plan: string | null;
  options: ParsedBriefOption[];
  raw: string;
}

/** Full escalation row returned by `GET /bugs/:id/escalation`. */
export interface EscalationDetail {
  id: number;
  bug_id: number;
  triggered_rule: string | null;
  created_at: string;
  parsed: ParsedBrief;
}

/** Response from approve / pick-alternative — matches `EscalationActionResponse`. */
export interface EscalationActionResult {
  status: string;
  bug_id: number;
  escalation_id: number;
  new_state: string;
  resolved_option: string;
}

/** Response from take-over (escalation-scoped). */
export interface EscalationTakeOverResult {
  status: string;
  bug_id: number;
  escalation_id: number;
  resolved_option: string;
}

/**
 * GET /bugs/:id/escalation — fetch the latest unresolved escalation.
 * Resolves to null on 404 (no active escalation / bug not found) so callers
 * can render nothing without a loud error.
 */
export async function fetchEscalation(
  bugId: number,
  signal?: AbortSignal,
): Promise<EscalationDetail | null> {
  const url = `${BACKEND_BASE_URL}/bugs/${bugId}/escalation`;
  const response = await fetch(url, { signal });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(
      `GET /bugs/${bugId}/escalation failed: HTTP ${response.status}`,
    );
  }
  return (await response.json()) as EscalationDetail;
}

/** POST /bugs/:id/escalation/approve — accept the recommended approach. */
export async function approveEscalation(
  bugId: number,
  signal?: AbortSignal,
): Promise<EscalationActionResult> {
  return apiPost<EscalationActionResult, { option_id: string }>(
    `/bugs/${bugId}/escalation/approve`,
    { option_id: "approve_recommended" },
    { signal },
  );
}

/**
 * POST /bugs/:id/escalation/pick-alternative — pick a specific alternative.
 * ``notes`` is optional (<=500 chars) and shows up in the Fixer's
 * escalation context.
 */
export async function pickAlternative(
  bugId: number,
  optionId: string,
  notes?: string,
  signal?: AbortSignal,
): Promise<EscalationActionResult> {
  const body: { option_id: string; notes?: string } = { option_id: optionId };
  if (notes !== undefined && notes.trim().length > 0) {
    body.notes = notes;
  }
  return apiPost<EscalationActionResult, { option_id: string; notes?: string }>(
    `/bugs/${bugId}/escalation/pick-alternative`,
    body,
    { signal },
  );
}

/**
 * POST /bugs/:id/escalation/take-over — mark the escalation taken-over
 * manually. The bug stays in BLOCKED; worktree is NOT cleaned up.
 */
export async function takeOverEscalation(
  bugId: number,
  signal?: AbortSignal,
): Promise<EscalationTakeOverResult> {
  return apiPost<EscalationTakeOverResult, Record<string, never>>(
    `/bugs/${bugId}/escalation/take-over`,
    {},
    { signal },
  );
}

// --- Agent 11 additions: Rebase / conflict surface ----------------------

/** One row from the ``rebase_attempts`` audit table. */
export interface RebaseAttempt {
  id: number;
  bug_id: number;
  repo_name: string;
  trigger_bug_id: number;
  result: "clean" | "conflict" | "error";
  conflict_files: string[];
  created_at: string;
}

/** Snapshot of the conflict diff for a BLOCKED-with-conflict bug. */
export interface ConflictDiff {
  bug_id: number;
  repo_name: string;
  diff: string;
}

/** Response from POST /bugs/:id/retry-fixer-with-conflict. */
export interface RetryFixerWithConflictResult {
  status: string;
  bug_id: number;
  new_state: string;
}

/**
 * GET /bugs/:id/rebase-attempts — every rebase attempt the sweeper
 * has made for this bug, oldest first.
 */
export async function fetchRebaseAttempts(
  bugId: number,
  signal?: AbortSignal,
): Promise<RebaseAttempt[]> {
  return apiGet<RebaseAttempt[]>(`/bugs/${bugId}/rebase-attempts`, {
    signal,
  });
}

/**
 * GET /bugs/:id/conflict-diff — diff snapshot for the conflict
 * worktree. Resolves to ``{diff: ""}`` if the worktree was cleaned up
 * or git failed; the frontend renders a "(no snapshot)" fallback.
 *
 * Resolves to ``null`` on 404 (no active conflict) so callers can
 * conditionally render without a loud error.
 */
export async function fetchConflictDiff(
  bugId: number,
  repo?: string,
  signal?: AbortSignal,
): Promise<ConflictDiff | null> {
  const url = `${BACKEND_BASE_URL}/bugs/${bugId}/conflict-diff${
    repo ? `?repo=${encodeURIComponent(repo)}` : ""
  }`;
  const response = await fetch(url, { signal });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(
      `GET /bugs/${bugId}/conflict-diff failed: HTTP ${response.status}`,
    );
  }
  return (await response.json()) as ConflictDiff;
}

/**
 * POST /bugs/:id/retry-fixer-with-conflict — re-run the Fixer with
 * the conflict files + diff snapshot threaded into the prompt.
 */
export async function retryFixerWithConflict(
  bugId: number,
  signal?: AbortSignal,
): Promise<RetryFixerWithConflictResult> {
  return apiPost<RetryFixerWithConflictResult, Record<string, never>>(
    `/bugs/${bugId}/retry-fixer-with-conflict`,
    {},
    { signal },
  );
}

// --- Agent 12 additions: needs-manual-review flag ------------------------

/** Response from POST /bugs/:id/clear-manual-review-flag. */
export interface ClearManualReviewFlagResult {
  status: string;
  bug_id: number;
  needs_manual_review: boolean;
}

/**
 * POST /bugs/:id/clear-manual-review-flag — dismiss the yellow
 * "needs manual review" banner the resume path set at boot. Idempotent.
 */
export async function clearManualReviewFlag(
  bugId: number,
  signal?: AbortSignal,
): Promise<ClearManualReviewFlagResult> {
  return apiPost<ClearManualReviewFlagResult, Record<string, never>>(
    `/bugs/${bugId}/clear-manual-review-flag`,
    {},
    { signal },
  );
}

// --- Agent 13 additions: Settings page -----------------------------------

/** Public-safe view of one repo, mirroring backend ``SettingsRepoView``. */
export interface SettingsRepoView {
  name: string;
  role: string;
  branch_prefix: string;
  main_branch: string;
  dev_port: number;
  test_launch_port: number;
  integration_port: number;
  github_origin: string;
}

/** Read-only deny-list view for the Settings UI. */
export interface SettingsDenyListView {
  paths: string[];
  categories: string[];
}

/**
 * One project's full Settings view. ``auto_merge`` is the *effective*
 * value (override-or-default); ``auto_merge_default`` is the registry
 * hardcoded value, surfaced so the UI can show "(default: off)" when
 * an override is in place.
 */
export interface SettingsProjectView {
  id: string;
  display_name: string;
  auto_merge: boolean;
  auto_merge_default: boolean;
  repos: SettingsRepoView[];
  deny_list: SettingsDenyListView;
}

/** Top-level shape of ``GET /settings``. */
export interface SettingsResponse {
  projects: SettingsProjectView[];
}

/** Response from ``PUT /settings/projects/{id}``. */
export interface UpdateProjectSettingsResponse {
  project_id: string;
  auto_merge: boolean;
  auto_merge_default: boolean;
}

/** GET /settings — registry view + auto-merge effective/default per project. */
export async function fetchSettings(
  signal?: AbortSignal,
): Promise<SettingsResponse> {
  return apiGet<SettingsResponse>("/settings", { signal });
}

/**
 * PUT /settings/projects/{id} — toggle the per-project auto-merge override.
 *
 * **Behavior contract:** in v1 this is persisted but does NOT change merging
 * behavior — every fix still requires a manual Confirm click. The UI
 * surfaces a tooltip explaining this.
 */
export async function updateProjectAutoMerge(
  projectId: string,
  autoMerge: boolean,
  signal?: AbortSignal,
): Promise<UpdateProjectSettingsResponse> {
  const url = `${BACKEND_BASE_URL}/settings/projects/${encodeURIComponent(
    projectId,
  )}`;
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_merge: autoMerge }),
    signal,
  });
  if (!response.ok) {
    let detail = "";
    try {
      const parsed = (await response.json()) as { detail?: string };
      if (typeof parsed.detail === "string") {
        detail = parsed.detail;
      }
    } catch {
      // Ignore — non-JSON body.
    }
    throw new ApiError(
      `/settings/projects/${projectId}`,
      response.status,
      detail,
    );
  }
  return (await response.json()) as UpdateProjectSettingsResponse;
}
