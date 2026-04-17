// Test-instance panel on BugDetail.
//
// Shows: launch/stop button, current status, which bug is launched, live
// per-repo logs via /ws/test-instance. If status is "crashed", the panel
// shows a distinct message and the Launch button becomes available again
// (the backend clears the lock automatically on crash).

import { Link } from "react-router-dom";

import LaunchTestButton from "@/components/LaunchTestButton";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useCurrentTestInstance } from "@/hooks/useTestInstance";
import { useTestInstanceLogs } from "@/hooks/useTestInstanceLogs";
import type { TestInstance } from "@/lib/api";

interface TestInstancePanelProps {
  bugId: number;
  bugState: string;
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    running: "bg-emerald-100 text-emerald-800 border-emerald-300",
    starting: "bg-blue-100 text-blue-800 border-blue-300",
    stopping: "bg-amber-100 text-amber-800 border-amber-300",
    stopped: "bg-gray-100 text-gray-700 border-gray-300",
    crashed: "bg-red-100 text-red-800 border-red-300",
    none: "bg-gray-100 text-gray-600 border-gray-300",
  };
  const cls = colors[status] ?? colors.none;
  return (
    <Badge variant="outline" className={cls}>
      {status}
    </Badge>
  );
}

/** Resolve the effective instance snapshot: prefer WS, fall back to poll. */
function useCombinedInstance(): TestInstance | null {
  const { data: polled } = useCurrentTestInstance();
  const { status } = useTestInstanceLogs();

  if (status === null) {
    return polled ?? null;
  }
  if (status.status === "none") {
    return polled ?? null;
  }
  // Merge — WS status is authoritative for state, polled provides fields
  // that may not ride on every status frame.
  const base = polled ?? {
    bug_id: status.bug_id ?? 0,
    status: status.status,
    ports: status.ports ?? {},
    pids: status.pids ?? {},
    db_snapshot_hash: status.db_snapshot_hash ?? null,
    launched_at: status.launched_at ?? "",
    browser_port: status.browser_port ?? null,
  };
  return {
    ...base,
    status: status.status,
    ports: status.ports ?? base.ports,
    pids: status.pids ?? base.pids,
  } as TestInstance;
}

export default function TestInstancePanel({
  bugId,
  bugState,
}: TestInstancePanelProps) {
  const instance = useCombinedInstance();
  const { linesByRepo, connected } = useTestInstanceLogs();

  const isRunningThis =
    instance !== null &&
    instance.bug_id === bugId &&
    (instance.status === "running" || instance.status === "starting");
  const otherBugRunning =
    instance !== null &&
    instance.bug_id !== bugId &&
    (instance.status === "running" || instance.status === "starting");
  const isCrashedThis =
    instance !== null &&
    instance.bug_id === bugId &&
    instance.status === "crashed";

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div className="space-y-1">
          <CardTitle className="text-base">Test instance</CardTitle>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <StatusBadge status={instance?.status ?? "none"} />
            <span>
              WS: {connected ? "connected" : "disconnected"}
            </span>
          </div>
        </div>
        <LaunchTestButton
          bugId={bugId}
          bugState={bugState}
          currentInstance={instance}
        />
      </CardHeader>
      <CardContent className="space-y-3">
        <InstanceSummary bugId={bugId} instance={instance} />
        {isCrashedThis && (
          <p className="text-sm text-red-700">
            Crashed — see logs below. The lock has been released, so Launch
            is available again.
          </p>
        )}
        {otherBugRunning && instance !== null && (
          <p className="text-sm text-muted-foreground">
            Another bug is currently launched:{" "}
            <Link
              to={`/bugs/${instance.bug_id}`}
              className="underline underline-offset-2"
            >
              bug #{instance.bug_id}
            </Link>
            . Launching this one will stop the other first.
          </p>
        )}

        <LogsTabs
          linesByRepo={linesByRepo}
          showEmptyHint={!isRunningThis && !isCrashedThis}
        />
      </CardContent>
    </Card>
  );
}

function InstanceSummary({
  bugId,
  instance,
}: {
  bugId: number;
  instance: TestInstance | null;
}) {
  if (instance === null || instance.status === "none") {
    return (
      <p className="text-sm text-muted-foreground">
        Nothing launched. Press "Launch for Testing" to spin up this bug's
        worktree.
      </p>
    );
  }
  const launchedThis = instance.bug_id === bugId;
  const ports = Object.entries(instance.ports ?? {});
  return (
    <div className="text-sm space-y-1">
      <div className="text-muted-foreground">
        {launchedThis ? (
          <>This bug's worktree is live.</>
        ) : (
          <>
            Bug #{instance.bug_id} is currently launched (not this one).
          </>
        )}
      </div>
      {ports.length > 0 && (
        <div className="font-mono text-xs">
          {ports.map(([repo, port]) => (
            <div key={repo}>
              {repo}:{" "}
              <a
                className="underline underline-offset-2"
                href={`http://127.0.0.1:${port}`}
                target="_blank"
                rel="noreferrer"
              >
                http://127.0.0.1:{port}
              </a>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function LogsTabs({
  linesByRepo,
  showEmptyHint,
}: {
  linesByRepo: Record<string, string[]>;
  showEmptyHint: boolean;
}) {
  const repos = Object.keys(linesByRepo).sort();
  if (repos.length === 0) {
    if (showEmptyHint) {
      return null;
    }
    return (
      <p className="text-sm text-muted-foreground">
        Waiting for log lines…
      </p>
    );
  }
  return (
    <Tabs defaultValue={repos[0]}>
      <TabsList>
        {repos.map((r) => (
          <TabsTrigger key={r} value={r}>
            {r}
          </TabsTrigger>
        ))}
      </TabsList>
      {repos.map((r) => (
        <TabsContent key={r} value={r}>
          <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-slate-950 p-3 text-xs text-slate-100">
            {linesByRepo[r]?.join("\n") ?? ""}
          </pre>
        </TabsContent>
      ))}
    </Tabs>
  );
}
