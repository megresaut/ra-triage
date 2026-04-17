import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import {
  fetchAgentRuns,
  fetchBug,
  type AgentRun,
  type Bug,
} from "@/lib/api";

/** Bug detail view hook. Auto-refetches every 10s as a WS fallback. */
export function useBugDetail(
  id: number | undefined,
): UseQueryResult<Bug> {
  return useQuery({
    queryKey: ["bug", id],
    queryFn: ({ signal }) => fetchBug(id as number, signal),
    enabled: id !== undefined,
    refetchInterval: 10_000,
    retry: false,
  });
}

export function useAgentRuns(
  id: number | undefined,
): UseQueryResult<AgentRun[]> {
  return useQuery({
    queryKey: ["bug", id, "agent-runs"],
    queryFn: ({ signal }) => fetchAgentRuns(id as number, signal),
    enabled: id !== undefined,
    refetchInterval: 10_000,
    retry: false,
  });
}
