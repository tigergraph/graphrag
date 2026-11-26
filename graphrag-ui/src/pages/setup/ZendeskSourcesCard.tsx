import React, { useCallback, useEffect, useRef, useState } from "react";
import { pauseIdleTimer, pingIdleTimer, resumeIdleTimer } from "@/hooks/useIdleTimeout";
import { Loader2, Plus, Save, ShieldCheck, Ticket, Trash2 } from "lucide-react";
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
import type { JiraSchemaStatus, ZendeskSource } from "@/types/dataSources";
import ConnectorPromptGuide from "@/pages/setup/ConnectorPromptGuide";
import RestoreApiTokenCard from "@/pages/setup/RestoreApiTokenCard";

const editingSourceKey = (graph: string) =>
  `graphrag:zendesk-editing-source:${graph}`;
const syncFeedbackKey = (graph: string) =>
  `graphrag:zendesk-sync-feedback:${graph}`;

const zendeskStatuses = [
  { value: "new", label: "New" },
  { value: "open", label: "Open" },
  { value: "pending", label: "Pending" },
  { value: "hold", label: "On hold" },
  { value: "solved", label: "Solved" },
  { value: "closed", label: "Closed" },
] as const;

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

const emptySource = (): ZendeskSource => ({
  id: "",
  type: "zendesk",
  enabled: true,
  display_name: "",
  connection: { subdomain: "", email: "", api_token: "" },
  scope: {
    updated_after: null,
    statuses: [],
    include_comments: true,
    jira_integration_external_id: null,
  },
  sync: { overlap_seconds: 120, last_ticket_count: 0 },
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
  return fallback;
};

const ingestProgressText = (source: ZendeskSource) => {
  const checked = source.sync.progress_checked || 0;
  const written = source.sync.progress_written || 0;
  const ticket =
    source.sync.last_ticket_id != null
      ? ` · saved through ticket ${source.sync.last_ticket_id}`
      : "";
  return `Ingesting ${source.display_name} · ${checked} checked · ${written} written${ticket}`;
};

const ZendeskSourcesCard: React.FC<{ graph: string }> = ({ graph }) => {
  const [sources, setSources] = useState<ZendeskSource[]>([]);
  const [editing, setEditing] = useState<ZendeskSource | null>(null);
  const [connectionVerified, setConnectionVerified] = useState(false);
  const [schema, setSchema] = useState<JiraSchemaStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState<"success" | "error" | "">("");
  const [connectionFeedback, setConnectionFeedback] =
    useState<ActionFeedback | null>(null);
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

  const watchSchemaInstall = useCallback(async () => {
    const watchId = ++installWatchRef.current;
    installActiveRef.current = true;
    setBusy("schema");
    pauseIdleTimer();
    setSchemaFeedback({
      type: "pending",
      text: "Installing Zendesk schema. This keeps running if the laptop locks.",
    });
    let misses = 0;
    try {
      while (installWatchRef.current === watchId) {
        try {
          const response = await fetch(`/ui/${graph}/data-sources/zendesk/schema`, {
            headers: { Authorization: sessionStorage.getItem("auth") || "" },
          });
          const data = await safeJson(response);
          if (installWatchRef.current !== watchId) return;
          if (!response.ok) {
            const message = errorDetail(data, `Failed to inspect schema (${response.status})`);
            if (catalogBusy(message, response.status)) {
              misses = 0;
              setSchemaFeedback({
                type: "pending",
                text: "Installing Zendesk schema. The catalog is busy while the change runs.",
              });
            } else {
              throw new Error(message);
            }
          } else {
          pingIdleTimer();
          setSchema(data);
          const installStatus = data.install?.status;
          if (installStatus === "failed") {
            throw new Error(data.install?.error || "Zendesk schema installation failed.");
          }
          if (installStatus === "completed") {
            setSchemaFeedback({ type: "success", text: "Zendesk schema installed." });
            return;
          }
          if (installStatus === "queued" || installStatus === "running") {
            misses = 0;
          } else if (data.status === "installed") {
            setSchemaFeedback({ type: "success", text: "Zendesk schema installed." });
            return;
          } else {
            misses += 1;
            if (misses >= 5) {
              throw new Error(
                "The connection dropped before installation started. Install the Zendesk schema again."
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
    const response = await fetch(`/ui/${graph}/data-sources/zendesk/schema`, {
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
          data.install?.error || "Zendesk schema installation failed."
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
      const response = await fetch(`/ui/${graph}/data-sources/zendesk`, {
        headers: { Authorization: sessionStorage.getItem("auth") || "" },
      });
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      const loaded: ZendeskSource[] = (
        Array.isArray(data.sources) ? data.sources : []
      ).filter((source) => source?.type === "zendesk");
      setSources(loaded);
      const running = loaded.find((source) => source.sync_running);
      if (running) {
        setSyncFeedback({
          type: "pending",
          text: ingestProgressText(running),
        });
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
      if (!quiet) {
        setMessage(`Failed to load Zendesk sources: ${error.message}`);
        setMessageType("error");
      }
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [graph, loadSchema]);

  const restoreToken = async (source: ZendeskSource, token: string) => {
    const response = await fetch(
      `/ui/${graph}/data-sources/zendesk/${encodeURIComponent(source.id)}/test`,
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

  useEffect(() => {
    setEditing(null);
    setConnectionVerified(false);
    setConnectionFeedback(null);
    setSaveFeedback(null);
    setCountFeedback(null);
    setSchemaFeedback(null);
    loadSources();
  }, [loadSources]);

  const syncRunning = sources.some((source) => source.sync_running);
  useEffect(() => {
    if (!open || !syncRunning) return;
    const timer = window.setInterval(() => {
      void loadSources(true);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [open, syncRunning, loadSources]);

  const patch = (updates: Partial<ZendeskSource>) => {
    setEditing((current) => (current ? { ...current, ...updates } : current));
  };

  const testConnection = async () => {
    if (!editing) return;
    setBusy("test");
    setConnectionFeedback(null);
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/zendesk/${encodeURIComponent(editing.id)}/test`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify(editing),
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      const saved = data.source as ZendeskSource;
      setEditing({ ...saved, connection: { ...saved.connection, api_token: editing.connection.api_token } });
      setConnectionVerified(true);
      setConnectionFeedback({
        type: "success",
        text: data.link_error
          ? `Connected as ${data.account?.name || data.account?.display_name || "the Zendesk account"}. Jira link inventory: ${data.link_error}`
          : `Connected as ${data.account?.name || data.account?.display_name || "the Zendesk account"}. Jira link inventory is readable.`,
      });
      await loadSources();
      setEditing((current) => current);
    } catch (error: any) {
      setConnectionVerified(false);
      setConnectionFeedback({ type: "error", text: error.message });
    } finally {
      setBusy("");
    }
  };

  const save = async () => {
    if (!editing) return;
    setBusy("save");
    setSaveFeedback(null);
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/zendesk/${encodeURIComponent(editing.id)}`,
        {
          method: "PUT",
          headers: authHeaders(),
          body: JSON.stringify(editing),
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      setSaveFeedback({ type: "success", text: "Zendesk scope saved." });
      setEditing(null);
      setConnectionVerified(false);
      sessionStorage.setItem(editingSourceKey(graph), "__closed__");
      await loadSources();
    } catch (error: any) {
      setSaveFeedback({ type: "error", text: error.message });
    } finally {
      setBusy("");
    }
  };

  const previewCount = async () => {
    if (!editing) return;
    setBusy("count");
    setCountFeedback(null);
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/zendesk/${encodeURIComponent(editing.id)}/tickets/count`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify(editing.scope),
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      setCountFeedback({
        type: "success",
        text: `About ${data.count} ticket${data.count === 1 ? "" : "s"} match this scope.`,
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
      text: "Installing Zendesk schema. This keeps running if the laptop locks.",
    });
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/zendesk/schema/install`,
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
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
      await loadSources(true);
      const response = await fetch(
        `/ui/${graph}/data-sources/zendesk/${encodeURIComponent(sourceId)}/sync/${runId}`,
        { headers: { Authorization: sessionStorage.getItem("auth") || "" } }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      if (data.status === "completed") {
        const count = data.result?.tickets_upserted;
        setSyncFeedback({
          type: "success",
          text:
            typeof count === "number"
              ? `Ingested ${count} Zendesk ticket${count === 1 ? "" : "s"}.`
              : "Zendesk ingest completed.",
        });
        await loadSources();
        return;
      }
      if (data.status === "failed") {
        throw new Error(data.error || "Zendesk ingest failed.");
      }
    }
  };

  const tokenMissing = (message: string) =>
    message.toLowerCase().includes("api token is not available");

  const ingestStoppedEarly = (sync: ZendeskSource["sync"]) => {
    if (!sync.last_started_at || (!sync.checkpoint && sync.last_ticket_id == null)) {
      return false;
    }
    if (!sync.last_completed_at) return true;
    const started = Date.parse(sync.last_started_at);
    const completed = Date.parse(sync.last_completed_at);
    return Number.isFinite(started) && Number.isFinite(completed) && started > completed;
  };

  const sync = async (source: ZendeskSource, resume = false) => {
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
        `/ui/${graph}/data-sources/zendesk/${encodeURIComponent(source.id)}/sync`,
        { method: "POST", headers: authHeaders() }
      );
      const data = await safeJson(response);
      if (!response.ok) {
        const message = errorDetail(data, `HTTP ${response.status}`);
        if (message.includes(`zendesk_sync:${source.id}`)) {
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
        throw new Error(message);
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

  const remove = async (source: ZendeskSource) => {
    setBusy(`delete:${source.id}`);
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/zendesk/${encodeURIComponent(source.id)}`,
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
      setMessageType("error");
    } finally {
      setBusy("");
    }
  };

  const testedAndSaved = sources.some((source) => source.sync.last_tested_at);
  const summary = !sources.length
    ? "No connections yet"
    : schema?.status === "installed"
      ? `${sources.length} connection${sources.length === 1 ? "" : "s"} · Schema installed`
      : `${sources.length} connection${sources.length === 1 ? "" : "s"} · Schema not installed`;

  return (
    <>
    <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
      <div className="mb-4">
        <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
          <Ticket className="h-6 w-6 text-tigerOrange" />
        </div>
        <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">
          Zendesk
        </h2>
        <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
          Install the Zendesk schema and ingest tickets. Use it on its own or
          on the same graph as Jira.
        </p>
        <p className="text-sm text-black dark:text-white">{summary}</p>
      </div>
      <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
        <Button
          onClick={() => setOpen(true)}
          className="gradient w-full text-white"
        >
          <Ticket className="h-4 w-4 mr-2" />
          Set up Zendesk
        </Button>
      </div>
    </div>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        className="sm:max-w-4xl max-h-[90vh] overflow-y-auto bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]"
        onInteractOutside={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle className="text-black dark:text-white">Zendesk</DialogTitle>
          <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
            Connect Zendesk, install its schema, then ingest tickets into this
            graph.
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-end">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setEditing(emptySource());
              setConnectionVerified(false);
              setConnectionFeedback(null);
              setSaveFeedback(null);
              setCountFeedback(null);
            }}
            disabled={!!busy || !!editing}
          >
            <Plus className="h-4 w-4 mr-2" /> Add connection
          </Button>
        </div>
      <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-4 text-sm text-amber-800 dark:text-amber-200">
        Graph users can retrieve every ticket visible to the Zendesk account
        used by this connector.
      </div>

      {message && (
        <div
          className={`p-3 rounded-md text-sm border ${
            messageType === "error"
              ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 border-red-200 dark:border-red-800"
              : "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 border-green-200 dark:border-green-800"
          }`}
        >
          {message}
        </div>
      )}

      <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5">
        <h3 className="font-medium text-black dark:text-white">
          1. Connection and scope
        </h3>
        <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 mb-4">
          {loading
            ? "Loading Zendesk sources..."
            : sources.length
              ? `${sources.length} configured source${sources.length === 1 ? "" : "s"}`
              : "No Zendesk sources configured for this graph."}
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
                  {source.connection.subdomain}.zendesk.com ·{" "}
                  {source.sync.last_tested_at ? "Connection tested" : "Connection not tested"}
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
                    setEditing(source);
                    setConnectionVerified(false);
                    setConnectionFeedback(null);
                    setSaveFeedback(null);
                    setCountFeedback(null);
                    sessionStorage.setItem(editingSourceKey(graph), source.id);
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
                disabled={sources.some((source) => source.id === editing.id)}
                onChange={(event) => patch({ id: event.target.value })}
                className={inputClass}
                placeholder="zendesk-acme"
              />
            </div>
            <div>
              <label className={labelClass}>Display name</label>
              <Input
                value={editing.display_name}
                onChange={(event) => patch({ display_name: event.target.value })}
                className={inputClass}
                placeholder="Acme Zendesk"
              />
            </div>
            <div>
              <label className={labelClass}>Subdomain</label>
              <Input
                value={editing.connection.subdomain}
                onChange={(event) =>
                  patch({
                    connection: { ...editing.connection, subdomain: event.target.value },
                  })
                }
                className={inputClass}
                placeholder="acme"
              />
            </div>
            <div>
              <label className={labelClass}>Email</label>
              <Input
                value={editing.connection.email}
                onChange={(event) =>
                  patch({
                    connection: { ...editing.connection, email: event.target.value },
                  })
                }
                className={inputClass}
                placeholder="agent@acme.com"
              />
            </div>
            <div className="md:col-span-2">
              <label className={labelClass}>Jira integration external id</label>
              <Input
                value={editing.scope.jira_integration_external_id || ""}
                onChange={(event) =>
                  patch({
                    scope: {
                      ...editing.scope,
                      jira_integration_external_id: event.target.value || null,
                    },
                  })
                }
                className={inputClass}
                placeholder="Optional. Admin Center → Apps and integrations → Integrations → Jira → Edit"
              />
            </div>
            <div className="md:col-span-2">
              <label className={labelClass}>API token</label>
              <Input
                type="password"
                value={editing.connection.api_token}
                onChange={(event) =>
                  patch({
                    connection: { ...editing.connection, api_token: event.target.value },
                  })
                }
                className={inputClass}
                placeholder="Zendesk API token"
              />
            </div>
          </div>
          <div className="flex justify-end">
            <Button onClick={testConnection} disabled={!!busy || !editing.id || !editing.display_name}>
              {busy === "test" && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Test connection
            </Button>
          </div>
          {connectionFeedback && <ActionStatus feedback={connectionFeedback} />}

          {connectionVerified && (
            <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5">
              <h4 className="font-medium text-black dark:text-white">Ticket filters</h4>
              <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 mb-4">
                Leave the filters empty to ingest every ticket this account can read.
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>Updated on or after</label>
                  <Input
                    type="date"
                    value={editing.scope.updated_after || ""}
                    onChange={(event) =>
                      patch({
                        scope: {
                          ...editing.scope,
                          updated_after: event.target.value || null,
                        },
                      })
                    }
                    className={inputClass}
                  />
                </div>
                <label className="flex items-center gap-2 text-sm text-black dark:text-white md:mt-8">
                  <input
                    type="checkbox"
                    checked={editing.scope.include_comments}
                    onChange={(event) =>
                      patch({
                        scope: {
                          ...editing.scope,
                          include_comments: event.target.checked,
                        },
                      })
                    }
                  />
                  Include comments
                </label>
                <div className="md:col-span-2">
                  <div className={labelClass}>Statuses</div>
                  <div className="flex flex-wrap gap-x-5 gap-y-2">
                    {zendeskStatuses.map((status) => {
                      const selected = editing.scope.statuses || [];
                      return (
                        <label
                          key={status.value}
                          className="flex items-center gap-2 text-sm text-black dark:text-white"
                        >
                          <input
                            type="checkbox"
                            checked={selected.includes(status.value)}
                            onChange={(event) =>
                              patch({
                                scope: {
                                  ...editing.scope,
                                  statuses: event.target.checked
                                    ? [...selected, status.value]
                                    : selected.filter((value) => value !== status.value),
                                },
                              })
                            }
                          />
                          {status.label}
                        </label>
                      );
                    })}
                  </div>
                </div>
                <div className="md:col-span-2 flex justify-end">
                  <Button variant="outline" onClick={previewCount} disabled={!!busy}>
                    {busy === "count" && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                    Preview ticket count
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
                setConnectionVerified(false);
                sessionStorage.setItem(editingSourceKey(graph), "__closed__");
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
                2. Zendesk graph schema
              </div>
              <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
                {schema?.install?.status === "queued" || schema?.install?.status === "running"
                  ? "Installing the Zendesk schema. This keeps running if the laptop locks."
                  : schema?.status === "installed"
                  ? "Installed"
                  : schema?.status === "not_initialized"
                    ? "Initialize this graph before installing the Zendesk schema."
                    : schema?.status === "conflict"
                      ? "Existing graph schema conflicts with the Zendesk connector."
                      : schema?.status === "incomplete"
                        ? "The Zendesk schema is incomplete."
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
                  Install Zendesk schema
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
            Import Zendesk tickets into this graph. A Jira issue that is not
            here yet is created with its issue key so the link can be stored.
            Resume continues a failed ingest from the saved cursor.
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
                (source.sync.failed_ticket_ids?.length ?? 0) > 0 ||
                (source.sync.pending_comment_ticket_ids?.length ?? 0) > 0 ||
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
                        {source.sync.last_ticket_id != null
                          ? ` · saved through ticket ${source.sync.last_ticket_id}`
                          : ""}
                      </>
                    ) : stoppedEarly ? (
                      <>
                        Ingest stopped before it finished
                        {source.sync.last_ticket_id != null
                          ? ` · saved through ticket ${source.sync.last_ticket_id}`
                          : ""}
                      </>
                    ) : (
                      <>
                        Last ingestion: {source.sync.last_completed_at || "Never"} · Tickets:{" "}
                        {source.sync.last_ticket_count || 0}
                      </>
                    )}
                  </div>
                  {source.sync.last_error &&
                    !tokenMissing(source.sync.last_error) && (
                    <div className="text-xs text-red-600 mt-1">{source.sync.last_error}</div>
                  )}
                  {source.sync.last_link_error && (
                    <div className="text-xs text-red-600 mt-1">{source.sync.last_link_error}</div>
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
                      detail={`${source.connection.subdomain}.zendesk.com`}
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
          <ConnectorPromptGuide connectorType="zendesk" />
        </div>
      )}
      </DialogContent>
    </Dialog>
    </>
  );
};

export default ZendeskSourcesCard;
