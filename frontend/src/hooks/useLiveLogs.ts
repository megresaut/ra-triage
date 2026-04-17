// Live agent-stdout streaming hook for BugDetail's log panel.
//
// Connects to ``/ws/bugs/:id/logs``; parses incoming JSON frames with
// ``type === "agent_stdout"``; appends each frame's ``text`` to a rolling
// in-memory buffer. Reconnects on close with exponential backoff (1s, 2s,
// 4s, ..., capped at 30s). The buffer is capped at ``maxLines`` to avoid
// unbounded growth — oldest lines are dropped first.
//
// The returned ``lines`` array is stable across renders when no new data
// arrives; callers can render it into a ``<pre>`` without re-flattening.

import { useCallback, useEffect, useRef, useState } from "react";

import { bugLogsWsUrl } from "@/lib/api";

export interface UseLiveLogsOptions {
  /** Max lines retained in memory. Older lines are dropped. Default: 2000. */
  maxLines?: number;
}

export interface UseLiveLogsResult {
  /** All log lines received since mount (oldest first). */
  lines: string[];
  /** Current WebSocket connection state. */
  connected: boolean;
  /** Manually reset the buffer (e.g. when switching bugs). */
  clear: () => void;
}

interface AgentStdoutFrame {
  type: "agent_stdout";
  text: string;
  role?: string;
  attempt?: number;
  backfill?: boolean;
  bug_id?: number;
}

export function useLiveLogs(
  bugId: number | undefined,
  options: UseLiveLogsOptions = {},
): UseLiveLogsResult {
  const maxLines = options.maxLines ?? 2000;
  const [lines, setLines] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  // Reconnect state: retry count drives the backoff delay.
  const retryRef = useRef<number>(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clear = useCallback(() => setLines([]), []);

  useEffect(() => {
    if (bugId === undefined) {
      return;
    }

    let cancelled = false;

    function clearReconnect() {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    }

    function connect() {
      clearReconnect();
      if (cancelled || bugId === undefined) {
        return;
      }
      const ws = new WebSocket(bugLogsWsUrl(bugId));
      wsRef.current = ws;

      ws.addEventListener("open", () => {
        setConnected(true);
        retryRef.current = 0;
      });

      ws.addEventListener("message", (event) => {
        let frame: AgentStdoutFrame | { type: string };
        try {
          frame = JSON.parse(event.data as string);
        } catch {
          return;
        }
        if (frame.type !== "agent_stdout") {
          return;
        }
        const stdoutFrame = frame as AgentStdoutFrame;
        const text = stdoutFrame.text;
        if (typeof text !== "string") {
          return;
        }
        setLines((prev) => {
          const next = prev.length >= maxLines
            ? [...prev.slice(prev.length - maxLines + 1), text]
            : [...prev, text];
          return next;
        });
      });

      ws.addEventListener("close", () => {
        setConnected(false);
        if (cancelled) {
          return;
        }
        // Exponential backoff, capped at 30s.
        const delay = Math.min(1000 * 2 ** retryRef.current, 30_000);
        retryRef.current += 1;
        reconnectTimerRef.current = setTimeout(connect, delay);
      });

      ws.addEventListener("error", () => {
        // ``error`` precedes ``close`` for abnormal closes — ``close`` is
        // where we actually schedule the reconnect. Nothing to do here.
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
  }, [bugId, maxLines]);

  return { lines, connected, clear };
}
