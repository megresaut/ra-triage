// WebSocket hook for /ws/test-instance.
//
// Subscribes to the single-instance log + status stream. Stores log lines
// keyed by repo so the TestInstancePanel can render a tab per repo without
// re-splitting the stream on every render. Status frames update a small
// ``status`` record alongside the lines.
//
// Reconnect: exponential backoff, capped at 30s. Cleared on unmount.
// Line cap: 2000 per repo (oldest first dropped).

import { useCallback, useEffect, useRef, useState } from "react";

import { testInstanceWsUrl } from "@/lib/api";

export interface TestInstanceStatusFrame {
  type: "test_instance_status";
  bug_id?: number;
  status: "starting" | "running" | "stopping" | "stopped" | "crashed" | "none";
  ports?: Record<string, number>;
  pids?: Record<string, number>;
  db_snapshot_hash?: string | null;
  launched_at?: string | null;
  stopped_at?: string | null;
  browser_port?: number | null;
  row_id?: number;
  // Agent 12: populated on crashed frames — reason + last 20 lines
  // of each proc's log file so the panel can show the immediate
  // diagnostic without making the operator chase the log path.
  reason?: string;
  log_tails?: Record<string, string[]>;
  dead_procs?: string[];
}

interface TestLogFrame {
  type: "test_log";
  bug_id: number;
  repo: string;
  line: string;
  backfill?: boolean;
}

export interface UseTestInstanceLogsResult {
  /** Most recent status frame received (or null if nothing yet). */
  status: TestInstanceStatusFrame | null;
  /** Log lines keyed by repo name (oldest first). */
  linesByRepo: Record<string, string[]>;
  /** True while the WS is open. */
  connected: boolean;
  /** Clear all buffered lines. */
  clear: () => void;
}

const MAX_LINES_PER_REPO = 2000;

export function useTestInstanceLogs(): UseTestInstanceLogsResult {
  const [status, setStatus] = useState<TestInstanceStatusFrame | null>(null);
  const [linesByRepo, setLinesByRepo] = useState<Record<string, string[]>>({});
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clear = useCallback(() => setLinesByRepo({}), []);

  useEffect(() => {
    let cancelled = false;

    function clearReconnect() {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    }

    function connect() {
      clearReconnect();
      if (cancelled) {
        return;
      }
      const ws = new WebSocket(testInstanceWsUrl());
      wsRef.current = ws;

      ws.addEventListener("open", () => {
        setConnected(true);
        retryRef.current = 0;
        // Fresh connection = fresh backfill. Reset the buffer so we don't
        // show duplicated lines from the previous session.
        setLinesByRepo({});
      });

      ws.addEventListener("message", (event) => {
        let parsed: TestLogFrame | TestInstanceStatusFrame | { type: string };
        try {
          parsed = JSON.parse(event.data as string);
        } catch {
          return;
        }
        if (parsed.type === "test_log") {
          const frame = parsed as TestLogFrame;
          setLinesByRepo((prev) => {
            const existing = prev[frame.repo] ?? [];
            const next =
              existing.length >= MAX_LINES_PER_REPO
                ? [
                    ...existing.slice(existing.length - MAX_LINES_PER_REPO + 1),
                    frame.line,
                  ]
                : [...existing, frame.line];
            return { ...prev, [frame.repo]: next };
          });
          return;
        }
        if (parsed.type === "test_instance_status") {
          setStatus(parsed as TestInstanceStatusFrame);
          return;
        }
        // "hello" and any unknown frames are ignored.
      });

      ws.addEventListener("close", () => {
        setConnected(false);
        if (cancelled) {
          return;
        }
        const delay = Math.min(1000 * 2 ** retryRef.current, 30_000);
        retryRef.current += 1;
        reconnectTimerRef.current = setTimeout(connect, delay);
      });

      ws.addEventListener("error", () => {
        // Handled by close.
      });
    }

    connect();

    return () => {
      cancelled = true;
      clearReconnect();
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws !== null && ws.readyState <= WebSocket.OPEN) {
        ws.close();
      }
    };
  }, []);

  return { status, linesByRepo, connected, clear };
}
