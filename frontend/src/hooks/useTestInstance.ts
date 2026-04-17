// Test-instance status hook.
//
// Tiny wrapper around TanStack Query that polls /test-instances/current at
// 5s. The WS hook in useTestInstanceLogs.ts is the real-time channel; this
// poll backstops reconnects and the occasional missed status frame.

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";

import {
  fetchCurrentTestInstance,
  launchTestInstance,
  stopTestInstance,
  type TestInstance,
} from "@/lib/api";

/** Poll the current test instance at a modest cadence. */
export function useCurrentTestInstance(): UseQueryResult<TestInstance | null> {
  return useQuery({
    queryKey: ["test-instance", "current"],
    queryFn: ({ signal }) => fetchCurrentTestInstance(signal),
    refetchInterval: 5_000,
    retry: false,
  });
}

/** Mutation wrapping POST /test-instances/launch. */
export function useLaunchTestInstance() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (bugId: number) => launchTestInstance(bugId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["test-instance"] });
      qc.invalidateQueries({ queryKey: ["bug"] });
      qc.invalidateQueries({ queryKey: ["bugs"] });
    },
  });
}

/** Mutation wrapping POST /test-instances/stop. */
export function useStopTestInstance() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => stopTestInstance(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["test-instance"] });
      qc.invalidateQueries({ queryKey: ["bug"] });
      qc.invalidateQueries({ queryKey: ["bugs"] });
    },
  });
}
