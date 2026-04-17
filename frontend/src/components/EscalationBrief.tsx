// Prominent red-bordered card rendering the Escalation Brief with action
// buttons per the Investigator's proposed options (Agent 9).
//
// Fetches GET /bugs/:id/escalation. Renders nothing when:
//   - the bug isn't BLOCKED, OR
//   - the backend has no unresolved escalation row (404 → null).
//
// Action buttons:
//   - "Approve Recommended" (green primary) → POST /escalation/approve.
//   - "Pick Alternative N" for each alternative the Investigator enumerated
//     → opens a dialog with a notes textarea (optional, 500 char cap).
//   - "Take Over Manually" → confirm dialog → POST /escalation/take-over.
//
// All buttons disable while any POST is in flight. On success the bug query
// is invalidated so the state change (BLOCKED → FIXING) propagates quickly.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  approveEscalation,
  fetchEscalation,
  pickAlternative,
  takeOverEscalation,
  type EscalationDetail,
  type ParsedBriefOption,
} from "@/lib/api";

// --- Props / types --------------------------------------------------------

interface EscalationBriefProps {
  bugId: number;
  /** Current bug state — we render nothing unless BLOCKED. */
  state: string;
}

type Status =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; message: string }
  | { kind: "done"; message: string };

// --- Root component -------------------------------------------------------

export default function EscalationBrief({
  bugId,
  state,
}: EscalationBriefProps): JSX.Element | null {
  const enabled = state === "BLOCKED";
  const { data, isLoading } = useQuery({
    queryKey: ["bug", bugId, "escalation"],
    queryFn: ({ signal }) => fetchEscalation(bugId, signal),
    enabled,
    refetchInterval: 15_000,
    retry: false,
  });

  if (!enabled) {
    return null;
  }
  if (isLoading) {
    return <LoadingCard />;
  }
  // 404 → data === null; render nothing so BLOCKED bugs without a brief
  // (e.g. take-over path) don't show this panel.
  if (!data) {
    return null;
  }
  return <ResolvedPanel bugId={bugId} escalation={data} />;
}

// --- Inner panel (once data is loaded) ------------------------------------

