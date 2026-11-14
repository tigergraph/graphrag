import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Database, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { errorMessage, safeJson } from "@/utils/safeJson";
import { formatBytes } from "@/utils/formatBytes";

type Source = "uploads" | "downloads";
type FileState =
  | "not_ingested"
  | "changed"
  | "not_verified"
  | "older_version"
  | "ingested"
  | "conflict";

interface StoredFile {
  name: string;
  source: Source;
  size: number;
  modified: number;
  doc_ids: string[];
  converted: boolean;
  state: FileState;
  conflict_with: string[];
  legacy_id: string | null;
}

interface DocumentsResponse {
  files: StoredFile[];
  counts: Partial<Record<FileState, number>>;
  total: number;
  versions_ready: boolean;
}

interface FileRef {
  source: Source;
  name: string;
}

interface IngestResult {
  loaded: Array<FileRef & { state: FileState }>;
  skipped: Array<FileRef & { state: FileState; reason: string }>;
  failed: Array<FileRef & { error: string }>;
}

const STATE_ORDER: FileState[] = [
  "not_ingested",
  "changed",
  "not_verified",
  "older_version",
  "ingested",
  "conflict",
];

const STATE_INFO: Record<
  FileState,
  { label: string; tooltip: string; pill: string }
> = {
  not_ingested: {
    label: "Not ingested",
    tooltip: "This file is not in the graph yet.",
    pill: "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-900/20 dark:text-blue-300 dark:border-blue-800",
  },
  changed: {
    label: "Changed",
    tooltip: "The graph has this file with different content.",
    pill: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:border-amber-800",
  },
  not_verified: {
    label: "Not verified",
    tooltip: "The graph has this file, but its content cannot be compared with the stored file.",
    pill: "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-800/60 dark:text-gray-300 dark:border-gray-600",
  },
  older_version: {
    label: "Older version",
    tooltip: "The graph has this file under its earlier name, without the file extension.",
    pill: "bg-purple-50 text-purple-700 border-purple-200 dark:bg-purple-900/20 dark:text-purple-300 dark:border-purple-800",
  },
  ingested: {
    label: "Ingested",
    tooltip: "The graph has this file with the same content.",
    pill: "bg-green-50 text-green-700 border-green-200 dark:bg-green-900/20 dark:text-green-300 dark:border-green-800",
  },
  conflict: {
    label: "Conflict",
    tooltip: "Another stored file has the same name when letter case is ignored. Rename or delete one of them.",
    pill: "bg-red-50 text-red-700 border-red-200 dark:bg-red-900/20 dark:text-red-300 dark:border-red-800",
  },
};

const keyOf = (f: FileRef) => `${f.source}/${f.name}`;

// Label of a stored-files source: the two built-in sources by what added the
// files, any other folder by its name ("server" -> "Server").
const sourceLabel = (source: string) =>
  source === "downloads"
    ? "Cloud"
    : source === "uploads"
    ? "Upload"
    : source.charAt(0).toUpperCase() + source.slice(1);
const plural = (n: number, one: string, many: string) =>
  `${n} ${n === 1 ? one : many}`;

interface StoredFilesPanelProps {
  graph: string;
  active: boolean;
  refreshKey: number;
  busy: boolean;
  confirm: (message: string) => Promise<boolean>;
  onIngestStart: () => void;
  onIngestEnd: () => void;
}

