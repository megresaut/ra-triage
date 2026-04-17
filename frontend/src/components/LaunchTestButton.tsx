// Launch / Stop button for the currently-focused bug.
//
// Rules (from Agent 7 brief):
//   * Disabled unless bug is in READY_FOR_REVIEW or BLOCKED.
//     (BLOCKED is launchable so Megha can inspect partial fixes — the
//     "with-diff" filter is enforced by pipeline side: a BLOCKED bug
//     without a worktree can't be launched anyway, and the backend returns
//     a 409 with a clear message.)
//   * If THIS bug is currently the launched instance, the button reads
//     "Stop" and calls /test-instances/stop.
//   * If a DIFFERENT bug is currently launched, clicking shows a dialog
//     confirming the switch before calling /launch.
//   * Otherwise it calls /launch immediately.

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  useLaunchTestInstance,
  useStopTestInstance,
} from "@/hooks/useTestInstance";
import type { TestInstance } from "@/lib/api";

interface LaunchTestButtonProps {
  bugId: number;
  bugState: string;
  /** Currently-running instance (from /current or WS). Null if none. */
  currentInstance: TestInstance | null;
}

const LAUNCHABLE_STATES = new Set(["READY_FOR_REVIEW", "BLOCKED"]);

export default function LaunchTestButton({
  bugId,
  bugState,
  currentInstance,
}: LaunchTestButtonProps) {
  const launchMut = useLaunchTestInstance();
  const stopMut = useStopTestInstance();

  // Dialog state for the confirm-switch flow.
  const [switchOpen, setSwitchOpen] = useState(false);

  const canLaunch = LAUNCHABLE_STATES.has(bugState);
  const isRunningThis =
    currentInstance !== null &&
    currentInstance.bug_id === bugId &&
    (currentInstance.status === "running" ||
      currentInstance.status === "starting");
  const otherBugRunning =
    currentInstance !== null &&
    currentInstance.bug_id !== bugId &&
    (currentInstance.status === "running" ||
      currentInstance.status === "starting");

  if (isRunningThis) {
    return (
      <Button
        variant="outline"
        onClick={() => stopMut.mutate()}
        disabled={stopMut.isPending}
      >
        {stopMut.isPending ? "Stopping…" : "Stop test instance"}
      </Button>
    );
  }

  const launch = () => launchMut.mutate(bugId);

  const onClick = () => {
    if (otherBugRunning) {
      setSwitchOpen(true);
      return;
    }
    launch();
  };

  return (
    <>
      <Button
        onClick={onClick}
        disabled={!canLaunch || launchMut.isPending}
        title={
          canLaunch
            ? undefined
            : "Launch is only available when the bug is READY_FOR_REVIEW or BLOCKED"
        }
      >
        {launchMut.isPending ? "Launching…" : "Launch for Testing"}
      </Button>

      <Dialog open={switchOpen} onOpenChange={setSwitchOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Switch test instance?</DialogTitle>
            <DialogDescription>
              This will stop the test instance for bug #
              {currentInstance?.bug_id} and launch bug #{bugId}.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setSwitchOpen(false)}
              disabled={launchMut.isPending}
            >
              Cancel
            </Button>
            <Button
              onClick={() => {
                setSwitchOpen(false);
                launch();
              }}
              disabled={launchMut.isPending}
            >
              {launchMut.isPending ? "Launching…" : "Stop & launch"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {launchMut.isError && (
        <p className="mt-2 text-xs text-red-700">
          Launch failed:{" "}
          {launchMut.error instanceof Error
            ? launchMut.error.message
            : "unknown error"}
        </p>
      )}
    </>
  );
}
