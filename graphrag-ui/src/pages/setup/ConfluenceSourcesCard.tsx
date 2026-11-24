import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { pauseIdleTimer, pingIdleTimer, resumeIdleTimer } from "@/hooks/useIdleTimeout";
import { FileText, Loader2, Plus, Save, ShieldCheck, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { safeJson } from "@/utils/safeJson";
import type { ConfluenceSource, JiraSchemaStatus } from "@/types/dataSources";
import RestoreApiTokenCard from "@/pages/setup/RestoreApiTokenCard";

const syncFeedbackKey = (graph: string) =>
  `graphrag:confluence-sync-feedback:${graph}`;

const contentTypes = [
  { value: "page", label: "Pages" },
  { value: "blogpost", label: "Blog posts" },
] as const;

interface ConfluenceSpace {
  id?: string;
  key: string;
  name: string;
}

interface ActionFeedback {
  type: "success" | "error" | "pending";
  text: string;
}

const ActionStatus: React.FC<{ feedback: ActionFeedback | null }> = ({
  feedback,
}) => {
  if (!feedback) return null;
  return (
    <div
      role={feedback.type === "error" ? "alert" : "status"}
      className={`rounded-md border p-3 text-sm ${
        feedback.type === "success"
          ? "border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-900/20 dark:text-green-300"
          : feedback.type === "error"
            ? "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300"
            : "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-300"
      }`}
    >
      {feedback.text}
    </div>
  );
};

const emptySource = (): ConfluenceSource => ({
  id: "",
  type: "confluence",
  enabled: true,
  display_name: "",
  connection: { site: "", email: "", api_token: "" },
  scope: {
    space_keys: [],
    content_types: ["page", "blogpost"],
    updated_after: null,
  },
  sync: { overlap_seconds: 120, last_page_count: 0 },
});

const labelClass = "block text-sm font-medium mb-2 text-black dark:text-white";
const inputClass = "dark:border-[#3D3D3D] dark:bg-background";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const isNetworkError = (error: unknown) => {
  const message = error instanceof Error ? error.message : String(error);
  return (
    message === "Failed to fetch" ||
    message === "Load failed" ||
    message === "NetworkError when attempting to fetch resource."
  );
};

const catalogBusy = (message: string, status?: number) =>
  (status != null && status >= 500) ||
  /lock catalog|another command is running|try lock timeout/i.test(message);

const errorDetail = (data: any, fallback: string) => {
  if (typeof data?.detail === "string") return data.detail;
  if (typeof data?.detail?.message === "string") {
    const conflicts = Array.isArray(data.detail.conflicts)
      ? ` ${data.detail.conflicts.join("; ")}`
      : "";
    return `${data.detail.message}${conflicts}`;
  }
  if (Array.isArray(data?.detail)) {
    return data.detail
      .map((item: any) => item?.msg)
      .filter(Boolean)
      .join("; ") || fallback;
  }
  return fallback;
};

const formatDate = (value?: string | null) => {
  if (!value) return "Never";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
};

const ingestProgressText = (source: ConfluenceSource) => {
  const checked = source.sync.progress_checked || 0;
  const written = source.sync.progress_written || 0;
  const content =
    source.sync.last_content_id != null
      ? ` · saved through content ${source.sync.last_content_id}`
      : "";
  return `Ingesting ${source.display_name} · ${checked} checked · ${written} written${content}`;
};

const tokenMissing = (message: string) =>
  message.toLowerCase().includes("api token is not available");

const ingestStoppedEarly = (sync: ConfluenceSource["sync"]) => {
  if (!sync.last_started_at || (!sync.checkpoint && sync.last_content_id == null)) {
    return false;
  }
  if (!sync.last_completed_at) return true;
  const started = Date.parse(sync.last_started_at);
  const completed = Date.parse(sync.last_completed_at);
  return Number.isFinite(started) && Number.isFinite(completed) && started > completed;
};

const ConfluenceSourcesCard: React.FC<{ graph: string }> = ({ graph }) => {
  const [sources, setSources] = useState<ConfluenceSource[]>([]);
  const [editing, setEditing] = useState<ConfluenceSource | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [connectionVerified, setConnectionVerified] = useState(false);
  const [spaces, setSpaces] = useState<ConfluenceSpace[]>([]);
  const [spaceSearch, setSpaceSearch] = useState("");
  const [schema, setSchema] = useState<JiraSchemaStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [connectionFeedback, setConnectionFeedback] =
    useState<ActionFeedback | null>(null);
  const [spacesFeedback, setSpacesFeedback] = useState<ActionFeedback | null>(null);
  const [saveFeedback, setSaveFeedback] = useState<ActionFeedback | null>(null);
  const [countFeedback, setCountFeedback] = useState<ActionFeedback | null>(null);
  const [schemaFeedback, setSchemaFeedback] = useState<ActionFeedback | null>(null);
  const [syncFeedback, setSyncFeedbackRaw] = useState<ActionFeedback | null>(null);
  const [open, setOpen] = useState(false);
  const installWatchRef = useRef(0);
  const installActiveRef = useRef(false);
  const [tokenPromptFor, setTokenPromptFor] = useState<string | null>(null);

  const authHeaders = () => ({
    Authorization: sessionStorage.getItem("auth") || "",
    "Content-Type": "application/json",
  });

  const setSyncFeedback = (feedback: ActionFeedback | null) => {
    const key = syncFeedbackKey(graph);
    if (feedback) sessionStorage.setItem(key, JSON.stringify(feedback));
    else sessionStorage.removeItem(key);
    setSyncFeedbackRaw(feedback);
  };

  const resetEditor = () => {
    setConnectionVerified(false);
    setSpaces([]);
    setSpaceSearch("");
    setConnectionFeedback(null);
    setSpacesFeedback(null);
    setSaveFeedback(null);
    setCountFeedback(null);
  };

  const watchSchemaInstall = useCallback(async () => {
    const watchId = ++installWatchRef.current;
    installActiveRef.current = true;
    setBusy("schema");
    pauseIdleTimer();
    setSchemaFeedback({
      type: "pending",
      text: "Installing Confluence schema. This keeps running if the laptop locks.",
    });
    let misses = 0;
    try {
      while (installWatchRef.current === watchId) {
        try {
          const response = await fetch(`/ui/${graph}/data-sources/confluence/schema`, {
            headers: { Authorization: sessionStorage.getItem("auth") || "" },
          });
          const data = await safeJson(response);
          if (installWatchRef.current !== watchId) return;
          if (!response.ok) {
            const detail = errorDetail(data, `Failed to inspect schema (${response.status})`);
            if (catalogBusy(detail, response.status)) {
              misses = 0;
              setSchemaFeedback({
                type: "pending",
                text: "Installing Confluence schema. The catalog is busy while the change runs.",
              });
            } else {
              throw new Error(detail);
            }
          } else {
            pingIdleTimer();
            setSchema(data);
            const installStatus = data.install?.status;
            if (installStatus === "failed") {
              throw new Error(data.install?.error || "Confluence schema installation failed.");
            }
            if (installStatus === "completed") {
              setSchemaFeedback({ type: "success", text: "Confluence schema installed." });
              return;
            }
            if (installStatus === "queued" || installStatus === "running") {
              misses = 0;
            } else if (data.status === "installed") {
              setSchemaFeedback({ type: "success", text: "Confluence schema installed." });
              return;
            } else {
              misses += 1;
              if (misses >= 5) {
                throw new Error(
                  "The connection dropped before installation started. Install the Confluence schema again."
                );
              }
            }
          }
        } catch (error: any) {
          if (installWatchRef.current !== watchId) return;
          if (!isNetworkError(error) && !catalogBusy(error.message || "")) throw error;
          setSchemaFeedback({
            type: "pending",
            text: "The laptop lock closed the browser connection. Schema installation is still running and will show here when the network returns.",
          });
        }
        await sleep(3000);
      }
    } catch (error: any) {
      if (installWatchRef.current !== watchId) return;
      setSchemaFeedback({
        type: "error",
        text: `Schema installation failed: ${error.message}`,
      });
    } finally {
      if (installWatchRef.current === watchId) {
        installActiveRef.current = false;
        resumeIdleTimer();
        setBusy("");
      }
    }
  }, [graph]);

  const loadSchema = useCallback(async () => {
    if (!graph) {
      setSchema(null);
      return;
    }
    const response = await fetch(`/ui/${graph}/data-sources/confluence/schema`, {
      headers: { Authorization: sessionStorage.getItem("auth") || "" },
    });
    const data = await safeJson(response);
    if (!response.ok) {
      throw new Error(errorDetail(data, `HTTP ${response.status}`));
    }
    setSchema(data);
    const installStatus = data.install?.status;
    if (
      (installStatus === "queued" || installStatus === "running") &&
      !installActiveRef.current
    ) {
      void watchSchemaInstall();
    } else if (installStatus === "failed" && !installActiveRef.current) {
      setSchemaFeedback({
        type: "error",
        text: `Schema installation failed: ${
          data.install?.error || "Confluence schema installation failed."
        }`,
      });
    }
  }, [graph, watchSchemaInstall]);

  const loadSources = useCallback(async (quiet = false) => {
    if (!graph) {
      setSources([]);
      return;
    }
    if (!quiet) {
      setLoading(true);
      setMessage("");
    }
    try {
      const response = await fetch(`/ui/${graph}/data-sources/confluence`, {
        headers: { Authorization: sessionStorage.getItem("auth") || "" },
      });
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      const loaded: ConfluenceSource[] = (
        Array.isArray(data.sources) ? data.sources : []
      ).filter((source: ConfluenceSource) => source?.type === "confluence");
      setSources(loaded);
      const running = loaded.find((source) => source.sync_running);
      if (running) {
        setSyncFeedback({ type: "pending", text: ingestProgressText(running) });
      } else if (!quiet) {
        const stored = sessionStorage.getItem(syncFeedbackKey(graph));
        if (stored) {
          try {
            setSyncFeedbackRaw(JSON.parse(stored));
          } catch {
            setSyncFeedbackRaw(null);
          }
        }
      }
      if (!quiet) await loadSchema();
    } catch (error: any) {
      if (!quiet) setMessage(`Failed to load Confluence sources: ${error.message}`);
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [graph, loadSchema]);

  useEffect(() => {
    setEditing(null);
    resetEditor();
    setSchemaFeedback(null);
    void loadSources();
  }, [loadSources]);

  const syncRunning = sources.some((source) => source.sync_running);
  useEffect(() => {
    if (!open || !syncRunning) return;
    const timer = window.setInterval(() => {
      void loadSources(true);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [open, syncRunning, loadSources]);

  const patch = (updates: Partial<ConfluenceSource>) => {
    setEditing((current) => (current ? { ...current, ...updates } : current));
  };

  const patchScope = (updates: Partial<ConfluenceSource["scope"]>) => {
    setEditing((current) =>
      current ? { ...current, scope: { ...current.scope, ...updates } } : current
    );
    setCountFeedback(null);
  };

  const loadSpaces = async (sourceId: string) => {
    setBusy("spaces");
    setSpacesFeedback({ type: "pending", text: "Loading visible Confluence spaces…" });
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(sourceId)}/spaces`,
        { headers: { Authorization: sessionStorage.getItem("auth") || "" } }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      const loaded: ConfluenceSpace[] = Array.isArray(data.spaces) ? data.spaces : [];
      loaded.sort((a, b) => a.key.localeCompare(b.key));
      setSpaces(loaded);
      setSpacesFeedback(
        loaded.length
          ? null
          : {
              type: "error",
              text: "Connection succeeded, but this account has no visible Confluence spaces.",
            }
      );
    } catch (error: any) {
      setSpaces([]);
      setSpacesFeedback({
        type: "error",
        text: `Failed to load Confluence spaces: ${error.message}`,
      });
    }
  };

  const testConnection = async () => {
    if (!editing) return;
    setBusy("test");
    setConnectionFeedback(null);
    setSpacesFeedback(null);
    setCountFeedback(null);
    const token = editing.connection.api_token || "";
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(editing.id)}/test`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify(editing),
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      const saved = data.source as ConfluenceSource;
      setEditing({ ...saved, connection: { ...saved.connection, api_token: token } });
      setIsNew(false);
      setConnectionVerified(true);
      setConnectionFeedback({
        type: "success",
        text: `Connected as ${
          data.account?.display_name || data.account?.email || "the Confluence account"
        }.`,
      });
      await loadSources(true);
      await loadSpaces(saved.id);
    } catch (error: any) {
      setConnectionVerified(false);
      setConnectionFeedback({ type: "error", text: error.message });
    } finally {
      setBusy("");
    }
  };

  const save = async () => {
    if (!editing) return;
    if (!(editing.scope.content_types || []).length) {
      setSaveFeedback({
        type: "error",
        text: "Select pages, blog posts, or both before saving the scope.",
      });
      return;
    }
    setBusy("save");
    setSaveFeedback(null);
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(editing.id)}`,
        {
          method: "PUT",
          headers: authHeaders(),
          body: JSON.stringify(editing),
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      setSaveFeedback({ type: "success", text: "Confluence scope saved." });
      setEditing(null);
      setConnectionVerified(false);
      await loadSources();
    } catch (error: any) {
      setSaveFeedback({ type: "error", text: error.message });
    } finally {
      setBusy("");
    }
  };

  const previewCount = async () => {
    if (!editing) return;
    if (!(editing.scope.content_types || []).length) {
      setCountFeedback({
        type: "error",
        text: "Select pages, blog posts, or both before previewing.",
      });
      return;
    }
    setBusy("count");
    setCountFeedback({
      type: "pending",
      text: "Checking how many pages and blog posts match this scope…",
    });
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(editing.id)}/content/count`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify(editing.scope),
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      const count = Number(data.count) || 0;
      const types = editing.scope.content_types || [];
      const noun =
        types.length === 2
          ? "pages and blog posts"
          : types[0] === "blogpost"
            ? count === 1 ? "blog post" : "blog posts"
            : count === 1 ? "page" : "pages";
      setCountFeedback({
        type: "success",
        text: `About ${count.toLocaleString()} ${noun} match this scope.`,
      });
    } catch (error: any) {
      setCountFeedback({ type: "error", text: error.message });
    } finally {
      setBusy("");
    }
  };

  const installSchema = async () => {
    setBusy("schema");
    pauseIdleTimer();
    setSchemaFeedback({
      type: "pending",
      text: "Installing Confluence schema. This keeps running if the laptop locks.",
    });
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/schema/install`,
        { method: "POST", headers: authHeaders() }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
    } catch (error: any) {
      if (!isNetworkError(error)) {
        setSchemaFeedback({
          type: "error",
          text: `Schema installation failed: ${error.message}`,
        });
        resumeIdleTimer();
        setBusy("");
        return;
      }
      setSchemaFeedback({
        type: "pending",
        text: "The laptop lock closed the browser connection. Checking whether schema installation is still running.",
      });
    }
    await watchSchemaInstall();
  };

  const pollSync = async (sourceId: string, runId: string) => {
    for (;;) {
      await sleep(2000);
      await loadSources(true);
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(sourceId)}/sync/${runId}`,
        { headers: { Authorization: sessionStorage.getItem("auth") || "" } }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      if (data.status === "completed") {
        const count = data.result?.pages_written;
        setSyncFeedback({
          type: "success",
          text:
            typeof count === "number"
              ? `Ingested ${count} Confluence page${count === 1 ? "" : "s"} and blog post${count === 1 ? "" : "s"}.`
              : "Confluence ingest completed.",
        });
        await loadSources();
        return;
      }
      if (data.status === "failed") {
        throw new Error(data.error || "Confluence ingest failed.");
      }
    }
  };

  const sync = async (source: ConfluenceSource, resume = false) => {
    if (source.token_available === false) {
      setTokenPromptFor(source.id);
      setSyncFeedback(null);
      return;
    }
    setBusy(`sync:${source.id}`);
    setSyncFeedback({
      type: "pending",
      text: resume
        ? `Resuming ${source.display_name}...`
        : `Ingesting ${source.display_name}...`,
    });
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(source.id)}/sync`,
        { method: "POST", headers: authHeaders() }
      );
      const data = await safeJson(response);
      if (!response.ok) {
        const detail = errorDetail(data, `HTTP ${response.status}`);
        if (detail.includes(`confluence_sync:${source.id}`)) {
          setSources((current) =>
            current.map((item) =>
              item.id === source.id ? { ...item, sync_running: true } : item
            )
          );
          setSyncFeedback({
            type: "pending",
            text: `${source.display_name} is already ingesting.`,
          });
          return;
        }
        throw new Error(detail);
      }
      await pollSync(source.id, data.run_id);
    } catch (error: any) {
      if (tokenMissing(error.message || "")) {
        setTokenPromptFor(source.id);
        setSyncFeedback(null);
        return;
      }
      setSyncFeedback({ type: "error", text: error.message });
    } finally {
      setBusy("");
    }
  };

  const restoreToken = async (source: ConfluenceSource, token: string) => {
    const response = await fetch(
      `/ui/${graph}/data-sources/confluence/${encodeURIComponent(source.id)}/test`,
      {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          ...source,
          connection: { ...source.connection, api_token: token },
        }),
      }
    );
    const data = await safeJson(response);
    if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
    await loadSources();
  };

  const remove = async (source: ConfluenceSource) => {
    if (!window.confirm(`Delete the Confluence connection "${source.display_name}"?`)) {
      return;
    }
    setBusy(`delete:${source.id}`);
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(source.id)}`,
        {
          method: "DELETE",
          headers: { Authorization: sessionStorage.getItem("auth") || "" },
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      if (editing?.id === source.id) setEditing(null);
      await loadSources();
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy("");
    }
  };

  const selectedSpaceKeys = editing?.scope.space_keys || [];
  const filteredSpaces = useMemo(() => {
    const query = spaceSearch.trim().toLowerCase();
    if (!query) return spaces;
    return spaces.filter(
      (space) =>
        space.key.toLowerCase().includes(query) ||
        space.name.toLowerCase().includes(query)
    );
  }, [spaces, spaceSearch]);

  const testedAndSaved = sources.some((source) => source.sync.last_tested_at);
  const summary = !sources.length
    ? "No connections yet"
    : schema?.status === "installed"
      ? `${sources.length} connection${sources.length === 1 ? "" : "s"} · Schema installed`
      : `${sources.length} connection${sources.length === 1 ? "" : "s"} · Schema not installed`;

  const scopeSummary = (source: ConfluenceSource) => {
    const keys = source.scope.space_keys || [];
    const spacesText = keys.length
      ? `${keys.length} space${keys.length === 1 ? "" : "s"}`
      : "All spaces";
    const types = (source.scope.content_types || [])
      .map((value) => (value === "page" ? "pages" : "blog posts"))
      .join(" + ");
    return `${spacesText} · ${types || "no content types"}`;
  };

  return (
    <>
    <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
      <div className="mb-4">
        <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
          <FileText className="h-6 w-6 text-tigerOrange" />
        </div>
        <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">
          Confluence
        </h2>
        <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
          Connect Confluence Cloud, select spaces, install its graph schema,
          then ingest pages and blog posts.
        </p>
        <p className="text-sm text-black dark:text-white">{summary}</p>
      </div>
      <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
        <Button
          onClick={() => setOpen(true)}
          className="gradient w-full text-white"
          disabled={!graph}
        >
          <FileText className="h-4 w-4 mr-2" />
          Set up Confluence
        </Button>
      </div>
    </div>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        className="sm:max-w-4xl max-h-[90vh] overflow-y-auto bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]"
        onInteractOutside={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle className="text-black dark:text-white">Confluence</DialogTitle>
          <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
            Connect Confluence Cloud, select spaces, install its graph schema,
            then ingest pages and blog posts into this graph.
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-end">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setEditing(emptySource());
              setIsNew(true);
              resetEditor();
            }}
            disabled={!!busy || !!editing || !graph}
          >
            <Plus className="h-4 w-4 mr-2" /> Add connection
          </Button>
        </div>
      <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-4 text-sm text-amber-800 dark:text-amber-200">
        Graph users can retrieve every page and blog post visible to the
        Confluence account used by this connector.
      </div>

      {message && (
        <div className="p-3 rounded-md text-sm border bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 border-red-200 dark:border-red-800">
          {message}
        </div>
      )}

      <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5">
        <h3 className="font-medium text-black dark:text-white">
          1. Connection and scope
        </h3>
        <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 mb-4">
          {loading
            ? "Loading Confluence sources..."
            : sources.length
              ? `${sources.length} configured source${sources.length === 1 ? "" : "s"}`
              : "No Confluence sources configured for this graph."}
        </p>
        <div className="space-y-3">
          {sources.map((source) => (
            <div
              key={source.id}
              className="border border-gray-200 dark:border-[#3D3D3D] rounded-md p-4 flex flex-wrap items-center justify-between gap-4"
            >
              <div>
                <div className="font-medium text-black dark:text-white">
                  {source.display_name}
                </div>
                <div className="text-xs text-gray-500 mt-1">
                  {source.connection.site} ·{" "}
                  {source.sync.last_tested_at ? "Connection tested" : "Connection not tested"}
                  {" · "}
                  {scopeSummary(source)}
                </div>
                {source.sync.last_error &&
                  !tokenMissing(source.sync.last_error) && (
                  <div className="text-xs text-red-600 mt-1">{source.sync.last_error}</div>
                )}
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setEditing({
                      ...source,
                      connection: { ...source.connection, api_token: "" },
                    });
                    setIsNew(false);
                    resetEditor();
                  }}
                  disabled={!!busy}
                >
                  Edit
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => remove(source)}
                  disabled={!!busy}
                  aria-label={`Delete ${source.display_name}`}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {editing && (
        <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5 space-y-5">
          <h3 className="font-medium text-black dark:text-white">Connection details</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className={labelClass}>Source id</label>
              <Input
                value={editing.id}
                disabled={!isNew}
                onChange={(event) => patch({ id: event.target.value })}
                className={inputClass}
                placeholder="confluence-acme"
              />
            </div>
            <div>
              <label className={labelClass}>Display name</label>
              <Input
                value={editing.display_name}
                onChange={(event) => patch({ display_name: event.target.value })}
                className={inputClass}
                placeholder="Acme Confluence"
              />
            </div>
            <div>
              <label className={labelClass}>Site</label>
              <Input
                value={editing.connection.site}
                onChange={(event) =>
                  patch({
                    connection: { ...editing.connection, site: event.target.value },
                  })
                }
                className={inputClass}
                placeholder="acme.atlassian.net"
              />
            </div>
            <div>
              <label className={labelClass}>Atlassian email</label>
              <Input
                value={editing.connection.email}
                onChange={(event) =>
                  patch({
                    connection: { ...editing.connection, email: event.target.value },
                  })
                }
                className={inputClass}
                placeholder="you@acme.com"
              />
            </div>
            <div className="md:col-span-2">
              <label className={labelClass}>API token</label>
              <Input
                type="password"
                value={editing.connection.api_token || ""}
                onChange={(event) =>
                  patch({
                    connection: { ...editing.connection, api_token: event.target.value },
                  })
                }
                className={inputClass}
                placeholder={
                  isNew
                    ? "Atlassian API token"
                    : "Enter the API token again to test this connection"
                }
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Create one at id.atlassian.com → Security → API tokens. The
                token is kept in server memory only.
              </p>
            </div>
          </div>
          <div className="flex justify-end">
            <Button
              onClick={testConnection}
              disabled={
                !!busy ||
                !editing.id ||
                !editing.display_name ||
                !editing.connection.site ||
                !editing.connection.email ||
                !editing.connection.api_token
              }
            >
              {busy === "test" && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Test connection
            </Button>
          </div>
          {connectionFeedback && <ActionStatus feedback={connectionFeedback} />}

          {connectionVerified && (
            <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5">
              <h4 className="font-medium text-black dark:text-white">Content filters</h4>
              <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 mb-4">
                Leave spaces unselected to ingest every space this account can
                read.
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>Updated on or after</label>
                  <Input
                    type="date"
                    value={(editing.scope.updated_after || "").slice(0, 10)}
                    onChange={(event) =>
                      patchScope({
                        updated_after: event.target.value
                          ? `${event.target.value}T00:00:00Z`
                          : null,
                      })
                    }
                    className={inputClass}
                  />
                </div>
                <div>
                  <div className={labelClass}>Content types</div>
                  <div className="flex flex-wrap gap-x-5 gap-y-2 md:mt-3">
                    {contentTypes.map((type) => {
                      const selected = editing.scope.content_types || [];
                      return (
                        <label
                          key={type.value}
                          className="flex items-center gap-2 text-sm text-black dark:text-white"
                        >
                          <input
                            type="checkbox"
                            checked={selected.includes(type.value)}
                            onChange={(event) =>
                              patchScope({
                                content_types: event.target.checked
                                  ? [...selected, type.value]
                                  : selected.filter((value) => value !== type.value),
                              })
                            }
                          />
                          {type.label}
                        </label>
                      );
                    })}
                  </div>
                </div>
                <div className="md:col-span-2">
                  <div className="flex items-center justify-between mb-2">
                    <div className="text-sm font-medium text-black dark:text-white">
                      Spaces
                    </div>
                    <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
                      <span>
                        {selectedSpaceKeys.length
                          ? `${selectedSpaceKeys.length} selected`
                          : "All spaces"}
                      </span>
                      {selectedSpaceKeys.length > 0 && (
                        <button
                          type="button"
                          className="underline"
                          onClick={() => patchScope({ space_keys: [] })}
                        >
                          Clear
                        </button>
                      )}
                    </div>
                  </div>
                  {busy === "spaces" ? (
                    <div className="flex items-center gap-2 text-sm text-gray-500">
                      <Loader2 className="h-4 w-4 animate-spin" /> Loading spaces…
                    </div>
                  ) : spaces.length > 0 ? (
                    <>
                      <Input
                        value={spaceSearch}
                        onChange={(event) => setSpaceSearch(event.target.value)}
                        className={`${inputClass} mb-2`}
                        placeholder="Search by space key or name"
                        aria-label="Search visible Confluence spaces"
                      />
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-2 max-h-48 overflow-y-auto border border-gray-200 dark:border-[#3D3D3D] rounded-md p-3">
                        {filteredSpaces.map((space) => (
                          <label
                            key={space.key}
                            className="flex items-center gap-2 text-sm text-black dark:text-white"
                          >
                            <input
                              type="checkbox"
                              checked={selectedSpaceKeys.includes(space.key.toUpperCase())}
                              onChange={(event) => {
                                const key = space.key.toUpperCase();
                                patchScope({
                                  space_keys: event.target.checked
                                    ? [...selectedSpaceKeys, key]
                                    : selectedSpaceKeys.filter((value) => value !== key),
                                });
                              }}
                            />
                            <span className="truncate">
                              {space.key} — {space.name}
                            </span>
                          </label>
                        ))}
                        {filteredSpaces.length === 0 && (
                          <p className="text-sm text-gray-500 dark:text-gray-400 md:col-span-2">
                            No spaces match this search.
                          </p>
                        )}
                      </div>
                    </>
                  ) : null}
                  {spacesFeedback && (
                    <div className="mt-2">
                      <ActionStatus feedback={spacesFeedback} />
                    </div>
                  )}
                </div>
                <div className="md:col-span-2 flex justify-end">
                  <Button variant="outline" onClick={previewCount} disabled={!!busy}>
                    {busy === "count" && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                    Preview page count
                  </Button>
                </div>
                {countFeedback && (
                  <div className="md:col-span-2">
                    <ActionStatus feedback={countFeedback} />
                  </div>
                )}
              </div>
            </div>
          )}

          {saveFeedback && <ActionStatus feedback={saveFeedback} />}
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              onClick={() => {
                setEditing(null);
                resetEditor();
              }}
              disabled={!!busy}
            >
              Cancel
            </Button>
            {connectionVerified && (
              <Button onClick={save} disabled={!!busy}>
                {busy === "save" ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Save className="h-4 w-4 mr-2" />
                )}
                Save scope
              </Button>
            )}
          </div>
        </div>
      )}

      {sources.length > 0 && (
        <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 font-medium text-black dark:text-white">
                <ShieldCheck className="h-4 w-4" />
                2. Confluence graph schema
              </div>
              <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
                {schema?.install?.status === "queued" || schema?.install?.status === "running"
                  ? "Installing the Confluence schema. This keeps running if the laptop locks."
                  : schema?.status === "installed"
                  ? "Installed"
                  : schema?.status === "not_initialized"
                    ? "Initialize this graph before installing the Confluence schema."
                    : schema?.status === "conflict"
                      ? "Existing graph schema conflicts with the Confluence connector."
                      : schema?.status === "incomplete"
                        ? "The Confluence schema is incomplete."
                        : testedAndSaved
                          ? "Ready to install."
                          : "Test a saved connection before installing the schema."}
              </p>
            </div>
            {schema &&
              schema.status !== "installed" &&
              schema.status !== "conflict" &&
              schema.status !== "not_initialized" && (
                <Button onClick={installSchema} disabled={!!busy || !testedAndSaved}>
                  {busy === "schema" && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                  Install Confluence schema
                </Button>
              )}
          </div>
          {(schema?.conflicts?.length || 0) > 0 && (
            <ul className="mt-3 text-sm text-red-600 list-disc pl-5">
              {schema?.conflicts.map((conflict) => (
                <li key={conflict}>{conflict}</li>
              ))}
            </ul>
          )}
          {schemaFeedback && (
            <div className="mt-4">
              <ActionStatus feedback={schemaFeedback} />
            </div>
          )}
        </div>
      )}

      {sources.length > 0 && schema?.status === "installed" && (
        <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5">
          <h3 className="font-medium text-black dark:text-white">3. Ingest and build</h3>
          <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 mb-4">
            Import Confluence pages and blog posts into this graph. Jira issue
            keys and Zendesk tickets mentioned in a page are linked. Resume
            continues a failed ingest from the saved checkpoint.
          </p>
          {syncFeedback && (
            <div className="mb-4">
              <ActionStatus feedback={syncFeedback} />
            </div>
          )}
          <div className="space-y-3">
            {sources.map((source) => {
              const stoppedEarly = ingestStoppedEarly(source.sync);
              const needsResume = Boolean(
                (source.sync.last_error && !tokenMissing(source.sync.last_error)) ||
                source.sync.last_link_error ||
                (source.sync.failed_content_ids?.length ?? 0) > 0 ||
                (source.sync.pending_comment_content_ids?.length ?? 0) > 0 ||
                stoppedEarly
              );
              return (
              <div
                key={source.id}
                className="border border-gray-200 dark:border-[#3D3D3D] rounded-md p-4"
              >
                <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <div className="font-medium text-black dark:text-white">
                    {source.display_name}
                  </div>
                  <div className="text-xs text-gray-500 mt-1">
                    {source.sync_running ? (
                      <>
                        Ingesting · {source.sync.progress_checked || 0} checked ·{" "}
                        {source.sync.progress_written || 0} written
                      </>
                    ) : stoppedEarly ? (
                      <>Ingest stopped before it finished</>
                    ) : (
                      <>
                        Last ingestion: {formatDate(source.sync.last_completed_at)} · Pages:{" "}
                        {source.sync.last_page_count || 0}
                      </>
                    )}
                  </div>
                  {source.sync.last_error &&
                    !tokenMissing(source.sync.last_error) && (
                    <div className="text-xs text-red-600 mt-1">{source.sync.last_error}</div>
                  )}
                  {source.sync.last_link_error && (
                    <div className="text-xs text-amber-700 dark:text-amber-300 mt-1">
                      Page saved. Link write failed: {source.sync.last_link_error}
                    </div>
                  )}
                </div>
                <Button
                  size="sm"
                  onClick={() => sync(source, needsResume)}
                  disabled={
                    !!busy ||
                    source.sync_running ||
                    !source.enabled ||
                    !source.sync.last_tested_at
                  }
                >
                  {(busy === `sync:${source.id}` || source.sync_running) && (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  )}
                  {source.sync_running
                    ? "Ingesting"
                    : needsResume
                      ? "Resume"
                      : "Ingest now"}
                </Button>
                </div>
                {tokenPromptFor === source.id && (
                  <div className="mt-3">
                    <RestoreApiTokenCard
                      name={source.display_name}
                      detail={source.connection.site}
                      busy={busy === `token:${source.id}`}
                      onSave={async (token) => {
                        setBusy(`token:${source.id}`);
                        try {
                          await restoreToken(source, token);
                          setTokenPromptFor(null);
                        } finally {
                          setBusy("");
                        }
                      }}
                    />
                  </div>
                )}
              </div>
              );
            })}
          </div>
        </div>
      )}
      </DialogContent>
    </Dialog>
    </>
  );
};

export default ConfluenceSourcesCard;
