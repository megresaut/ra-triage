import { useNavigate } from "react-router-dom";

import StatusBadge from "@/components/StatusBadge";
import { TableCell, TableRow } from "@/components/ui/table";
import type { Bug } from "@/lib/api";
import { relativeTimeFrom } from "@/lib/time";

// One row in the queue table. Clicking navigates to the bug's detail page.

interface BugRowProps {
  bug: Bug;
}

// Returns a small badge node for the conflict column. Agent 11: when
// the rebase sweeper marks a bug BLOCKED with conflict_files, render a
// red dot + the file count so the queue surfaces conflicting bugs at a
// glance. The tooltip carries the full file list so a hover gives the
// reviewer the names without leaving the queue.
function conflictBadge(bug: Bug): JSX.Element {
  const files = bug.conflict_files;
  if (!files || files.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }
  const count = files.length;
  const label = count === 1 ? "1 file" : `${count} files`;
  const tooltip = files.join("\n");
  return (
    <span
      className="inline-flex items-center gap-1 text-red-700"
      title={tooltip}
      aria-label={`Merge conflict on ${label}`}
      data-testid="conflict-badge"
    >
      <span
        className="inline-block h-2 w-2 rounded-full bg-red-600"
        aria-hidden="true"
      />
      {label}
    </span>
  );
}

export default function BugRow({ bug }: BugRowProps) {
  const navigate = useNavigate();
  const onOpen = () => navigate(`/bugs/${bug.id}`);
  return (
    <TableRow
      className="cursor-pointer"
      onClick={onOpen}
      data-testid={`bug-row-${bug.id}`}
    >
      <TableCell className="font-mono text-xs">#{bug.id}</TableCell>
      <TableCell className="max-w-xl truncate">{bug.title}</TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {bug.project_id}
      </TableCell>
      <TableCell>
        <StatusBadge state={bug.state} />
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {bug.current_agent ?? "—"}
      </TableCell>
      <TableCell
        className="text-xs text-muted-foreground"
        title={new Date(bug.created_at).toLocaleString()}
      >
        {relativeTimeFrom(bug.created_at)}
      </TableCell>
      <TableCell className="text-center text-sm">
        {bug.rejection_count}
      </TableCell>
      <TableCell className="text-sm">{conflictBadge(bug)}</TableCell>
      <TableCell className="text-center">
        {bug.launched_for_testing ? (
          <span
            className="inline-block h-2 w-2 rounded-full bg-emerald-500"
            aria-label="launched for testing"
            title="Currently launched for testing"
          />
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>
    </TableRow>
  );
}
