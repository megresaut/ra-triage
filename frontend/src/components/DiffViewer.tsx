// Per-bug unified-diff renderer for BugDetail.
//
// Fetches the diff via ``useDiff``, splits the unified output into
// per-file chunks with a small hand-rolled parser (keeps the dep
// surface small), and renders each file as a collapsible
// ``<details>`` element. Each file's old/new content is reconstructed
// from the hunk headers and rendered through ``react-diff-viewer-
// continued`` in split-view mode.
//
// Structured so Agent 10 can trivially wrap the outer component in a
// Tabs container for the cross-repo case.

import ReactDiffViewer from "react-diff-viewer-continued";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useDiff } from "@/hooks/useDiff";

// --- unified-diff parser -----------------------------------------------

interface FileDiff {
  /** Path of the file on the new side (post-image). */
  newPath: string;
  /** Path of the file on the old side (pre-image). */
  oldPath: string;
  /** Full text of the diff chunk for this file (``diff --git`` block). */
  raw: string;
  /** Reconstructed old content from the hunks. */
  oldContent: string;
  /** Reconstructed new content from the hunks. */
  newContent: string;
}

/** Split a unified diff into per-file chunks.
 *
 * Recognizes the standard ``diff --git a/... b/...`` headers git
 * emits. Each chunk starts at a ``diff --git`` line and runs until the
 * next ``diff --git`` line (or end of input).
 */
function parseUnifiedDiff(diff: string): FileDiff[] {
  if (!diff.trim()) {
    return [];
  }

  const lines = diff.split("\n");
  const files: FileDiff[] = [];
  let current: string[] = [];

  const flush = () => {
    if (current.length === 0) {
      return;
    }
    const raw = current.join("\n");
    const parsed = parseFileChunk(raw);
    if (parsed) {
      files.push(parsed);
    }
    current = [];
  };

  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      flush();
    }
    current.push(line);
  }
  flush();

  return files;
}

function parseFileChunk(raw: string): FileDiff | null {
  const lines = raw.split("\n");
  if (lines.length === 0) {
    return null;
  }
  // ``diff --git a/<old> b/<new>``. Preserve paths; fall back to the
  // ``--- /+++`` lines if the header is malformed for any reason.
  let oldPath = "";
  let newPath = "";

  const match = lines[0].match(/^diff --git a\/(.+?) b\/(.+?)\s*$/);
  if (match) {
    oldPath = match[1];
    newPath = match[2];
  }

  // Walk through the hunk data reconstructing before/after content.
  const oldParts: string[] = [];
  const newParts: string[] = [];
  let inHunks = false;

  for (const line of lines) {
    if (line.startsWith("--- ")) {
      if (!oldPath) {
        const p = line.slice(4).trim();
        oldPath = p.startsWith("a/") ? p.slice(2) : p;
      }
      continue;
    }
    if (line.startsWith("+++ ")) {
      if (!newPath) {
        const p = line.slice(4).trim();
        newPath = p.startsWith("b/") ? p.slice(2) : p;
      }
      continue;
    }
    if (line.startsWith("@@")) {
      inHunks = true;
      // Preserve the hunk header as context in both sides so the
      // diff viewer renders hunk boundaries. react-diff-viewer-
      // continued handles this nicely via its built-in hunk support.
      continue;
    }
    if (line.startsWith("diff --git ") || line.startsWith("index ")) {
      continue;
    }
    if (line.startsWith("new file mode") || line.startsWith("deleted file mode")) {
      continue;
    }
    if (!inHunks) {
      continue;
    }
    // Actual content line. Prefix determines which side.
    const prefix = line.charAt(0);
    const content = line.slice(1);
    if (prefix === "+") {
      newParts.push(content);
    } else if (prefix === "-") {
      oldParts.push(content);
    } else if (prefix === " ") {
      // Context line — appears in both.
      oldParts.push(content);
      newParts.push(content);
    } else if (prefix === "\\") {
      // ``\ No newline at end of file`` — ignore.
      continue;
    }
  }

  if (!oldPath && !newPath) {
    return null;
  }

  return {
    oldPath: oldPath || newPath,
    newPath: newPath || oldPath,
    raw,
    oldContent: oldParts.join("\n"),
    newContent: newParts.join("\n"),
  };
}

// --- component ----------------------------------------------------------

export interface DiffViewerProps {
  bugId: number;
  repo: string;
  /** The bug's current state. Drives polling frequency. */
  state: string;
}

/** Single-repo diff panel. Cross-repo: wrap N of these in Tabs (Agent 10). */
export default function DiffViewer({ bugId, repo, state }: DiffViewerProps) {
  const { data, isLoading, isError, error } = useDiff(bugId, repo, state);

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Diff · {repo}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Loading diff…
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    const msg = error instanceof Error ? error.message : "unknown error";
    const is404 = msg.includes("404");
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Diff · {repo}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm">
          {is404 ? (
            <span className="text-muted-foreground">
              No worktree on disk yet — the pipeline hasn't started this bug.
            </span>
          ) : (
            <span className="text-red-700">
              Failed to load diff: {msg}
            </span>
          )}
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return null;
  }

  const files = parseUnifiedDiff(data.diff);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Diff · {repo}
          <span className="ml-2 text-xs font-mono text-muted-foreground">
            {data.head_ref} vs {data.base_ref}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {files.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No changes on this branch yet.
          </p>
        )}
        {files.map((file, idx) => (
          <details
            key={`${file.newPath}-${idx}`}
            className="border rounded overflow-hidden"
            open={idx < 3}
          >
            <summary className="cursor-pointer px-3 py-2 bg-slate-50 hover:bg-slate-100 font-mono text-xs">
              {file.newPath}
              {file.oldPath !== file.newPath && (
                <span className="text-muted-foreground">
                  {" "}(renamed from {file.oldPath})
                </span>
              )}
            </summary>
            <div className="text-xs">
              <ReactDiffViewer
                oldValue={file.oldContent}
                newValue={file.newContent}
                splitView
                useDarkTheme={false}
                leftTitle={file.oldPath}
                rightTitle={file.newPath}
              />
            </div>
          </details>
        ))}
      </CardContent>
    </Card>
  );
}
