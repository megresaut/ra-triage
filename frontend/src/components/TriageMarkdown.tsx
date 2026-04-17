// Renders the parsed TRIAGE.md as a set of shadcn Cards — one per section.
// Canonical sections (per the backend's CANONICAL_SECTIONS list) render in
// their declared order; unknown sections render at the bottom under an
// "Additional Notes" header. When the bug is BLOCKED and has a non-empty
// Escalation Brief section, that card renders FIRST and gets a red/amber
// highlight.

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

// Must stay in sync with backend/src/ra_triage/triage_md/schema.py's
// CANONICAL_SECTIONS. Copied here rather than imported because the OpenAPI
// types don't expose it as a value.
const CANONICAL_SECTIONS: readonly string[] = [
  "Root Cause",
  "Scope",
  "Analysis",
  "Recommended Fix Approach",
  "Escalation Brief",
  "Cross-Repo Contract",
  "Fixer Notes",
  "Fixer-Frontend Notes",
  "Fixer-Backend Notes",
  "Fixer-Frontend Contract Concern",
  "Fixer-Backend Contract Concern",
  "Tester Notes",
  "Tester Failure",
  "Integration Tester Notes",
  "Integration Tester Failure",
  "Investigator Failure",
  "Fixer Failure",
  "Fixer Escalation",
] as const;

export interface TriageMarkdownProps {
  sections: Record<string, string>;
  raw: string;
  /** Current bug state — drives Escalation Brief highlighting. */
  state?: string;
}

function SectionCard({
  name,
  body,
  highlight,
}: {
  name: string;
  body: string;
  highlight?: boolean;
}) {
  return (
    <Card
      className={cn(
        highlight && "border-red-300 bg-amber-50",
      )}
    >
      <CardHeader className={cn(highlight && "bg-amber-100 rounded-t-lg")}>
        <CardTitle
          className={cn(
            "text-base",
            highlight && "text-amber-900",
          )}
        >
          {name}
        </CardTitle>
      </CardHeader>
      <CardContent className="prose prose-sm max-w-none">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
      </CardContent>
    </Card>
  );
}

export default function TriageMarkdown({
  sections,
  raw,
  state,
}: TriageMarkdownProps) {
  const sectionNames = Object.keys(sections);

  if (sectionNames.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">TRIAGE.md</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Not yet written — the Investigator hasn't run or has just started.
        </CardContent>
      </Card>
    );
  }

  // Split into canonical + unknown buckets.
  const canonicalOrdered: [string, string][] = [];
  const canonicalSet = new Set(CANONICAL_SECTIONS);
  for (const name of CANONICAL_SECTIONS) {
    if (name in sections) {
      canonicalOrdered.push([name, sections[name]]);
    }
  }
  const unknowns: [string, string][] = sectionNames
    .filter((n) => !canonicalSet.has(n))
    .map((n) => [n, sections[n]]);

  const shouldHighlightEscalation =
    state === "BLOCKED" &&
    typeof sections["Escalation Brief"] === "string" &&
    sections["Escalation Brief"].trim().length > 0;

  // Promote the Escalation Brief to the top when highlighted.
  let displayList: [string, string, boolean][] = canonicalOrdered.map(
    ([n, b]) => [n, b, false],
  );
  if (shouldHighlightEscalation) {
    const idx = displayList.findIndex(([n]) => n === "Escalation Brief");
    if (idx >= 0) {
      const [entry] = displayList.splice(idx, 1);
      displayList = [[entry[0], entry[1], true], ...displayList];
    }
  }

  return (
    <div className="space-y-3">
      <div className="text-xs text-muted-foreground">
        TRIAGE.md · {raw.length.toLocaleString()} bytes
      </div>
      {displayList.map(([name, body, highlight]) => (
        <SectionCard
          key={name}
          name={name}
          body={body}
          highlight={highlight}
        />
      ))}
      {unknowns.length > 0 && (
        <>
          <div className="text-xs text-muted-foreground pt-2">
            Additional Notes
          </div>
          {unknowns.map(([name, body]) => (
            <SectionCard key={name} name={name} body={body} />
          ))}
        </>
      )}
    </div>
  );
}
