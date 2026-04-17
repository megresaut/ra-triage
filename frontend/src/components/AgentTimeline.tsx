// Vertical per-agent-run timeline for BugDetail. Fetches agent runs via
// ``useAgentRuns``; renders each row with role, attempt, start+end times,
// duration, exit code, and completion token.
//
// Clicking a row expands it inline to show:
//   - a link to GET /bugs/:id/prompt (opens in a new tab)
//   - a ``LogStream`` drawer if this is the most recent run.

import { useState } from "react";

import LogStream from "@/components/LogStream";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAgentRuns } from "@/hooks/useBugDetail";
import { promptUrl, type AgentRun } from "@/lib/api";
import { cn } from "@/lib/utils";

function tokenClass(token: string | null | undefined): string {
  if (!token) {
    return "bg-slate-200 text-slate-700";
  }
  if (token.endsWith("_COMPLETE")) {
    return "bg-green-100 text-green-800";
  }
  if (token.endsWith("_FAILED")) {
    return "bg-red-100 text-red-800";
  }
  return "bg-slate-200 text-slate-700";
}

function formatDuration(
  started: string | null,
  ended: string | null,
): string {
  if (!started) {
    return "—";
  }
  const start = new Date(started).getTime();
  const end = ended ? new Date(ended).getTime() : Date.now();
  const ms = Math.max(0, end - start);
  if (ms < 1000) {
    return `${ms}ms`;
  }
  const s = Math.floor(ms / 1000);
  if (s < 60) {
    return `${s}s`;
  }
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

function formatTime(when: string | null | undefined): string {
  if (!when) {
    return "—";
  }
  return new Date(when).toLocaleTimeString();
}

function Row({
  run,
  bugId,
  expanded,
  onToggle,
  isLatest,
}: {
  run: AgentRun;
  bugId: number;
  expanded: boolean;
  onToggle: () => void;
  isLatest: boolean;
}) {
  const tokenLabel = run.completion_token ?? "running";
  return (
    <div className="border rounded">
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-left px-3 py-2 flex items-center gap-3 text-sm hover:bg-slate-50"
      >
        <span className="font-mono text-xs text-muted-foreground w-28">
          {run.agent_role}#{run.attempt}
        </span>
        <span className="font-mono text-xs text-muted-foreground w-24">
          {formatTime(run.started_at)}
        </span>
        <span className="font-mono text-xs text-muted-foreground w-20">
          {formatDuration(run.started_at, run.ended_at)}
        </span>
        <span
          className={cn(
            "rounded px-2 py-0.5 text-xs font-mono",
            tokenClass(run.completion_token),
          )}
        >
          {tokenLabel}
        </span>
        <span className="ml-auto text-xs text-muted-foreground">
          exit {run.exit_code ?? "—"}
        </span>
      </button>
      {expanded && (
        <div className="border-t px-3 py-2 space-y-2">
          <div className="text-xs">
            <a
              href={promptUrl(bugId, run.agent_role, run.attempt)}
              target="_blank"
              rel="noreferrer noopener"
              className="text-blue-700 underline"
            >
              view rendered prompt
            </a>{" "}
            <span className="text-muted-foreground">
              · log: {run.log_path}
            </span>
          </div>
          {isLatest && <LogStream bugId={bugId} />}
        </div>
      )}
    </div>
  );
}

export interface AgentTimelineProps {
  bugId: number | undefined;
}

export default function AgentTimeline({ bugId }: AgentTimelineProps) {
  const { data: runs, isLoading, isError } = useAgentRuns(bugId);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Agent timeline</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {isLoading && (
          <p className="text-sm text-muted-foreground">Loading runs…</p>
        )}
        {isError && (
          <p className="text-sm text-red-700">Failed to load agent runs.</p>
        )}
        {runs !== undefined && runs.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No agent runs yet — the pipeline hasn't started any.
          </p>
        )}
        {runs !== undefined && runs.length > 0 && bugId !== undefined && (
          <div className="space-y-2">
            {runs.map((run, idx) => (
              <Row
                key={run.id}
                run={run}
                bugId={bugId}
                expanded={expandedId === run.id}
                onToggle={() =>
                  setExpandedId((cur) => (cur === run.id ? null : run.id))
                }
                isLatest={idx === runs.length - 1}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
