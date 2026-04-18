// Needs-manual-review banner (Agent 12).
//
// Shown at the top of BugDetail when the resume path flagged the bug
// with an inconsistent state at boot. The banner links the operator to
// TRIAGE.md's ``## Needs Manual Review`` section and offers a dismiss
// button that clears the flag via
// ``POST /bugs/:id/clear-manual-review-flag``.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { clearManualReviewFlag } from "@/lib/api";

interface NeedsManualReviewBannerProps {
  bugId: number;
  needsManualReview: boolean;
}

export default function NeedsManualReviewBanner({
  bugId,
  needsManualReview,
}: NeedsManualReviewBannerProps) {
  const queryClient = useQueryClient();
  const [localDismissed, setLocalDismissed] = useState(false);

  const mutation = useMutation({
    mutationFn: () => clearManualReviewFlag(bugId),
    onSuccess: () => {
      setLocalDismissed(true);
      queryClient.invalidateQueries({ queryKey: ["bug", bugId] });
    },
  });

  if (!needsManualReview || localDismissed) {
    return null;
  }

  return (
    <div
      role="alert"
      className="rounded-md border border-amber-300 bg-amber-50 p-4"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-2">
          <div className="text-sm font-semibold text-amber-900">
            This bug was resumed in an inconsistent state.
          </div>
          <div className="text-sm text-amber-800">
            The backend restart detected a mismatch between the bug's state
            and its agent-runs audit trail. See the{" "}
            <span className="font-mono">## Needs Manual Review</span> section
            in TRIAGE.md below for details, then either click{" "}
            <span className="font-semibold">Retry</span>,{" "}
            <span className="font-semibold">Take Over Manually</span>, or{" "}
            <span className="font-semibold">Cancel</span>.
          </div>
          {mutation.isError && (
            <div className="text-xs text-red-700">
              Failed to dismiss: {mutation.error instanceof Error
                ? mutation.error.message
                : "unknown error"}
            </div>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
        >
          {mutation.isPending ? "Dismissing…" : "Dismiss"}
        </Button>
      </div>
    </div>
  );
}
