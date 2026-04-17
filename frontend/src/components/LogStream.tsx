// Live log stream panel for BugDetail. Subscribes to
// ``/ws/bugs/:id/logs`` via ``useLiveLogs`` and renders lines in a
// monospace ``<pre>`` with debounced auto-scroll to the bottom.

import { useEffect, useRef } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useLiveLogs } from "@/hooks/useLiveLogs";
import { cn } from "@/lib/utils";

export interface LogStreamProps {
  bugId: number | undefined;
  /** Cap on lines kept in the rolling buffer. Default 2000. */
  maxLines?: number;
}

export default function LogStream({ bugId, maxLines = 2000 }: LogStreamProps) {
  const { lines, connected } = useLiveLogs(bugId, { maxLines });
  const preRef = useRef<HTMLPreElement | null>(null);
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounced auto-scroll: when new lines arrive, schedule a scroll to
  // bottom 100ms later — if more lines arrive in that window, the scroll
  // is rescheduled and only runs once.
  useEffect(() => {
    if (scrollTimerRef.current !== null) {
      clearTimeout(scrollTimerRef.current);
    }
    scrollTimerRef.current = setTimeout(() => {
      const node = preRef.current;
      if (node !== null) {
        node.scrollTop = node.scrollHeight;
      }
    }, 100);
    return () => {
      if (scrollTimerRef.current !== null) {
        clearTimeout(scrollTimerRef.current);
        scrollTimerRef.current = null;
      }
    };
  }, [lines]);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="text-base">Live logs</CardTitle>
        <span
          className={cn(
            "text-xs rounded px-2 py-0.5",
            connected
              ? "bg-green-100 text-green-800"
              : "bg-slate-200 text-slate-700",
          )}
        >
          {connected ? "connected" : "disconnected"}
        </span>
      </CardHeader>
      <CardContent>
        <pre
          ref={preRef}
          className="h-72 overflow-auto rounded bg-slate-950 p-3 text-xs text-slate-100 font-mono whitespace-pre-wrap"
        >
          {lines.length === 0 ? (
            <span className="text-slate-400">
              Waiting for agent output…
            </span>
          ) : (
            lines.join("\n")
          )}
        </pre>
      </CardContent>
    </Card>
  );
}
