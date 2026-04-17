import { Badge } from "@/components/ui/badge";
import type { BugState } from "@/lib/api";
import { cn } from "@/lib/utils";

// Colour mapping per Agent 2 orchestrator spec. Values are literal Tailwind
// class strings so Tailwind's JIT can statically extract them.
const STATE_CLASSES: Record<BugState, string> = {
  OPEN: "bg-slate-200 text-slate-800 border-slate-300 hover:bg-slate-200",
  TRIAGED: "bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-100",
  INVESTIGATING: "bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-100",
  DESIGNING: "bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-100",
  FIXING: "bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-100",
  FIXING_PARALLEL:
    "bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-100",
  TESTING: "bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-100",
  INTEGRATION_TESTING:
    "bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-100",
  READY_FOR_REVIEW:
    "bg-amber-100 text-amber-800 border-amber-200 hover:bg-amber-100",
  BLOCKED: "bg-red-100 text-red-800 border-red-200 hover:bg-red-100",
  MERGING: "bg-purple-100 text-purple-800 border-purple-200 hover:bg-purple-100",
  FIXED: "bg-green-100 text-green-800 border-green-200 hover:bg-green-100",
  WONT_FIX: "bg-slate-200 text-slate-700 border-slate-300 hover:bg-slate-200",
  CANCELLED:
    "bg-slate-200 text-slate-700 border-slate-300 hover:bg-slate-200",
};

// Display labels keep the value otherwise — just humanize underscores.
function humanize(state: BugState): string {
  return state.replace(/_/g, " ").toLowerCase();
}

interface StatusBadgeProps {
  state: BugState | string;
  className?: string;
}

function isKnownState(s: string): s is BugState {
  return s in STATE_CLASSES;
}

export default function StatusBadge({ state, className }: StatusBadgeProps) {
  const normalized = isKnownState(state) ? state : "OPEN";
  return (
    <Badge
      variant="outline"
      className={cn(
        "font-medium capitalize border",
        STATE_CLASSES[normalized],
        className,
      )}
    >
      {humanize(normalized)}
    </Badge>
  );
}
