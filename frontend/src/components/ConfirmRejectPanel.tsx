// Confirm / Reject panel — visible only on READY_FOR_REVIEW.
//
// - Confirm (primary green): POST /bugs/:id/confirm → `merging_started`.
// - Reject (outline red): opens a Dialog with a textarea; min 5 / max 500
//   chars; POST /bugs/:id/reject. We surface the resulting status
//   (reinvestigating vs blocked-at-3) in the inline status line so
//   Megha sees immediate feedback without hunting for a toast.
//
// After a Confirm click we flip the local state to `busy` and stop
// offering the buttons until the parent's bug state changes away from
// READY_FOR_REVIEW (TanStack Query's 10s refetch + WS state events pick
// up the transition).

import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, confirmBug, rejectBug, type Bug } from "@/lib/api";

type Status =
  | { kind: "idle" }
  | { kind: "confirming" }
  | { kind: "rejecting" }
  | {
      kind: "done";
      message: string;
      tone: "success" | "info" | "warning";
    }
  | { kind: "error"; message: string };

interface ConfirmRejectPanelProps {
  bug: Bug;
}

export default function ConfirmRejectPanel({
  bug,
}: ConfirmRejectPanelProps): JSX.Element | null {
  if (bug.state !== "READY_FOR_REVIEW") {
    return null;
  }
  return <Panel bug={bug} />;
}

function Panel({ bug }: { bug: Bug }): JSX.Element {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reason, setReason] = useState("");

  const busy =
    status.kind === "confirming" || status.kind === "rejecting";

  const onConfirm = async (): Promise<void> => {
    setStatus({ kind: "confirming" });
    try {
      await confirmBug(bug.id);
      setStatus({
        kind: "done",
        message: "Merging… watch the state timeline for FIXED.",
        tone: "info",
      });
      // Invalidate the bug detail so the state transitions pick up
      // quickly; the refetch-interval will do the rest.
      await queryClient.invalidateQueries({ queryKey: ["bug", bug.id] });
    } catch (err) {
      setStatus({ kind: "error", message: formatError(err) });
    }
  };

  const onReject = async (): Promise<void> => {
    const trimmed = reason.trim();
    if (trimmed.length < 5) {
      setStatus({
        kind: "error",
        message: "Please provide a reason (min 5 characters).",
      });
      return;
    }
    setStatus({ kind: "rejecting" });
    try {
      const response = await rejectBug(bug.id, trimmed);
      const blocked = response.status === "blocked";
      setStatus({
        kind: "done",
        message: blocked
          ? `Rejected — bug is blocked (${response.rejection_count}/3).`
          : `Rejected — re-investigating (reject ${response.rejection_count}/3).`,
        tone: blocked ? "warning" : "info",
      });
      setDialogOpen(false);
      setReason("");
      await queryClient.invalidateQueries({ queryKey: ["bug", bug.id] });
    } catch (err) {
      setStatus({ kind: "error", message: formatError(err) });
    }
  };

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Confirm / Reject</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            This fix is ready for review. Confirm to merge the PR, or reject
            with a reason to send the bug back to the Investigator.
          </p>
          <div className="flex gap-2">
            <Button
              variant="default"
              className="bg-green-600 hover:bg-green-700"
              disabled={busy}
              onClick={onConfirm}
            >
              Confirm
            </Button>
            <Button
              variant="outline"
              className="border-red-600 text-red-700 hover:bg-red-50"
              disabled={busy}
              onClick={() => {
                setStatus({ kind: "idle" });
                setDialogOpen(true);
              }}
            >
              Reject
            </Button>
          </div>
          {status.kind === "confirming" && (
            <p className="text-sm text-muted-foreground">Starting merge…</p>
          )}
          {status.kind === "rejecting" && (
            <p className="text-sm text-muted-foreground">Submitting reject…</p>
          )}
          {status.kind === "done" && (
            <p
              className={`text-sm ${
                status.tone === "warning"
                  ? "text-amber-700"
                  : status.tone === "success"
                    ? "text-green-700"
                    : "text-muted-foreground"
              }`}
            >
              {status.message}
            </p>
          )}
          {status.kind === "error" && (
            <p className="text-sm text-red-700">{status.message}</p>
          )}
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reject fix for Bug #{bug.id}</DialogTitle>
            <DialogDescription>
              Explain what's wrong. The Investigator will see this feedback
              and re-analyze. Three rejections will pin the bug to BLOCKED.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="The fix broke autosave. Check the validator, not the autosave path."
            maxLength={500}
            rows={5}
          />
          <div className="text-xs text-muted-foreground">
            {reason.trim().length} / 500 (min 5)
          </div>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setDialogOpen(false)}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={onReject}
              disabled={busy || reason.trim().length < 5}
            >
              Reject
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
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
