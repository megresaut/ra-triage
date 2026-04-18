import { useParams } from "react-router-dom";

import AgentTimeline from "@/components/AgentTimeline";
import BugActionsMenu from "@/components/BugActionsMenu";
import ConfirmRejectPanel from "@/components/ConfirmRejectPanel";
import ConflictPanel from "@/components/ConflictPanel";
import DiffViewer from "@/components/DiffViewer";
import EscalationBrief from "@/components/EscalationBrief";
import StatusBadge from "@/components/StatusBadge";
import TestInstancePanel from "@/components/TestInstancePanel";
import TriageMarkdown from "@/components/TriageMarkdown";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useBugDetail } from "@/hooks/useBugDetail";
import { useTriage } from "@/hooks/useTriage";
import type { Bug, PullRequest } from "@/lib/api";

// Bug detail view — header + real TRIAGE.md + agent timeline (Agent 5)
// + real Fixer diff viewer (Agent 6). Escalation Brief (Agent 9) renders
// prominently at the top of the detail view when the bug is BLOCKED with
// an unresolved escalation row.

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[140px_1fr] gap-3 py-1 text-sm">
      <div className="text-muted-foreground">{label}</div>
      <div className="font-mono text-xs">{value}</div>
    </div>
  );
}

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  return new Date(value).toLocaleString();
}

function RawFieldsCard({ bug }: { bug: Bug }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Raw fields</CardTitle>
      </CardHeader>
      <CardContent>
        <Field label="Notion page" value={bug.notion_page_id} />
        <Field label="Project" value={bug.project_id} />
        <Field label="Scope" value={bug.scope ?? "—"} />
        <Field
          label="Affected repos"
          value={
            bug.affected_repos && bug.affected_repos.length > 0
              ? bug.affected_repos.join(", ")
              : "—"
          }
        />
        <Field label="Deny-list hit" value={String(bug.deny_list_hit)} />
        <Field label="Rejection count" value={String(bug.rejection_count)} />
        <Field
          label="Launched for testing"
          value={bug.launched_for_testing ? "yes" : "no"}
        />
        <Field label="Created" value={formatDate(bug.created_at)} />
        <Field label="Updated" value={formatDate(bug.updated_at)} />
        <Field label="Terminated" value={formatDate(bug.terminated_at)} />
      </CardContent>
    </Card>
  );
}

function DescriptionCard({ bug }: { bug: Bug }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Description</CardTitle>
      </CardHeader>
      <CardContent className="whitespace-pre-wrap text-sm">
        {bug.description || <span className="text-muted-foreground">—</span>}
      </CardContent>
    </Card>
  );
}

function TriagePanel({
  bugId,
  state,
}: {
  bugId: number;
  state: string;
}) {
  const { data, isLoading, isError, error } = useTriage(bugId);

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">TRIAGE.md</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Loading…
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    // 404 is common before the pipeline has created worktrees — don't
    // treat it as loud error. Other errors surface red.
    const msg = error instanceof Error ? error.message : "unknown error";
    const is404 = msg.includes("404");
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">TRIAGE.md</CardTitle>
        </CardHeader>
        <CardContent className="text-sm">
          {is404 ? (
            <span className="text-muted-foreground">
              No TRIAGE.md yet — the pipeline hasn't created a worktree.
            </span>
          ) : (
            <span className="text-red-700">Failed to load: {msg}</span>
          )}
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return null;
  }

  return (
    <TriageMarkdown sections={data.sections} raw={data.raw} state={state} />
  );
}

