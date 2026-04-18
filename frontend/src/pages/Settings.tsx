// Settings page (Agent 13).
//
// Read-only registry view per project, plus a single mutable control:
// the per-project auto-merge toggle. Wired to GET /settings and
// PUT /settings/projects/:id via the typed helpers in `lib/api.ts`.
//
// **Behavior contract (v1):** the toggle is persisted but does NOT change
// merging behavior — every fix still requires a manual Confirm click. The
// tooltip on the toggle's label spells this out, and a banner at the top
// of the page reminds the reader of the same constraint.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  fetchSettings,
  updateProjectAutoMerge,
  type SettingsProjectView,
  type SettingsResponse,
  type SettingsRepoView,
} from "@/lib/api";

const AUTO_MERGE_TOOLTIP =
  "v1 does not yet auto-merge — even with this enabled, every fix still requires manual Confirm. Setting persisted for v2.";

export default function Settings(): JSX.Element {
  const {
    data,
    isLoading,
    isError,
    error,
  } = useQuery<SettingsResponse, Error>({
    queryKey: ["settings"],
    queryFn: ({ signal }) => fetchSettings(signal),
    // Settings are static — no refetchInterval per the Agent 13 brief.
  });

  return (
    <TooltipProvider delayDuration={150}>
      <div className="space-y-6">
        <div className="space-y-2">
          <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
          <p className="text-sm text-muted-foreground">
            View per-project registry config and toggle the (v2-only) auto-merge
            preference. All other config is read-only — adding a project still
            requires a code change to <code className="font-mono">config/registry.py</code>.
          </p>
        </div>

        <V1NoOpBanner />

        {isLoading && (
          <p className="text-sm text-muted-foreground">Loading settings…</p>
        )}
        {isError && (
          <p className="text-sm text-red-700">
            Failed to load settings: {error?.message ?? "unknown error"}
          </p>
        )}
        {data?.projects.map((project) => (
          <ProjectCard key={project.id} project={project} />
        ))}
        {data && data.projects.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No projects configured.
          </p>
        )}
      </div>
    </TooltipProvider>
  );
}

function V1NoOpBanner(): JSX.Element {
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
      <strong className="font-semibold">Heads up:</strong> the auto-merge toggle
      is persisted but is a no-op in v1. The product plan explicitly forbids
      auto-merging without a human Confirm click. Toggle now to lock in your
      preference for v2.
    </div>
  );
}

interface ProjectCardProps {
  project: SettingsProjectView;
}

function ProjectCard({ project }: ProjectCardProps): JSX.Element {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div className="space-y-1">
            <CardTitle className="text-lg">{project.display_name}</CardTitle>
            <CardDescription>
              <span className="font-mono text-xs">{project.id}</span>
            </CardDescription>
          </div>
          <AutoMergeToggle project={project} />
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        <ReposSection repos={project.repos} />
        <DenyListSection
          paths={project.deny_list.paths}
          categories={project.deny_list.categories}
        />
      </CardContent>
    </Card>
  );
}

interface AutoMergeToggleProps {
  project: SettingsProjectView;
}

