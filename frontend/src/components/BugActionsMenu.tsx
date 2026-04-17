// Kebab-menu on the BugDetail header for "manual" actions that aren't
// confirm/reject: retry the last agent, cancel the bug, take over
// manually. Appears on every bug detail row except terminal states
// (cancel/take-over don't work there; retry needs specific eligibility).

import { MoreVertical } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  ApiError,
  cancelBug,
  retryBug,
  takeOverBug,
  type Bug,
} from "@/lib/api";

type ActionKind = "retry" | "cancel" | "take-over";

type ActionStatus =
  | { kind: "idle" }
  | { kind: "running"; action: ActionKind }
  | { kind: "done"; message: string }
  | { kind: "error"; message: string };

interface BugActionsMenuProps {
  bug: Bug;
}

const TERMINAL_STATES = new Set([
  "FIXED",
  "WONT_FIX",
  "CANCELLED",
]);

const RETRY_ELIGIBLE_STATES = new Set([
  "INVESTIGATING",
  "DESIGNING",
  "FIXING",
  "FIXING_PARALLEL",
  "TESTING",
  "INTEGRATION_TESTING",
  "BLOCKED",
]);

export default function BugActionsMenu({
  bug,
}: BugActionsMenuProps): JSX.Element {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<ActionStatus>({ kind: "idle" });
  const [pendingAction, setPendingAction] = useState<ActionKind | null>(null);
  const [open, setOpen] = useState(false);

  const isTerminal = TERMINAL_STATES.has(bug.state);
  const canRetry = RETRY_ELIGIBLE_STATES.has(bug.state);

  const confirmAction = async (): Promise<void> => {
    if (pendingAction === null) {
      return;
    }
    const action = pendingAction;
    setStatus({ kind: "running", action });
    setPendingAction(null);
    try {
      if (action === "retry") {
        const res = await retryBug(bug.id);
        setStatus({
          kind: "done",
          message: `Retry scheduled: ${res.agent_role} (attempt ${res.attempt}).`,
        });
      } else if (action === "cancel") {
        await cancelBug(bug.id);
        setStatus({ kind: "done", message: "Bug cancelled." });
      } else {
        await takeOverBug(bug.id);
        setStatus({
          kind: "done",
          message: "Marked as taken over. Bug is BLOCKED; worktree retained.",
        });
      }
      await queryClient.invalidateQueries({ queryKey: ["bug", bug.id] });
    } catch (err) {
      setStatus({ kind: "error", message: formatError(err) });
    }
  };

  return (
    <>
      <div className="flex items-center gap-2">
        {status.kind === "running" && (
          <span className="text-xs text-muted-foreground">
            {actionLabel(status.action)}…
          </span>
        )}
        {status.kind === "done" && (
          <span className="text-xs text-muted-foreground">
            {status.message}
          </span>
        )}
        {status.kind === "error" && (
          <span className="text-xs text-red-700">{status.message}</span>
        )}
        <DropdownMenu open={open} onOpenChange={setOpen}>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Bug actions"
              disabled={status.kind === "running"}
            >
              <MoreVertical className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              disabled={!canRetry}
              onSelect={() => setPendingAction("retry")}
            >
              Retry last agent
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              disabled={isTerminal}
              onSelect={() => setPendingAction("take-over")}
            >
              Take over manually
            </DropdownMenuItem>
            <DropdownMenuItem
              destructive
              disabled={isTerminal}
              onSelect={() => setPendingAction("cancel")}
            >
              Cancel bug
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <Dialog
        open={pendingAction !== null}
        onOpenChange={(v) => {
          if (!v) {
            setPendingAction(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{confirmTitle(pendingAction)}</DialogTitle>
            <DialogDescription>
              {confirmDescription(pendingAction, bug)}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setPendingAction(null)}
            >
              Cancel
            </Button>
            <Button
              variant={
                pendingAction === "cancel" ? "destructive" : "default"
              }
              onClick={confirmAction}
            >
              {confirmButtonLabel(pendingAction)}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function actionLabel(action: ActionKind): string {
  switch (action) {
    case "retry":
      return "Retrying";
    case "cancel":
      return "Cancelling";
    case "take-over":
      return "Taking over";
  }
}

function confirmTitle(action: ActionKind | null): string {
  switch (action) {
    case "retry":
      return "Retry last agent?";
    case "cancel":
      return "Cancel this bug?";
    case "take-over":
      return "Take over manually?";
    default:
      return "";
  }
}

function confirmDescription(
  action: ActionKind | null,
  bug: Bug,
): string {
  switch (action) {
    case "retry":
      return `Re-run the most recent agent (incremented attempt) for bug #${bug.id}.`;
    case "cancel":
      return `Cancel bug #${bug.id}. The worktree will be cleaned up and the Notion page will flip to "Won't Fix".`;
    case "take-over":
      return `Mark bug #${bug.id} as taken over manually. The bug will transition to BLOCKED; the worktree is retained so you can work in it.`;
    default:
      return "";
  }
}

function confirmButtonLabel(action: ActionKind | null): string {
  switch (action) {
    case "retry":
      return "Retry";
    case "cancel":
      return "Cancel bug";
    case "take-over":
      return "Take over";
    default:
      return "Confirm";
  }
}

function formatError(err: unknown): string {
  if (err instanceof ApiError) {
    return `${err.status}: ${err.detail || err.message}`;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return "Unknown error";
}
