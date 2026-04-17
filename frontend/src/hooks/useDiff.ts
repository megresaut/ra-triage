// TanStack Query hook for GET /bugs/:id/diff.
//
// Polls every 10s while the bug is in a state where the diff is
// actively changing (Fixer/Tester are running, or the bug just landed
// in READY_FOR_REVIEW and the diff may still be settling). Other states
// → static fetch, no polling. This keeps the pipeline's write pressure
// off bugs that are in a stable state (OPEN/TRIAGED/BLOCKED/FIXED/etc).

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { fetchDiff, type DiffResponse } from "@/lib/api";

const POLLING_STATES: ReadonlySet<string> = new Set([
  "FIXING",
  "TESTING",
  "READY_FOR_REVIEW",
  "FIXING_PARALLEL",
  "INTEGRATION_TESTING",
]);

export function useDiff(
  bugId: number | undefined,
  repo: string | undefined,
  state: string | undefined,
): UseQueryResult<DiffResponse> {
  const shouldPoll = state !== undefined && POLLING_STATES.has(state);
  return useQuery({
    queryKey: ["bug", bugId, "diff", repo],
    queryFn: ({ signal }) => fetchDiff(bugId as number, repo as string, signal),
    enabled: bugId !== undefined && repo !== undefined,
    refetchInterval: shouldPoll ? 10_000 : false,
    // 404 is a valid "no worktree yet" state — don't retry.
    retry: false,
  });
}