function ResolvedPanel({
  bugId,
  escalation,
}: {
  bugId: number;
  escalation: EscalationDetail;
}): JSX.Element {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [altDialog, setAltDialog] = useState<{
    option: ParsedBriefOption;
    notes: string;
  } | null>(null);
  const [takeOverOpen, setTakeOverOpen] = useState(false);

  const busy = status.kind === "submitting";

  const invalidateBug = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: ["bug", bugId] });
    await queryClient.invalidateQueries({
      queryKey: ["bug", bugId, "escalation"],
    });
  };

  const onApprove = async (): Promise<void> => {
    setStatus({ kind: "submitting" });
    try {
      await approveEscalation(bugId);
      setStatus({
        kind: "done",
        message: "Approved — Fixer will re-run with the approved approach.",
      });
      await invalidateBug();
    } catch (err) {
      setStatus({ kind: "error", message: formatError(err) });
    }
  };

  const onPickAlternative = async (): Promise<void> => {
    if (altDialog === null) {
      return;
    }
    const { option, notes } = altDialog;
    setStatus({ kind: "submitting" });
    try {
      await pickAlternative(bugId, option.id, notes);
      setStatus({
        kind: "done",
        message: `Picked ${option.label} — Fixer will run with the alternative.`,
      });
      setAltDialog(null);
      await invalidateBug();
    } catch (err) {
      setStatus({ kind: "error", message: formatError(err) });
    }
  };

  const onTakeOver = async (): Promise<void> => {
    setStatus({ kind: "submitting" });
    try {
      await takeOverEscalation(bugId);
      setStatus({
        kind: "done",
        message:
          "Taken over manually — bug stays BLOCKED; worktree retained.",
      });
      setTakeOverOpen(false);
      await invalidateBug();
    } catch (err) {
      setStatus({ kind: "error", message: formatError(err) });
    }
  };

  const { parsed } = escalation;
  const alternativeOptions = parsed.options.filter(
    (o) => o.kind === "alternative",
  );

  return (
    <>
      <Card className="border-2 border-red-500 bg-red-50/60">
        <CardHeader>
          <div className="flex items-center gap-3">
            <span className="inline-flex items-center rounded-md bg-red-600 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-white">
              Escalation
            </span>
            <CardTitle className="text-base">Escalation Brief</CardTitle>
            {escalation.triggered_rule && (
              <span className="text-xs text-muted-foreground">
                ({escalation.triggered_rule})
              </span>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {parsed.tldr && (
            <div className="rounded-md border border-red-200 bg-white p-3">
              <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-red-700">
                TL;DR
              </div>
              <div className="text-base font-semibold leading-snug">
                {parsed.tldr}
              </div>
            </div>
          )}
          <div className="prose prose-sm max-w-none rounded-md bg-white p-3">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {parsed.raw}
            </ReactMarkdown>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button
              variant="default"
              className="bg-green-600 hover:bg-green-700"
              disabled={busy}
              onClick={onApprove}
            >
              Approve Recommended
            </Button>
            {alternativeOptions.map((opt) => (
              <Button
                key={opt.id}
                variant="outline"
                disabled={busy}
                onClick={() =>
                  setAltDialog({ option: opt, notes: "" })
                }
              >
                {opt.label}
              </Button>
            ))}
            <Button
              variant="outline"
              className="border-red-600 text-red-700 hover:bg-red-50"
              disabled={busy}
              onClick={() => setTakeOverOpen(true)}
            >
              Take Over Manually
            </Button>
          </div>

          {status.kind === "submitting" && (
            <p className="text-sm text-muted-foreground">Submitting…</p>
          )}
          {status.kind === "done" && (
            <p className="text-sm text-green-700">{status.message}</p>
          )}
          {status.kind === "error" && (
            <p className="text-sm text-red-700">{status.message}</p>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={altDialog !== null}
        onOpenChange={(open) => {
          if (!open) {
            setAltDialog(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {altDialog?.option.label ?? "Pick Alternative"}
            </DialogTitle>
            <DialogDescription>
              Optional: add notes the Fixer should consider while
              implementing this alternative (max 500 characters).
            </DialogDescription>
          </DialogHeader>
          {altDialog && (
            <>
              {parsed.ranked_alternatives[
                Number.parseInt(
                  altDialog.option.id.replace("alternative_", ""),
                  10,
                ) - 1
              ] && (
                <div className="rounded-md border bg-muted p-3 text-sm">
                  <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    Alternative summary
                  </div>
                  <div className="whitespace-pre-wrap">
                    {
                      parsed.ranked_alternatives[
                        Number.parseInt(
                          altDialog.option.id.replace(
                            "alternative_",
                            "",
                          ),
                          10,
                        ) - 1
                      ]
                    }
                  </div>
                </div>
              )}
              <Textarea
                value={altDialog.notes}
                onChange={(e) =>
                  setAltDialog({ ...altDialog, notes: e.target.value })
                }
                placeholder="(optional) The Fixer should also watch for the foo edge case…"
                maxLength={500}
                rows={4}
              />
              <div className="text-xs text-muted-foreground">
                {altDialog.notes.length} / 500
              </div>
            </>
          )}
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setAltDialog(null)}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button onClick={onPickAlternative} disabled={busy}>
              Confirm
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={takeOverOpen} onOpenChange={setTakeOverOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Take over manually?</DialogTitle>
            <DialogDescription>
              The bug stays BLOCKED and the worktree is retained so you can
              finish the fix out of band. The escalation row is marked
              resolved.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setTakeOverOpen(false)}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={onTakeOver}
              disabled={busy}
            >
              Take Over
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

// --- Helpers --------------------------------------------------------------

function LoadingCard(): JSX.Element {
  return (
    <Card className="border-2 border-red-200 bg-red-50/30">
      <CardHeader>
        <CardTitle className="text-base">Escalation Brief</CardTitle>
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground">
        Loading escalation…
      </CardContent>
    </Card>
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
