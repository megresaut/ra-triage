import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { fetchBugs, type Bug, type BugState } from "@/lib/api";

export interface UseBugsOptions {
  projectId?: string;
  state?: BugState;
  includeTerminal?: boolean;
}

/** Queue-view hook. Polls every 10s per the product plan. */
export function useBugs(opts: UseBugsOptions = {}): UseQueryResult<Bug[]> {
  const { projectId, state, includeTerminal } = opts;
  return useQuery({
    queryKey: ["bugs", { projectId, state, includeTerminal }],
    queryFn: ({ signal }) =>
      fetchBugs(
        { projectId, state, includeTerminal },
        signal,
      ),
    refetchInterval: 10_000,
    retry: false,
  });
}
