// Agent 11 — Conflict Panel.
//
// Renders a yellow-bordered card on BugDetail when the bug's
// ``conflict_files`` is populated (non-null). Lists the conflicting
// files, shows the diff snippet from /bugs/:id/conflict-diff, and
// provides two action buttons:
//
//   - "Retry Fixer with conflict as context" → POST
//     /bugs/:id/retry-fixer-with-conflict.
//   - "Take over manually" → POST /bugs/:id/take-over (Agent 8).
//
// On success the bug query is invalidated so the state change
// (BLOCKED → FIXING for retry; stays BLOCKED for take-over) propagates
// quickly and this panel disappears once conflict_files is cleared.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ApiError,
  fetchConflictDiff,
  retryFixerWithConflict,
  takeOverBug,
  type Bug,
} from "@/lib/api";

interface ConflictPanelProps {
  bug: Bug;
}

type Status =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; message: string }
  | { kind: "done"; message: string };

export default function ConflictPanel({
  bug,
}: ConflictPanelProps): JSX.Element | null {
  const conflictFiles = bug.conflict_files;
  if (!conflictFiles || conflictFiles.length === 0) {
    return null;
  }
  return <ConflictPanelInner bug={bug} conflictFiles={conflictFiles} />;
}

function ConflictPanelInner({
  bug,
  conflictFiles,
}: {
  bug: Bug;
  conflictFiles: string[];
}): JSX.Element {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const { data: diffData, isLoading: diffLoading } = useQuery({
    queryKey: ["bug", bug.id, "conflict-diff"],
    queryFn: ({ signal }) => fetchConflictDiff(bug.id, undefined, signal),
    refetchInterval: false,
    retry: false,
  });

  async function onRetry(): Promise<void> {
    setStatus({ kind: "submitting" });
    try {
      const res = await retryFixerWithConflict(bug.id);
      setStatus({
        kind: "done",
        message: `Retrying fixer (${res.new_state}).`,
      });
      await queryClient.invalidateQueries({ queryKey: ["bug", bug.id] });
    } catch (err) {
      setStatus({ kind: "error", message: extractMessage(err) });
    }
  }

  async function onTakeOver(): Promise<void> {
    setStatus({ kind: "submitting" });
    try {
      await takeOverBug(bug.id);
      setStatus({
        kind: "done",
        message: "Marked as taken-over manually.",
      });
      await queryClient.invalidateQueries({ queryKey: ["bug", bug.id] });
    } catch (err) {
      setStatus({ kind: "error", message: extractMessage(err) });
    }
  }

  const submitting = status.kind === "submitting";

  return (
    <Card
      className="border-2 border-amber-500"
      data-testid="conflict-panel"
    >
      <CardHeader>
        <CardTitle className="text-base text-amber-800">
          Merge conflict
          <span className="ml-2 text-xs font-mono text-muted-foreground">
            {conflictFiles.length} file{conflictFiles.length === 1 ? "" : "s"}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Another bug merged first and the auto-rebase of this bug's
          worktree hit a conflict. The rebase has been aborted; either
          retry the Fixer with the conflict context or take over manually.
        </p>
        <div>
          <p className="text-xs font-semibold uppercase text-amber-800">
            Conflicting files
          </p>
          <ul className="mt-1 ml-1 space-y-0.5 text-sm font-mono">
            {conflictFiles.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
        </div>
        <div>
          <p className="text-xs font-semibold uppercase text-amber-800">
            Diff snippet
          </p>
          <ConflictDiffBlock
            loading={diffLoading}
            diff={diffData?.diff ?? null}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button
            variant="default"
            disabled={submitting}
            onClick={onRetry}
            data-testid="retry-fixer-with-conflict"
          >
            Retry Fixer with conflict as context
          </Button>
          <Button
            variant="outline"
            disabled={submitting}
            onClick={onTakeOver}
            data-testid="take-over-conflict"
          >
            Take over manually
          </Button>
          {status.kind === "submitting" && (
            <span className="text-sm text-muted-foreground">Working…</span>
          )}
          {status.kind === "error" && (
            <span className="text-sm text-red-700">{status.message}</span>
          )}
          {status.kind === "done" && (
            <span className="text-sm text-emerald-700">{status.message}</span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function ConflictDiffBlock({
  loading,
  diff,
}: {
  loading: boolean;
  diff: string | null;
}): JSX.Element {
  if (loading) {
    return (
      <p className="mt-1 text-sm text-muted-foreground">Loading diff…</p>
    );
  }
  if (!diff || diff.trim().length === 0) {
    return (
      <p className="mt-1 text-sm text-muted-foreground">
        (no diff snapshot available)
      </p>
    );
  }
  return (
    <pre className="mt-1 max-h-[300px] overflow-auto rounded border border-amber-200 bg-amber-50 p-2 text-xs">
      {diff}
    </pre>
  );
}

function extractMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return err.detail || err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return "Unknown error";
}
