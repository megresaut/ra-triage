import { useNavigate } from "react-router-dom";

import StatusBadge from "@/components/StatusBadge";
import { TableCell, TableRow } from "@/components/ui/table";
import type { Bug } from "@/lib/api";
import { relativeTimeFrom } from "@/lib/time";

// One row in the queue table. Clicking navigates to the bug's detail page.

interface BugRowProps {
  bug: Bug;
}

function conflictLabel(bug: Bug): string {
  const files = bug.conflict_files;
  if (!files || files.length === 0) {
    return "—";
  }
  return files.length === 1 ? "1 file" : `${files.length} files`;
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
      <TableCell className="text-sm text-muted-foreground">
        {conflictLabel(bug)}
      </TableCell>
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