function AutoMergeToggle({ project }: AutoMergeToggleProps): JSX.Element {
  const queryClient = useQueryClient();
  const [toast, setToast] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);

  const mutation = useMutation({
    mutationFn: (next: boolean) =>
      updateProjectAutoMerge(project.id, next),
    onSuccess: async (response) => {
      setToast({
        kind: "success",
        message: response.auto_merge
          ? "Auto-merge preference saved (still requires Confirm in v1)."
          : "Auto-merge preference saved.",
      });
      // Re-pull the canonical view rather than trusting our local update.
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      // Auto-clear the toast after a short delay so it doesn't pile up.
      window.setTimeout(() => setToast(null), 4000);
    },
    onError: (err: Error) => {
      setToast({
        kind: "error",
        message: `Save failed: ${err.message}`,
      });
    },
  });

  const overridden = project.auto_merge !== project.auto_merge_default;

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-3">
        <Tooltip>
          <TooltipTrigger asChild>
            <label
              htmlFor={`auto-merge-${project.id}`}
              className="cursor-help text-sm font-medium underline decoration-dotted underline-offset-4"
            >
              Auto-merge
            </label>
          </TooltipTrigger>
          <TooltipContent>{AUTO_MERGE_TOOLTIP}</TooltipContent>
        </Tooltip>
        <Switch
          id={`auto-merge-${project.id}`}
          checked={project.auto_merge}
          disabled={mutation.isPending}
          onCheckedChange={(next) => mutation.mutate(next)}
          aria-label={`Auto-merge for ${project.display_name}`}
        />
      </div>
      <span className="text-xs text-muted-foreground">
        default: {project.auto_merge_default ? "on" : "off"}
        {overridden && (
          <span className="ml-1 italic">(overridden)</span>
        )}
      </span>
      {toast && (
        <span
          role="status"
          className={`text-xs ${
            toast.kind === "success" ? "text-emerald-700" : "text-red-700"
          }`}
        >
          {toast.message}
        </span>
      )}
    </div>
  );
}

interface ReposSectionProps {
  repos: SettingsRepoView[];
}

function ReposSection({ repos }: ReposSectionProps): JSX.Element {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        Repos
      </h3>
      <div className="grid gap-3 md:grid-cols-2">
        {repos.map((repo) => (
          <RepoTile key={repo.name} repo={repo} />
        ))}
      </div>
    </section>
  );
}

function RepoTile({ repo }: { repo: SettingsRepoView }): JSX.Element {
  return (
    <div className="rounded-md border bg-muted/30 p-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="font-mono font-semibold">{repo.name}</span>
        <Badge variant="secondary">{repo.role}</Badge>
      </div>
      <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted-foreground">github</dt>
        <dd className="font-mono break-all">{repo.github_origin}</dd>

        <dt className="text-muted-foreground">main branch</dt>
        <dd className="font-mono">{repo.main_branch}</dd>

        <dt className="text-muted-foreground">branch prefix</dt>
        <dd className="font-mono">{repo.branch_prefix}</dd>

        <dt className="text-muted-foreground">dev port</dt>
        <dd className="font-mono">{repo.dev_port}</dd>

        <dt className="text-muted-foreground">test port</dt>
        <dd className="font-mono">{repo.test_launch_port}</dd>

        <dt className="text-muted-foreground">integration port</dt>
        <dd className="font-mono">{repo.integration_port}</dd>
      </dl>
    </div>
  );
}

interface DenyListSectionProps {
  paths: string[];
  categories: string[];
}

function DenyListSection({
  paths,
  categories,
}: DenyListSectionProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const total = paths.length + categories.length;

  return (
    <section className="space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between rounded-md border bg-muted/30 px-3 py-2 text-left text-sm hover:bg-muted/50"
        aria-expanded={open}
      >
        <span className="font-semibold uppercase tracking-wide text-muted-foreground">
          Deny-list
          <span className="ml-2 text-xs normal-case text-muted-foreground">
            ({total} {total === 1 ? "entry" : "entries"})
          </span>
        </span>
        <span aria-hidden className="font-mono text-xs">
          {open ? "[hide]" : "[show]"}
        </span>
      </button>
      {open && (
        <div className="grid gap-4 rounded-md border p-3 md:grid-cols-2">
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Paths
            </h4>
            {paths.length === 0 ? (
              <p className="text-sm text-muted-foreground">(none)</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {paths.map((p) => (
                  <li key={p} className="font-mono text-xs">
                    {p}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Categories
            </h4>
            {categories.length === 0 ? (
              <p className="text-sm text-muted-foreground">(none)</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {categories.map((c) => (
                  <li key={c} className="font-mono text-xs">
                    {c}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