export default function BugDetail() {
  const { id } = useParams<{ id: string }>();
  const numericId = id ? Number.parseInt(id, 10) : undefined;
  const { data: bug, isError, isLoading, error } = useBugDetail(numericId);

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading bug…</p>;
  }

  if (isError || !bug) {
    return (
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          Bug #{id ?? "?"}
        </h1>
        <p className="text-sm text-red-700">
          Failed to load:{" "}
          {error instanceof Error ? error.message : "unknown error"}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2">
        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-3">
              <span className="font-mono text-sm text-muted-foreground">
                #{bug.id}
              </span>
              <h1 className="text-2xl font-semibold tracking-tight">
                {bug.title}
              </h1>
              <StatusBadge state={bug.state} />
            </div>
            <div className="text-sm text-muted-foreground">
              <span>project: {bug.project_id}</span>
              {" · "}
              <span>severity: {bug.severity ?? "—"}</span>
              {" · "}
              <span>reporter: {bug.reporter ?? "—"}</span>
            </div>
          </div>
          <BugActionsMenu bug={bug} />
        </div>
      </div>

      <EscalationBrief bugId={bug.id} state={bug.state} />

      <ConflictPanel bug={bug} />

      <PullRequestsCard pull_requests={bug.pull_requests ?? []} />

      <DescriptionCard bug={bug} />
      <RawFieldsCard bug={bug} />

      <AgentTimeline bugId={bug.id} />
      <TriagePanel bugId={bug.id} state={bug.state} />

      <DiffPanel bug={bug} />
      <TestInstancePanel bugId={bug.id} bugState={bug.state} />
      <ConfirmRejectPanel bug={bug} />
    </div>
  );
}

function PullRequestsCard({
  pull_requests,
}: {
  pull_requests: PullRequest[];
}): JSX.Element | null {
  if (pull_requests.length === 0) {
    return null;
  }
  // For cross-repo merges we want the operator to see which PR merged
  // first (backend is merged before frontend per the Agent 10 flow).
  // The backend marks merged_at on successful merges; we compute a
  // per-PR "merged first" label by sorting merged PRs by merged_at.
  const merged = pull_requests
    .filter((pr) => pr.status === "merged" && pr.merged_at)
    .sort((a, b) => {
      const aT = a.merged_at ? Date.parse(a.merged_at) : 0;
      const bT = b.merged_at ? Date.parse(b.merged_at) : 0;
      return aT - bT;
    });
  const firstMergedId = merged.length > 0 ? merged[0].id : null;
  const isCrossRepo = pull_requests.length > 1;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Pull Requests
          {isCrossRepo && (
            <span className="ml-2 text-xs font-mono text-muted-foreground">
              cross-repo
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        {pull_requests.map((pr) => (
          <div key={pr.id} className="flex items-center gap-2">
            <span className="font-mono text-xs text-muted-foreground">
              {pr.repo_name}
            </span>
            {pr.pr_url ? (
              <a
                href={pr.pr_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-700 underline"
              >
                PR #{pr.pr_number ?? "?"}
              </a>
            ) : (
              <span className="text-muted-foreground">(pending)</span>
            )}
            <span
              className={
                pr.status === "merged"
                  ? "text-green-700"
                  : pr.status === "closed"
                    ? "text-muted-foreground"
                    : "text-amber-700"
              }
            >
              {pr.status}
            </span>
            {isCrossRepo && pr.id === firstMergedId && (
              <span className="text-xs text-green-700 italic">
                (merged first)
              </span>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function DiffPanel({ bug }: { bug: Bug }) {
  const repos = bug.affected_repos ?? [];
  if (repos.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Diff viewer</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No affected repos on this bug yet — the Investigator hasn't run, or
          the scope is indeterminate.
        </CardContent>
      </Card>
    );
  }
  if (repos.length === 1) {
    return <DiffViewer bugId={bug.id} repo={repos[0]} state={bug.state} />;
  }
  // Cross-repo (Agent 10): one tab per repo, each loading its own DiffViewer.
  // The existing DiffViewer card renders fine inside a tab's content. We
  // default the active tab to the first repo in registry order (backend
  // for AVM).
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Cross-repo diff
          <span className="ml-2 text-xs font-mono text-muted-foreground">
            {repos.length} repos
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue={repos[0]} className="w-full">
          <TabsList>
            {repos.map((repoName) => (
              <TabsTrigger key={repoName} value={repoName}>
                {repoName}
              </TabsTrigger>
            ))}
          </TabsList>
          {repos.map((repoName) => (
            <TabsContent key={repoName} value={repoName}>
              <DiffViewer
                bugId={bug.id}
                repo={repoName}
                state={bug.state}
              />
            </TabsContent>
          ))}
        </Tabs>
      </CardContent>
    </Card>
  );
}
