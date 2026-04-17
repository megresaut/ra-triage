import { useParams } from "react-router-dom";

import StatusBadge from "@/components/StatusBadge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useBugDetail } from "@/hooks/useBugDetail";
import type { Bug } from "@/lib/api";

// Bug detail view — header + raw fields. Later agents layer richer panels
// (TRIAGE.md, agent timeline, diff viewer, confirm/reject, escalation brief)
// in place of the labeled placeholders below.

function PlaceholderCard({ title, owner }: { title: string; owner: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground">
        Placeholder — {owner} wires this up.
      </CardContent>
    </Card>
  );
}

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
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm text-muted-foreground">
            #{bug.id}
          </span>
          <h1 className="text-2xl font-semibold tracking-tight">{bug.title}</h1>
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

      <DescriptionCard bug={bug} />
      <RawFieldsCard bug={bug} />

      <PlaceholderCard title="Agent timeline (Agent 5)" owner="Agent 5" />
      <PlaceholderCard title="TRIAGE.md (Agent 5)" owner="Agent 5" />
      <PlaceholderCard title="Diff viewer (Agent 6)" owner="Agent 6" />
      <PlaceholderCard title="Test instance (Agent 7)" owner="Agent 7" />
      <PlaceholderCard
        title="Confirm / Reject (Agent 8)"
        owner="Agent 8"
      />
      <PlaceholderCard
        title="Escalation Brief (Agent 9)"
        owner="Agent 9"
      />
    </div>
  );
}
