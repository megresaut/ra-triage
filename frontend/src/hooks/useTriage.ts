import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { fetchTriage, type TriageSections } from "@/lib/api";

/** Fetch + poll the bug's parsed TRIAGE.md every 10s. */
export function useTriage(
  id: number | undefined,
): UseQueryResult<TriageSections> {
  return useQuery({
    queryKey: ["bug", id, "triage"],
    queryFn: ({ signal }) => fetchTriage(id as number, signal),
    enabled: id !== undefined,
    refetchInterval: 10_000,
    // 404 on missing worktree is a valid state pre-pipeline; don't retry
    // on it — the frontend renders an empty card instead.
    retry: false,
  });
}
