import { useQuery } from "@tanstack/react-query";

import BugRow from "@/components/BugRow";
import {
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useBugs } from "@/hooks/useBugs";
import { fetchHealth } from "@/lib/api";

// The queue view — non-terminal bugs only, columns match the Agent 2 spec.
// A small "backend: ok/unreachable" indicator lives in the header so the
// Agent 1 health probe stays visible.

function BackendIndicator() {
  const { data, isError, isLoading } = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => fetchHealth(signal),
    refetchInterval: 10_000,
    retry: false,
  });
  let text: string;
  let color: string;
  if (isLoading) {
    text = "backend: connecting…";
    color = "text-muted-foreground";
  } else if (isError || !data) {
    text = "backend: unreachable";
    color = "text-red-700";
  } else {
    text = `backend: ${data.status}`;
    color = "text-emerald-700";
  }
  return (
    <span data-testid="backend-status" className={`text-xs font-mono ${color}`}>
      {text}
    </span>
  );
}

export default function Queue() {
  const { data: bugs, isError, isLoading, error } = useBugs();

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Queue</h1>
        <BackendIndicator />
      </div>

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Title</TableHead>
              <TableHead>Project</TableHead>
              <TableHead>State</TableHead>
              <TableHead>Current agent</TableHead>
              <TableHead>Age</TableHead>
              <TableHead className="text-center">Rej</TableHead>
              <TableHead>Conflict</TableHead>
              <TableHead className="text-center">Testing</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              <TableRow>
                <td
                  colSpan={9}
                  className="p-4 text-center text-sm text-muted-foreground"
                >
                  Loading…
                </td>
              </TableRow>
            )}
            {isError && (
              <TableRow>
                <td
                  colSpan={9}
                  className="p-4 text-center text-sm text-red-700"
                >
                  Failed to load bugs:{" "}
                  {error instanceof Error ? error.message : "unknown error"}
                </td>
              </TableRow>
            )}
            {!isLoading && !isError && bugs && bugs.length === 0 && (
              <TableRow>
                <td
                  colSpan={9}
                  className="p-6 text-center text-sm text-muted-foreground"
                >
                  No bugs in pipeline.
                </td>
              </TableRow>
            )}
            {bugs?.map((bug) => (
              <BugRow key={bug.id} bug={bug} />
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
