import { useQuery } from "@tanstack/react-query";

import { fetchHealth } from "@/lib/api";

// v1 scaffold — Agent 2 replaces this with the real queue table.
// For the scaffold this page renders a backend connectivity indicator so that
// `make dev` in the monorepo produces something meaningful in the browser.

export default function Queue() {
  const { data, isError, isLoading } = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => fetchHealth(signal),
    refetchInterval: 10_000,
    retry: false,
  });

  let statusText: string;
  if (isLoading) {
    statusText = "backend: connecting…";
  } else if (isError || !data) {
    statusText = "backend: unreachable";
  } else {
    statusText = `backend: ${data.status}`;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Queue</h1>
      <p className="text-sm text-muted-foreground">
        The queue view will list all in-flight bugs. For now, this is the
        scaffold connectivity probe.
      </p>
      <div
        data-testid="backend-status"
        className="rounded-md border bg-card p-4 text-sm font-mono"
      >
        {statusText}
      </div>
    </div>
  );
}
