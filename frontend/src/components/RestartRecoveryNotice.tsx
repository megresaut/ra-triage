// Restart-recovery notice (Agent 12).
//
// Info-style banner that appears when the bug's TRIAGE.md contains a
// ``## Backend Restart Recovery`` section. Resume_inflight appends this
// section on every boot that restarted a crashed agent run; the notice
// lets the operator know why the agent appears to have been re-run.
//
// We detect the section by reading the parsed TRIAGE.md sections the
// useTriage hook already fetches, so there's no separate API call.

import type { TriageSections } from "@/lib/api";

interface RestartRecoveryNoticeProps {
  triage: TriageSections | undefined;
}

export default function RestartRecoveryNotice({
  triage,
}: RestartRecoveryNoticeProps) {
  if (!triage) {
    return null;
  }
  const body = triage.sections["Backend Restart Recovery"];
  if (!body || !body.trim()) {
    return null;
  }
  return (
    <div
      role="status"
      className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm"
    >
      <div className="font-semibold text-sky-900">Backend restart recovery</div>
      <div className="mt-1 whitespace-pre-wrap text-sky-800">{body}</div>
    </div>
  );
}
