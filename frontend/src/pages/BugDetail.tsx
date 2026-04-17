import { useParams } from "react-router-dom";

// TODO(Agent 2): render the bug's fields. Later agents layer TRIAGE.md
// rendering, the agent timeline, the diff viewer, the Launch-for-Testing
// button, the Confirm/Reject panel, and the Escalation Brief.

export default function BugDetail() {
  const { id } = useParams<{ id: string }>();
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">
        Bug #{id ?? "?"}
      </h1>
      <p className="text-sm text-muted-foreground">
        Detail view stub — Agent 2 will wire this up.
      </p>
    </div>
  );
}