const StoredFilesPanel: React.FC<StoredFilesPanelProps> = ({
  graph,
  active,
  refreshKey,
  busy,
  confirm,
  onIngestStart,
  onIngestEnd,
}) => {
  const [data, setData] = useState<DocumentsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [override, setOverride] = useState(false);
  const [working, setWorking] = useState<"" | "ingest" | "delete" | "rename">("");
  // The conflict file being renamed, and the new name without its extension.
  const [renaming, setRenaming] = useState<{
    key: string;
    dir: string;
    base: string;
    ext: string;
  } | null>(null);
  const [message, setMessage] = useState("");
  const [result, setResult] = useState<IngestResult | null>(null);

  const load = useCallback(async () => {
    if (!graph) {
      setData(null);
      return;
    }
    setLoading(true);
    setLoadError("");
    try {
      const creds = sessionStorage.getItem("auth");
      const resp = await fetch(`/ui/${graph}/documents`, {
        headers: { Authorization: creds! },
      });
      const body = await safeJson(resp);
      if (!resp.ok) {
        setData(null);
        setLoadError(body.detail || `Could not load the files: ${resp.statusText}`);
        return;
      }
      const next: DocumentsResponse = {
        files: Array.isArray(body.files) ? body.files : [],
        counts: body.counts || {},
        total: body.total ?? 0,
        versions_ready: !!body.versions_ready,
      };
      setData(next);
      // Keep only selections that still exist.
      const valid = new Set(next.files.map(keyOf));
      setSelected((prev) => new Set([...prev].filter((k) => valid.has(k))));
    } catch (err: unknown) {
      setData(null);
      setLoadError(errorMessage(err) || "Could not load the files.");
    } finally {
      setLoading(false);
    }
  }, [graph]);

  // A different graph starts with a clean slate.
  useEffect(() => {
    setSelected(new Set());
    setOverride(false);
    setMessage("");
    setResult(null);
  }, [graph]);

  useEffect(() => {
    if (active) load();
  }, [active, load, refreshKey]);

  const files = useMemo(() => data?.files ?? [], [data]);
  const fileByKey = useMemo(
    () => new Map(files.map((f) => [keyOf(f), f])),
    [files]
  );
  const selectedFiles = useMemo(
    () =>
      [...selected]
        .map((k) => fileByKey.get(k))
        .filter((f): f is StoredFile => !!f),
    [selected, fileByKey]
  );
  const selectedCounts = useMemo(() => {
    const c: Partial<Record<FileState, number>> = {};
    for (const f of selectedFiles) c[f.state] = (c[f.state] ?? 0) + 1;
    return c;
  }, [selectedFiles]);

  const versionsReady = data?.versions_ready ?? true;
  const nNew = selectedCounts.not_ingested ?? 0;
  const nContent =
    (selectedCounts.changed ?? 0) + (selectedCounts.not_verified ?? 0);
  const nOlder = selectedCounts.older_version ?? 0;
  // Older versions can only be replaced once the graph supports it.
  const nOlderLoadable = versionsReady ? nOlder : 0;
  const nOverridable = nContent + nOlder;
  const nToLoad = nNew + (override ? nContent + nOlderLoadable : 0);
  const isBusy = busy || !!working;

  const toggle = (f: StoredFile) => {
    setSelected((prev) => {
      const next = new Set(prev);
      const k = keyOf(f);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  const addState = (state: FileState) => {
    setSelected((prev) => {
      const next = new Set(prev);
      files.filter((f) => f.state === state).forEach((f) => next.add(keyOf(f)));
      return next;
    });
  };

  const allSelected =
    files.length > 0 && files.every((f) => selected.has(keyOf(f)));
  const selectAll = () => setSelected(new Set(files.map(keyOf)));
  const clearSelection = () => setSelected(new Set());

  const handleIngest = async () => {
    if (!graph || nToLoad === 0) return;
    if (override && (nContent > 0 || nOlderLoadable > 0)) {
      const lines: string[] = [];
      if (nContent > 0)
        lines.push(
          `${plural(nContent, "document gets", "documents get")} ${nContent === 1 ? "its" : "their"} new content.`
        );
      if (nOlderLoadable > 0)
        lines.push(
          `${plural(nOlderLoadable, "older version is", "older versions are")} replaced. The earlier ${nOlderLoadable === 1 ? "document is" : "documents are"} kept and marked as outdated.`
        );
      if (nNew > 0)
        lines.push(
          `${plural(nNew, "new document is", "new documents are")} added.`
        );
      const ok = await confirm(`${lines.join(" ")}\n\nIngest the selected files?`);
      if (!ok) return;
    }

    setWorking("ingest");
    setResult(null);
    setMessage(`Ingesting ${plural(nToLoad, "file", "files")}…`);
    onIngestStart();
    try {
      const creds = sessionStorage.getItem("auth");
      const resp = await fetch(`/ui/${graph}/documents/ingest`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: creds!,
        },
        body: JSON.stringify({
          files: selectedFiles.map((f) => ({ source: f.source, name: f.name })),
          override,
        }),
      });
      const body = await safeJson(resp);
      if (!resp.ok) {
        setMessage(`❌ ${body.detail || `Ingest failed: ${resp.statusText}`}`);
        return;
      }
      const res: IngestResult = {
        loaded: Array.isArray(body.loaded) ? body.loaded : [],
        skipped: Array.isArray(body.skipped) ? body.skipped : [],
        failed: Array.isArray(body.failed) ? body.failed : [],
      };
      setResult(res);
      const summary = `${res.loaded.length} loaded, ${res.skipped.length} skipped, ${res.failed.length} failed.`;
      const icon = res.failed.length > 0 ? "⚠️" : "✅";
      setMessage(
        `${icon} ${summary}` +
          (res.loaded.length > 0
            ? " Run a rebuild to process the new content."
            : "")
      );
      setSelected(new Set());
    } catch (err: unknown) {
      setMessage(`❌ Ingest failed: ${errorMessage(err)}`);
    } finally {
      setWorking("");
      onIngestEnd();
      load();
    }
  };

  // Only the file name changes: the subfolder and the extension stay.
  const startRename = (f: StoredFile) => {
    const slash = f.name.lastIndexOf("/");
    const dir = slash >= 0 ? f.name.slice(0, slash + 1) : "";
    const file = f.name.slice(slash + 1);
    const dot = file.lastIndexOf(".");
    const hasExt = dot > 0;
    setRenaming({
      key: keyOf(f),
      dir,
      base: hasExt ? file.slice(0, dot) : file,
      ext: hasExt ? file.slice(dot) : "",
    });
  };

  const handleRename = async (f: StoredFile) => {
    if (!graph || !renaming) return;
    const newName = `${renaming.base.trim()}${renaming.ext}`;
    setWorking("rename");
    setResult(null);
    try {
      const creds = sessionStorage.getItem("auth");
      const resp = await fetch(`/ui/${graph}/documents/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: creds! },
        body: JSON.stringify({ source: f.source, name: f.name, new_name: newName }),
      });
      const body = await safeJson(resp);
      if (!resp.ok) {
        setMessage(`❌ ${body.detail || `Rename failed: ${resp.statusText}`}`);
        return;
      }
      setRenaming(null);
      setMessage(`✅ Renamed ${f.name} to ${body.name}.`);
    } catch (err: unknown) {
      setMessage(`❌ Rename failed: ${errorMessage(err)}`);
    } finally {
      setWorking("");
      load();
    }
  };

  const handleDelete = async (targets: StoredFile[]) => {
    if (!graph || targets.length === 0) return;
    const n = targets.length;
    const what = plural(n, "selected file", "selected files");
    const ok = await confirm(
      `Delete ${what}? Documents already in the graph do not change.`
    );
    if (!ok) return;
    setWorking("delete");
    setResult(null);
    setMessage(`Deleting ${plural(n, "file", "files")}…`);
    try {
      const creds = sessionStorage.getItem("auth");
      const resp = await fetch(`/ui/${graph}/documents/delete`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: creds!,
        },
        body: JSON.stringify({
          files: targets.map((f) => ({ source: f.source, name: f.name })),
        }),
      });
      const body = await safeJson(resp);
      if (!resp.ok) {
        setMessage(`❌ ${body.detail || `Delete failed: ${resp.statusText}`}`);
        return;
      }
      const deleted = Array.isArray(body.deleted) ? body.deleted.length : 0;
      setMessage(`✅ Deleted ${plural(deleted, "file", "files")}.`);
      setSelected((prev) => {
        const next = new Set(prev);
        targets.forEach((f) => next.delete(keyOf(f)));
        return next;
      });
    } catch (err: unknown) {
      setMessage(`❌ Delete failed: ${errorMessage(err)}`);
    } finally {
      setWorking("");
      load();
    }
  };

  const total = data?.total ?? files.length;
  const selectionSummary =
    selectedFiles.length === 0
      ? plural(total, "file", "files")
      : `${selectedFiles.length} of ${total} selected: ` +
        STATE_ORDER.filter((s) => (selectedCounts[s] ?? 0) > 0)
          .map((s) => `${selectedCounts[s]} ${STATE_INFO[s].label.toLowerCase()}`)
          .join(", ");

  const messageClass = message.includes("❌")
    ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
    : message.includes("⚠️")
    ? "bg-yellow-50 dark:bg-yellow-900/20 text-yellow-700 dark:text-yellow-300"
    : message.includes("✅")
    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
    : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300";

  const problemFiles = result
    ? [
        ...result.skipped.map((s) => ({ ...s, text: s.reason, kind: "Skipped" })),
        ...result.failed.map((f) => ({ ...f, text: f.error, kind: "Failed" })),
      ]
    : [];

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium text-gray-500 dark:text-gray-400">
          Files added by upload, by cloud download, or in a folder on the server. Select files to ingest into
          your knowledge graph or to delete.
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={load}
          disabled={loading || !graph}
          className="dark:border-[#3D3D3D] shrink-0"
        >
          <RefreshCw className={`h-3 w-3 mr-1 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      {loadError && (
        <div className="p-3 rounded-lg text-sm bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300">
          ❌ {loadError}
        </div>
      )}

      {data && (
        <>
          {/* Select by state */}
          <div>
            <div className="text-sm font-medium mb-2 text-black dark:text-white">
              Select by state
            </div>
            <div className="flex flex-wrap gap-2">
              {STATE_ORDER.map((s) => {
                const count = data.counts[s] ?? 0;
                return (
                  <button
                    key={s}
                    type="button"
                    title={STATE_INFO[s].tooltip}
                    onClick={() => addState(s)}
                    disabled={count === 0 || isBusy}
                    className={`px-2.5 py-1 rounded-full border text-xs font-medium transition-opacity ${STATE_INFO[s].pill} ${
                      count === 0
                        ? "opacity-50 cursor-default"
                        : "hover:opacity-80 cursor-pointer"
                    }`}
                  >
                    {STATE_INFO[s].label} ({count})
                  </button>
                );
              })}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={selectAll}
                disabled={files.length === 0 || isBusy}
                className="h-7 text-xs dark:border-[#3D3D3D]"
              >
                Select all
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={clearSelection}
                disabled={selected.size === 0 || isBusy}
                className="h-7 text-xs dark:border-[#3D3D3D]"
              >
                Clear
              </Button>
            </div>
          </div>

          {!data.versions_ready && (data.counts.older_version ?? 0) > 0 && (
            <div className="p-3 rounded-lg text-xs bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-300 border border-amber-200 dark:border-amber-700">
              Older versions can be replaced after the graph is updated in the
              Migration Assistant.
            </div>
          )}

          {/* File table */}
          <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg max-h-96 overflow-y-auto">
            <Table>
              <TableHeader>
                <TableRow className="dark:border-[#3D3D3D]">
                  <TableHead className="w-10 h-10 px-3">
                    <input
                      type="checkbox"
                      aria-label="Select all files"
                      checked={allSelected}
                      onChange={() => (allSelected ? clearSelection() : selectAll())}
                      disabled={files.length === 0 || isBusy}
                      className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
                    />
                  </TableHead>
                  <TableHead className="h-10 px-3">File</TableHead>
                  <TableHead className="h-10 px-3">Source</TableHead>
                  <TableHead className="h-10 px-3 text-right">Size</TableHead>
                  <TableHead className="h-10 px-3">Added</TableHead>
                  <TableHead className="h-10 px-3">State</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {files.length === 0 ? (
                  <TableRow className="dark:border-[#3D3D3D]">
                    <TableCell
                      colSpan={6}
                      className="px-3 py-6 text-center text-sm text-gray-500 dark:text-gray-400"
                    >
                      No stored files. Add files on the Upload or Download from
                      Cloud tab.
                    </TableCell>
                  </TableRow>
                ) : (
                  files.map((f) => {
                    const k = keyOf(f);
                    const isConflict = f.state === "conflict";
                    return (
                      <TableRow
                        key={k}
                        data-state={selected.has(k) ? "selected" : undefined}
                        onClick={() => !isBusy && toggle(f)}
                        className="dark:border-[#3D3D3D] cursor-pointer"
                      >
                        <TableCell className="px-3 py-2">
                          <input
                            type="checkbox"
                            aria-label={`Select ${f.name}`}
                            checked={selected.has(k)}
                            disabled={isBusy}
                            onClick={(e) => e.stopPropagation()}
                            onChange={() => toggle(f)}
                            className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
                          />
                        </TableCell>
                        <TableCell className="px-3 py-2 max-w-[16rem]">
                          <div
                            className="truncate text-black dark:text-white"
                            title={f.name}
                          >
                            {f.name}
                          </div>
                          {isConflict && (
                            <div className="flex items-center gap-1 text-xs text-red-600 dark:text-red-400">
                              {f.conflict_with.length > 0 && (
                                <span className="truncate">
                                  Same name as{" "}
                                  {f.conflict_with.join(", ")}
                                </span>
                              )}
                              {renaming?.key !== k && (
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    startRename(f);
                                  }}
                                  disabled={isBusy}
                                  className="shrink-0 underline hover:no-underline disabled:opacity-50"
                                >
                                  Rename
                                </button>
                              )}
                            </div>
                          )}
                          {isConflict && renaming?.key === k && (
                            <form
                              className="mt-1 flex items-center gap-1"
                              onClick={(e) => e.stopPropagation()}
                              onSubmit={(e) => {
                                e.preventDefault();
                                handleRename(f);
                              }}
                            >
                              {renaming.dir && (
                                <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[8rem]" title={renaming.dir}>
                                  {renaming.dir}
                                </span>
                              )}
                              <input
                                autoFocus
                                aria-label={`New name for ${f.name}`}
                                value={renaming.base}
                                onChange={(e) => setRenaming({ ...renaming, base: e.target.value })}
                                onKeyDown={(e) => e.key === "Escape" && setRenaming(null)}
                                disabled={isBusy}
                                className="min-w-0 flex-1 rounded border border-gray-300 dark:border-[#3D3D3D] bg-white dark:bg-shadeA px-1.5 py-0.5 text-xs text-black dark:text-white"
                              />
                              {renaming.ext && (
                                <span className="text-xs text-gray-500 dark:text-gray-400">{renaming.ext}</span>
                              )}
                              <Button
                                type="submit"
                                size="sm"
                                disabled={isBusy || !renaming.base.trim()}
                                className="h-6 px-2 text-xs"
                              >
                                Save
                              </Button>
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                onClick={() => setRenaming(null)}
                                disabled={working === "rename"}
                                className="h-6 px-2 text-xs dark:border-[#3D3D3D]"
                              >
                                Cancel
                              </Button>
                            </form>
                          )}
                        </TableCell>
                        <TableCell className="px-3 py-2 text-gray-600 dark:text-gray-300">
                          {sourceLabel(f.source)}
                        </TableCell>
                        <TableCell className="px-3 py-2 text-right whitespace-nowrap text-gray-600 dark:text-gray-300">
                          {formatBytes(f.size)}
                        </TableCell>
                        <TableCell className="px-3 py-2 whitespace-nowrap text-gray-600 dark:text-gray-300">
                          {f.modified
                            ? new Date(f.modified * 1000).toLocaleString()
                            : "—"}
                        </TableCell>
                        <TableCell className="px-3 py-2">
                          <span
                            title={STATE_INFO[f.state]?.tooltip}
                            className={`inline-block px-2 py-0.5 rounded-full border text-xs font-medium whitespace-nowrap ${
                              STATE_INFO[f.state]?.pill ?? ""
                            }`}
                          >
                            {STATE_INFO[f.state]?.label ?? f.state}
                          </span>
                        </TableCell>
                      </TableRow>
                    );
                  })
                )}
              </TableBody>
            </Table>
          </div>

          <div className="text-sm text-gray-600 dark:text-gray-300">
            {selectionSummary}
          </div>

          <label className="flex items-center gap-2 text-sm text-black dark:text-white cursor-pointer w-fit">
            <input
              type="checkbox"
              checked={override}
              onChange={(e) => setOverride(e.target.checked)}
              disabled={isBusy}
              className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
            />
            <span>
              Override documents already in the graph
              <span className="text-gray-500 dark:text-gray-400">
                {" "}
                ({plural(nOverridable, "selected file", "selected files")})
              </span>
            </span>
          </label>

          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => handleDelete(selectedFiles)}
              disabled={selectedFiles.length === 0 || isBusy}
              className="dark:border-[#3D3D3D]"
            >
              {working === "delete" ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Trash2 className="h-4 w-4 mr-2" />
              )}
              Delete selected
            </Button>
            <Button
              type="button"
              onClick={handleIngest}
              disabled={nToLoad === 0 || isBusy}
              className="gradient text-white flex-1"
            >
              {working === "ingest" ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Ingesting...
                </>
              ) : (
                <>
                  <Database className="h-4 w-4 mr-2" />
                  Ingest selected ({nToLoad})
                </>
              )}
            </Button>
          </div>
        </>
      )}

      {message && (
        <div className={`p-3 rounded-lg text-sm whitespace-pre-line ${messageClass}`}>
          {message}
          {problemFiles.length > 0 && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs font-medium">
                Show skipped and failed files ({problemFiles.length})
              </summary>
              <ul className="mt-2 space-y-1 text-xs">
                {problemFiles.map((p) => (
                  <li key={`${p.kind}:${keyOf(p)}`}>
                    <span className="font-medium">{p.name}</span>
                    {" "}({sourceLabel(p.source)}) —{" "}
                    {p.kind}: {p.text}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </div>
  );
};

export default StoredFilesPanel;
