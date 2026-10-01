import React, { useCallback, useEffect, useState } from "react";
import {
  DatabaseZap,
  Loader2,
  Plus,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import ConfigScopeToggle from "@/components/ConfigScopeToggle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { safeJson } from "@/utils/safeJson";
import { pauseIdleTimer, pingIdleTimer, resumeIdleTimer } from "@/hooks/useIdleTimeout";
import type { JiraSchemaStatus, JiraSource } from "@/types/dataSources";

const editingSourceKey = (graph: string) =>
  `graphrag:jira-editing-source:${graph}`;

const syncFeedbackKey = (graph: string) =>
  `graphrag:jira-sync-feedback:${graph}`;

interface JiraProject {
  id: string;
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

const emptySource = (): JiraSource => ({
  id: "",
  type: "jira_cloud",
  enabled: true,
  display_name: "",
  connection: { site_url: "", email: "", api_token: "" },
  scope: {
    project_keys: [],
    created_after: null,
    updated_after: null,
    status_categories: [],
    jql_extra: "",
    include_comments: true,
  },
  sync: { overlap_seconds: 120, last_issue_count: 0 },
});

const labelClass = "block text-sm font-medium mb-2 text-black dark:text-white";
const inputClass = "dark:border-[#3D3D3D] dark:bg-background";
const jiraStatusCategories = [
  { value: "new", label: "To do" },
  { value: "indeterminate", label: "In progress" },
  { value: "done", label: "Done" },
] as const;

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

const DataSourcesConfig: React.FC = () => {
  const [selectedGraph, setSelectedGraph] = useState(
    sessionStorage.getItem("selectedGraph") || ""
  );
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);
  const [sources, setSources] = useState<JiraSource[]>([]);
  const [editing, setEditing] = useState<JiraSource | null>(null);
  const [connectionVerified, setConnectionVerified] = useState(false);
  const [projectSelectionLoaded, setProjectSelectionLoaded] = useState(false);
  const [projects, setProjects] = useState<JiraProject[]>([]);
  const [directProjectKey, setDirectProjectKey] = useState("");
  const [projectSearch, setProjectSearch] = useState("");
  const [schema, setSchema] = useState<JiraSchemaStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState<"success" | "error" | "">("");
  const [connectionFeedback, setConnectionFeedback] =
    useState<ActionFeedback | null>(null);
  const [scopeFeedback, setScopeFeedback] =
    useState<ActionFeedback | null>(null);
  const [saveFeedback, setSaveFeedback] =
    useState<ActionFeedback | null>(null);
  const [countFeedback, setCountFeedback] =
    useState<ActionFeedback | null>(null);
  const [sourceFeedback, setSourceFeedback] =
    useState<ActionFeedback | null>(null);
  const [schemaFeedback, setSchemaFeedback] =
    useState<ActionFeedback | null>(null);
  const [syncFeedback, setSyncFeedbackRaw] =
    useState<ActionFeedback | null>(null);

  const setSyncFeedback = (
    feedback: ActionFeedback | null,
    graph?: string
  ) => {
    const key = syncFeedbackKey(graph ?? selectedGraph);
    if (feedback) {
      sessionStorage.setItem(key, JSON.stringify(feedback));
    } else {
      sessionStorage.removeItem(key);
    }
    setSyncFeedbackRaw(feedback);
  };

  useEffect(() => {
    const creds = sessionStorage.getItem("auth");
    if (!creds) return;
    fetch("/ui/list_graphs", { headers: { Authorization: creds } })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        const graphs = Array.isArray(data?.graphs)
          ? data.graphs
          : Array.isArray(data)
            ? data
            : [];
        setAvailableGraphs(graphs);
        if (!selectedGraph && graphs.length) setSelectedGraph(graphs[0]);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const syncSelectedGraph = () =>
      setSelectedGraph(sessionStorage.getItem("selectedGraph") || "");
    window.addEventListener("graphrag:selectedGraph", syncSelectedGraph);
    return () =>
      window.removeEventListener("graphrag:selectedGraph", syncSelectedGraph);
  }, []);

  const loadSources = useCallback(async () => {
    if (!selectedGraph) {
      setSources([]);
      return;
    }
    setLoading(true);
    setMessage("");
    try {
      const response = await fetch(`/ui/${selectedGraph}/data-sources`, {
        headers: { Authorization: sessionStorage.getItem("auth")! },
      });
      const data = await safeJson(response);
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      const loadedSources: JiraSource[] = Array.isArray(data.sources)
        ? data.sources
        : [];
      setSources(loadedSources);
      setEditing((current) => {
        if (current) return current;
        const rememberedId = sessionStorage.getItem(
          editingSourceKey(selectedGraph)
        );
        if (rememberedId === "__closed__") return null;
        const remembered = loadedSources.find(
          (source) => source.id === rememberedId
        );
        return remembered || (loadedSources.length === 1 ? loadedSources[0] : null);
      });
    } catch (error: any) {
      setMessage(`Failed to load data sources: ${error.message}`);
      setMessageType("error");
    } finally {
      setLoading(false);
    }
  }, [selectedGraph]);

  const loadSchema = useCallback(async () => {
    if (!selectedGraph) {
      setSchema(null);
      return false;
    }
    try {
      const response = await fetch(
        `/ui/${selectedGraph}/data-sources/jira/schema`,
        { headers: { Authorization: sessionStorage.getItem("auth")! } }
      );
      const data = await safeJson(response);
      if (!response.ok) {
        throw new Error(
          errorDetail(data, `Failed to inspect schema (${response.status})`)
        );
      }
      setSchema(data);
      return true;
    } catch (error: any) {
      setSchemaFeedback({
        type: "error",
        text: `Failed to inspect Jira schema: ${error.message}`,
      });
      return false;
    }
  }, [selectedGraph]);

  useEffect(() => {
    setEditing(null);
    setConnectionVerified(false);
    setProjectSelectionLoaded(false);
    setProjects([]);
    setDirectProjectKey("");
    setProjectSearch("");
    setConnectionFeedback(null);
    setScopeFeedback(null);
    setSaveFeedback(null);
    setCountFeedback(null);
    setSourceFeedback(null);
    setSchemaFeedback(null);
    // Restore persisted sync feedback for this graph so navigation doesn't clear it.
    const stored = sessionStorage.getItem(syncFeedbackKey(selectedGraph));
    setSyncFeedbackRaw(stored ? (JSON.parse(stored) as ActionFeedback) : null);
    loadSources();
    loadSchema();
  }, [loadSchema, loadSources]);

  const patch = (value: Partial<JiraSource>) => {
    setConnectionFeedback(null);
    setScopeFeedback(null);
    setSaveFeedback(null);
    setCountFeedback(null);
    setEditing((current) => (current ? { ...current, ...value } : current));
  };

  const patchConnectionDetails = (value: Partial<JiraSource>) => {
    setConnectionVerified(false);
    setProjectSelectionLoaded(false);
    setProjects([]);
    setProjectSearch("");
    patch(value);
  };

  const validateConnection = (
    source: JiraSource,
    requireToken = true
  ) => {
    const missingFields = [
      !source.id.trim() && "source id",
      !source.display_name.trim() && "display name",
      !source.connection.site_url.trim() && "site URL",
      !source.connection.email.trim() && "Atlassian email",
      requireToken && !source.connection.api_token.trim() && "API token",
    ].filter(Boolean) as string[];

    if (missingFields.length > 0) {
      const fields =
        missingFields.length === 1
          ? missingFields[0]
          : `${missingFields.slice(0, -1).join(", ")} and ${
              missingFields[missingFields.length - 1]
            }`;
      setConnectionFeedback({
        type: "error",
        text: `${
          fields.charAt(0).toUpperCase() + fields.slice(1)
        } ${missingFields.length === 1 ? "is" : "are"} required.`,
      });
      return false;
    }
    return true;
  };

  const persistSource = async (source: JiraSource) => {
    const response = await fetch(
      `/ui/${selectedGraph}/data-sources/${encodeURIComponent(source.id)}`,
      {
        method: "PUT",
        headers: {
          Authorization: sessionStorage.getItem("auth")!,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(source),
      }
    );
    const data = await safeJson(response);
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data.source as JiraSource;
  };

  const save = async () => {
    if (
      !editing ||
      !selectedGraph ||
      !connectionVerified ||
      !validateConnection(editing, false)
    ) {
      return;
    }
    if (editing.scope.project_keys.length === 0) {
      setSaveFeedback({
        type: "error",
        text: "Select at least one Jira project before saving the scope.",
      });
      return;
    }
    setBusy("save");
    setSaveFeedback({
      type: "pending",
      text: "Saving Jira project scope…",
    });
    try {
      const saved = await persistSource(editing);
      setEditing(saved);
      sessionStorage.setItem(editingSourceKey(selectedGraph), saved.id);
      await loadSources();
      setSaveFeedback({
        type: "success",
        text: `Project scope saved (${saved.scope.project_keys.length} selected).`,
      });
    } catch (error: any) {
      setSaveFeedback({
        type: "error",
        text: `Failed to save project scope: ${error.message}`,
      });
    } finally {
      setBusy("");
    }
  };

  const test = async () => {
    if (!editing || !selectedGraph) return;
    setConnectionVerified(false);
    setProjectSelectionLoaded(false);
    setProjects([]);
    setProjectSearch("");
    setCountFeedback(null);
    setSaveFeedback(null);
    if (!validateConnection(editing)) return;
    setScopeFeedback(null);
    setBusy("test");
    setConnectionFeedback({
      type: "pending",
      text: "Testing Jira connection…",
    });
    try {
      const response = await fetch(
        `/ui/${selectedGraph}/data-sources/${encodeURIComponent(editing.id)}/test`,
        {
          method: "POST",
          headers: {
            Authorization: sessionStorage.getItem("auth")!,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(editing),
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      setConnectionFeedback({
        type: "success",
        text: `Connected as ${data.account?.display_name || "Jira user"}.`,
      });
      setConnectionVerified(true);
      setEditing(data.source);
      sessionStorage.setItem(editingSourceKey(selectedGraph), data.source.id);
      await loadSources();
    } catch (error: any) {
      setConnectionVerified(false);
      setConnectionFeedback({
        type: "error",
        text: `Connection failed: ${error.message}`,
      });
    } finally {
      setBusy("");
    }
  };

  const loadProjects = async () => {
    if (!editing || !connectionVerified) return;
    const projectKey = directProjectKey.trim().toUpperCase();
    setProjectSelectionLoaded(false);
    setProjects([]);
    setProjectSearch("");
    setCountFeedback(null);
    setBusy("projects");
    setScopeFeedback({
      type: "pending",
      text: projectKey
        ? `Loading Jira project ${projectKey}…`
        : "Loading visible Jira projects…",
    });
    try {
      const query = projectKey
        ? `?project_key=${encodeURIComponent(projectKey)}`
        : "";
      const response = await fetch(
        `/ui/${selectedGraph}/data-sources/${encodeURIComponent(editing.id)}/projects${query}`,
        { headers: { Authorization: sessionStorage.getItem("auth")! } }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      const visibleProjects = Array.isArray(data.projects) ? data.projects : [];
      setProjects(visibleProjects);
      setProjectSearch("");
      setProjectSelectionLoaded(true);
      if (projectKey && visibleProjects.length === 1) {
        setEditing((current) =>
          current
            ? {
                ...current,
                scope: {
                  ...current.scope,
                  project_keys: [visibleProjects[0].key],
                },
              }
            : current
        );
      }
      setScopeFeedback({
        type: "success",
        text:
          visibleProjects.length > 0
            ? projectKey
              ? `Loaded and selected ${visibleProjects[0].key} — ${visibleProjects[0].name}.`
              : `Loaded ${visibleProjects.length} visible Jira projects.`
            : "Connection succeeded, but this account has no visible Jira projects.",
      });
    } catch (error: any) {
      setProjectSelectionLoaded(false);
      setScopeFeedback({
        type: "error",
        text: `Failed to load projects: ${error.message}`,
      });
    } finally {
      setBusy("");
    }
  };

  const previewTicketCount = async () => {
    if (
      !editing ||
      !connectionVerified ||
      editing.scope.project_keys.length === 0
    ) {
      setCountFeedback({
        type: "error",
        text: "Select at least one Jira project before previewing tickets.",
      });
      return;
    }
    setBusy("count");
    setCountFeedback({
      type: "pending",
      text: "Checking how many Jira tickets match this scope…",
    });
    try {
      const response = await fetch(
        `/ui/${selectedGraph}/data-sources/${encodeURIComponent(editing.id)}/issues/count`,
        {
          method: "POST",
          headers: {
            Authorization: sessionStorage.getItem("auth")!,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(editing.scope),
        }
      );
      const data = await safeJson(response);
      if (!response.ok) {
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      const count = Number(data.count || 0);
      setCountFeedback({
        type: "success",
        text: `Approximately ${count.toLocaleString()} Jira tickets match this scope.`,
      });
    } catch (error: any) {
      setCountFeedback({
        type: "error",
        text: `Failed to preview ticket count: ${error.message}`,
      });
    } finally {
      setBusy("");
    }
  };

  const installSchema = async () => {
    if (!selectedGraph) return;
    setBusy("schema");
    setSchemaFeedback({
      type: "pending",
      text: "Installing Jira schema…",
    });
    try {
      const response = await fetch(
        `/ui/${selectedGraph}/data-sources/jira/schema/install`,
        {
          method: "POST",
          headers: { Authorization: sessionStorage.getItem("auth")! },
        }
      );
      const data = await safeJson(response);
      if (!response.ok) {
        throw new Error(
          errorDetail(data, `Schema installation failed (${response.status})`)
        );
      }
      setSchema(data.schema);
      setSchemaFeedback({
        type: "success",
        text: "Jira schema installed.",
      });
    } catch (error: any) {
      setSchemaFeedback({
        type: "error",
        text: `Schema installation failed: ${error.message}`,
      });
    } finally {
      setBusy("");
    }
  };

  const pollSync = async (sourceId: string, runId: string) => {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      const response = await fetch(
        `/ui/${selectedGraph}/data-sources/${encodeURIComponent(sourceId)}/sync/${runId}`,
        { headers: { Authorization: sessionStorage.getItem("auth")! } }
      );
      const data = await safeJson(response);
      if (!response.ok) {
        throw new Error(
          errorDetail(data, `Sync status failed (${response.status})`)
        );
      }
      pingIdleTimer();
      if (data.status === "failed") {
        throw new Error(data.error || "Jira synchronization failed");
      }
      if (data.status === "completed") return data.result;
    }
  };

  const sync = async (source: JiraSource) => {
    setBusy(`sync:${source.id}`);
    setSources((current) =>
      current.map((item) =>
        item.id === source.id
          ? { ...item, sync: { ...item.sync, last_error: null } }
          : item
      )
    );
    setSyncFeedback({
      type: "pending",
      text: `Ingesting ${source.display_name} into the graph…`,
    });
    pauseIdleTimer();
    try {
      const response = await fetch(
        `/ui/${selectedGraph}/data-sources/${encodeURIComponent(source.id)}/sync`,
        {
          method: "POST",
          headers: { Authorization: sessionStorage.getItem("auth")! },
        }
      );
      const data = await safeJson(response);
      if (!response.ok) {
        throw new Error(
          errorDetail(data, `Jira synchronization failed (${response.status})`)
        );
      }
      const result = await pollSync(source.id, data.run_id);
      let buildStarted = false;
      const missingChunkEmbeddings = Number(
        result.missing_chunk_embeddings || 0
      );
      if (result.rebuild_required) {
        setSyncFeedback({
          type: "pending",
          text:
            missingChunkEmbeddings > 0 && result.documents_loaded === 0
              ? `No Jira changes detected, but ${missingChunkEmbeddings} graph chunks are missing embeddings. Starting a recovery build…`
              : "Jira ingestion complete. Starting the GraphRAG build…",
        });
        const rebuild = await fetch(`/ui/${selectedGraph}/rebuild_graph`, {
          method: "POST",
          headers: { Authorization: sessionStorage.getItem("auth")! },
        });
        const rebuildData = await safeJson(rebuild);
        if (!rebuild.ok) {
          throw new Error(
            errorDetail(
              rebuildData,
              "Jira data was ingested, but the GraphRAG build failed to start."
            )
          );
        }
        buildStarted = true;

        // Poll rebuild_status and show live ECC progress on the Data Sources
        // page, identical to the progress bar shown on the KGAdmin page.
        const creds = sessionStorage.getItem("auth")!;
        const baseMsg =
          `Jira ingestion complete: ${result.issues_upserted} issues updated, ` +
          `${result.issues_deleted || 0} removed, and ` +
          `${result.documents_loaded} changed documents loaded. `;
        let pollDone = false;
        while (!pollDone) {
          await new Promise((r) => setTimeout(r, 3000));
          try {
            const statusResp = await fetch(
              `/ui/${selectedGraph}/rebuild_status`,
              { headers: { Authorization: creds } }
            );
            if (!statusResp.ok) break;
            const statusData = await statusResp.json();
            if (statusData.is_running) {
              const stage = statusData.stage ? ` — ${statusData.stage}` : " — Building…";
              setSyncFeedback({
                type: "pending",
                text: baseMsg + `GraphRAG build in progress${stage}`,
              });
            } else {
              pollDone = true;
            }
          } catch {
            break;
          }
        }
      }
      await loadSources();
      setSyncFeedback({
        type: "success",
        text:
          `Jira ingestion complete: ${result.issues_upserted} issues updated, ` +
          `${result.issues_deleted || 0} removed, and ` +
          `${result.documents_loaded} changed documents loaded.` +
          (buildStarted
            ? missingChunkEmbeddings > 0
              ? ` GraphRAG recovery build complete.`
              : " GraphRAG build for chunking and embedding complete."
            : " No Jira changes were detected, so no new build was started."),
      });
    } catch (error: any) {
      setSyncFeedback({
        type: "error",
        text: `Jira ingestion failed: ${error.message}`,
      });
    } finally {
      resumeIdleTimer();
      setBusy("");
    }
  };

  const remove = async (source: JiraSource) => {
    if (!window.confirm(`Remove data source "${source.display_name}"?`)) return;
    setBusy(`delete:${source.id}`);
    setSourceFeedback({
      type: "pending",
      text: `Removing ${source.display_name}…`,
    });
    try {
      const response = await fetch(
        `/ui/${selectedGraph}/data-sources/${encodeURIComponent(source.id)}`,
        {
          method: "DELETE",
          headers: { Authorization: sessionStorage.getItem("auth")! },
        }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      if (editing?.id === source.id) {
        setEditing(null);
        sessionStorage.removeItem(editingSourceKey(selectedGraph));
      }
      await loadSources();
      setSourceFeedback({
        type: "success",
        text: `${source.display_name} was removed.`,
      });
    } catch (error: any) {
      setSourceFeedback({
        type: "error",
        text: `Delete failed: ${error.message}`,
      });
    } finally {
      setBusy("");
    }
  };

  const normalizedProjectSearch = projectSearch.trim().toLowerCase();
  const filteredProjects = normalizedProjectSearch
    ? projects.filter(
        (project) =>
          project.key.toLowerCase().includes(normalizedProjectSearch) ||
          project.name.toLowerCase().includes(normalizedProjectSearch)
      )
    : projects;

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center gap-4 mb-6">
        <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center">
          <DatabaseZap className="h-6 w-6 text-tigerOrange" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-black dark:text-white">
            Data sources
          </h1>
          <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
            Connect, configure, ingest, and monitor external sources for the
            selected knowledge graph.
          </p>
        </div>
      </div>

      <ConfigScopeToggle
        graphOnly
        configScope="graph"
        selectedGraph={selectedGraph}
        availableGraphs={availableGraphs}
        onScopeChange={() => {}}
        onGraphChange={(graph) => {
          setSelectedGraph(graph);
          sessionStorage.setItem("selectedGraph", graph);
          window.dispatchEvent(new Event("graphrag:selectedGraph"));
        }}
      />

      {message && (
        <div
          className={`mb-4 p-3 rounded-md text-sm border ${
            messageType === "success"
              ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 border-green-200 dark:border-green-800"
              : messageType === "error"
                ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 border-red-200 dark:border-red-800"
                : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300 border-blue-200 dark:border-blue-800"
          }`}
        >
          {message}
        </div>
      )}

      {selectedGraph && (
        <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 space-y-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-lg bg-blue-50 dark:bg-blue-900/20 flex items-center justify-center">
                <DatabaseZap className="h-5 w-5 text-blue-600 dark:text-blue-400" />
              </div>
              <div>
                <h2 className="text-lg font-semibold text-black dark:text-white">
                  Jira Cloud
                </h2>
                <p className="text-sm text-gray-600 dark:text-gray-400">
                  Connect Jira, select projects, install its graph schema, then
                  ingest issues and build GraphRAG.
                </p>
              </div>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setEditing(emptySource());
                setConnectionVerified(false);
                setProjectSelectionLoaded(false);
                setProjects([]);
                setDirectProjectKey("");
                setProjectSearch("");
                setConnectionFeedback(null);
                setScopeFeedback(null);
                setSaveFeedback(null);
                setCountFeedback(null);
              }}
              disabled={!!busy || !!editing}
            >
              <Plus className="h-4 w-4 mr-2" /> Add connection
            </Button>
          </div>

          <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-4 text-sm text-amber-800 dark:text-amber-200">
            Until per-user Jira ACL enforcement is enabled, graph users can
            retrieve every selected-project issue visible to the connector account.
          </div>

          <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5">
            {sourceFeedback && (
              <div className="mb-4">
                <ActionStatus feedback={sourceFeedback} />
              </div>
            )}
            <div className="flex justify-between items-center mb-4">
              <div>
                <h3 className="font-medium text-black dark:text-white">
                  1. Connection and scope
                </h3>
                <span className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                {loading ? "Loading…" : `${sources.length} configured source${sources.length === 1 ? "" : "s"}`}
                </span>
              </div>
            </div>
            {sources.length === 0 && !loading && (
              <p className="text-sm text-gray-600 dark:text-gray-400">
                No Jira sources configured for this graph.
              </p>
            )}
            <div className="space-y-3">
              {sources.map((source) => (
                <div
                  key={source.id}
                  className="border border-gray-200 dark:border-[#3D3D3D] rounded-md p-4 flex justify-between gap-4"
                >
                  <div>
                    <div className="font-medium text-black dark:text-white">
                      {source.display_name}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400">
                      {source.connection.site_url}
                      {source.scope.project_keys.length > 0 &&
                        ` · ${source.scope.project_keys.join(", ")}`}
                    </div>
                    <div className="text-xs text-gray-500 mt-1">
                      <span
                        className={
                          source.sync.last_tested_at
                            ? "text-green-600 dark:text-green-400"
                            : "text-amber-600 dark:text-amber-400"
                        }
                      >
                        {source.sync.last_tested_at
                          ? "Connection tested"
                          : "Connection not tested"}
                      </span>
                    </div>
                    {source.sync.last_error && (
                      <div className="text-xs text-red-600 mt-1">{source.sync.last_error}</div>
                    )}
                  </div>
                  <div className="flex gap-2 items-start">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setEditing(source);
                        setConnectionVerified(false);
                        setProjectSelectionLoaded(false);
                        setProjects([]);
                        setDirectProjectKey("");
                        setProjectSearch("");
                        setConnectionFeedback(null);
                        setScopeFeedback(null);
                        setSaveFeedback(null);
                        setCountFeedback(null);
                        sessionStorage.setItem(
                          editingSourceKey(selectedGraph),
                          source.id
                        );
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
              <h3 className="font-medium text-black dark:text-white">
                Connection details
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>Source id</label>
                  <Input
                    value={editing.id}
                    disabled={sources.some((source) => source.id === editing.id)}
                    onChange={(event) =>
                      patchConnectionDetails({ id: event.target.value })
                    }
                    className={inputClass}
                    placeholder="jira-acme"
                  />
                </div>
                <div>
                  <label className={labelClass}>Display name</label>
                  <Input
                    value={editing.display_name}
                    onChange={(event) => patch({ display_name: event.target.value })}
                    className={inputClass}
                    placeholder="Acme Jira"
                  />
                </div>
                <div>
                  <label className={labelClass}>Site URL</label>
                  <Input
                    value={editing.connection.site_url}
                    onChange={(event) =>
                      patchConnectionDetails({
                        connection: {
                          ...editing.connection,
                          site_url: event.target.value,
                        },
                      })
                    }
                    className={inputClass}
                    placeholder="https://your-company.atlassian.net"
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Jira tenant base URL—not graphql.atlassian.net or an issue URL.
                  </p>
                </div>
                <div>
                  <label className={labelClass}>Atlassian account email</label>
                  <Input
                    value={editing.connection.email}
                    onChange={(event) =>
                      patchConnectionDetails({
                        connection: {
                          ...editing.connection,
                          email: event.target.value,
                        },
                      })
                    }
                    className={inputClass}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Email address associated with the API token.
                  </p>
                </div>
                <div className="md:col-span-2">
                  <label className={labelClass}>API token</label>
                  <Input
                    type="password"
                    value={editing.connection.api_token}
                    onChange={(event) =>
                      patchConnectionDetails({
                        connection: {
                          ...editing.connection,
                          api_token: event.target.value,
                        },
                      })
                    }
                    className={inputClass}
                    placeholder="Required each time you test the connection"
                  />
                </div>
                <div className="md:col-span-2">
                  <label className={labelClass}>Project key (optional)</label>
                  <Input
                    value={directProjectKey}
                    onChange={(event) => {
                      setDirectProjectKey(event.target.value);
                      setProjectSelectionLoaded(false);
                      setProjects([]);
                      setProjectSearch("");
                      setConnectionFeedback(null);
                      setScopeFeedback(null);
                      setSaveFeedback(null);
                      setCountFeedback(null);
                    }}
                    className={inputClass}
                    placeholder="GML"
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Enter a key to load only that Jira project, or leave this
                    blank to load every project visible to the account.
                  </p>
                </div>
                <div className="md:col-span-2 flex justify-end">
                  <Button
                    variant="outline"
                    onClick={test}
                    disabled={!!busy}
                  >
                    {busy === "test" && (
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    )}
                    Test connection
                  </Button>
                </div>
                {connectionFeedback && (
                  <div className="md:col-span-2">
                    <ActionStatus feedback={connectionFeedback} />
                  </div>
                )}
                {connectionVerified && (
                  <>
                    <div className="md:col-span-2 border-t border-gray-200 dark:border-[#3D3D3D] pt-5 mt-2">
                      <h4 className="font-medium text-black dark:text-white">
                        Projects to ingest
                      </h4>
                      <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
                        Load the requested Jira project or all projects visible
                        to this account, then select the projects for this graph.
                      </p>
                    </div>
                    <div className="md:col-span-2 flex justify-end">
                      <Button
                        variant="outline"
                        onClick={loadProjects}
                        disabled={!!busy}
                      >
                        {busy === "projects" && (
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        )}
                        {directProjectKey.trim()
                          ? "Load project"
                          : "Load visible projects"}
                      </Button>
                    </div>
                  </>
                )}
              </div>

              {projects.length > 0 && (
                <div>
                  <div className={labelClass}>
                    {directProjectKey.trim()
                      ? "Jira project"
                      : "Visible Jira projects"}
                  </div>
                  {!directProjectKey.trim() && (
                    <Input
                      value={projectSearch}
                      onChange={(event) => setProjectSearch(event.target.value)}
                      className={`${inputClass} mb-2`}
                      placeholder="Search by project key or name"
                      aria-label="Search visible Jira projects"
                    />
                  )}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2 max-h-48 overflow-y-auto border border-gray-200 dark:border-[#3D3D3D] rounded-md p-3">
                    {filteredProjects.map((project) => (
                      <label key={project.id} className="flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={editing.scope.project_keys.includes(project.key)}
                          onChange={(event) => {
                            const keys = event.target.checked
                              ? [...editing.scope.project_keys, project.key]
                              : editing.scope.project_keys.filter(
                                  (key) => key !== project.key
                                );
                            patch({ scope: { ...editing.scope, project_keys: keys } });
                          }}
                        />
                        {project.key} — {project.name}
                      </label>
                    ))}
                    {filteredProjects.length === 0 && (
                      <p className="text-sm text-gray-500 dark:text-gray-400 md:col-span-2">
                        No projects match this search.
                      </p>
                    )}
                  </div>
                </div>
              )}

              {connectionVerified && (
                <ActionStatus feedback={scopeFeedback} />
              )}

              {connectionVerified &&
                projectSelectionLoaded &&
                projects.length > 0 && (
                  <div className="border-t border-gray-200 dark:border-[#3D3D3D] pt-5">
                    <h4 className="font-medium text-black dark:text-white">
                      Ticket filters
                    </h4>
                    <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 mb-4">
                      Optionally limit which tickets are included from the
                      selected projects.
                    </p>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <label className={labelClass}>
                          Created on or after
                        </label>
                        <Input
                          type="date"
                          value={editing.scope.created_after || ""}
                          onChange={(event) =>
                            patch({
                              scope: {
                                ...editing.scope,
                                created_after: event.target.value || null,
                              },
                            })
                          }
                          className={inputClass}
                        />
                      </div>
                      <div>
                        <label className={labelClass}>
                          Updated on or after
                        </label>
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
                      <div className="md:col-span-2">
                        <div className={labelClass}>Status categories</div>
                        <div className="flex flex-wrap gap-x-5 gap-y-2">
                          {jiraStatusCategories.map((category) => {
                            const selected =
                              editing.scope.status_categories || [];
                            return (
                              <label
                                key={category.value}
                                className="flex items-center gap-2 text-sm text-black dark:text-white"
                              >
                                <input
                                  type="checkbox"
                                  checked={selected.includes(category.value)}
                                  onChange={(event) =>
                                    patch({
                                      scope: {
                                        ...editing.scope,
                                        status_categories: event.target.checked
                                          ? [...selected, category.value]
                                          : selected.filter(
                                              (value) =>
                                                value !== category.value
                                            ),
                                      },
                                    })
                                  }
                                />
                                {category.label}
                              </label>
                            );
                          })}
                        </div>
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                          Leave all categories unselected to include every status.
                        </p>
                      </div>
                      <label className="flex items-center gap-2 text-sm text-black dark:text-white">
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
                      <details className="md:col-span-2">
                        <summary className="cursor-pointer text-sm font-medium text-black dark:text-white">
                          Advanced scope options
                        </summary>
                        <div className="mt-3">
                          <label className={labelClass}>
                            Additional JQL filter
                          </label>
                          <Input
                            value={editing.scope.jql_extra}
                            onChange={(event) =>
                              patch({
                                scope: {
                                  ...editing.scope,
                                  jql_extra: event.target.value,
                                },
                              })
                            }
                            className={inputClass}
                            placeholder="statusCategory != Done"
                          />
                        </div>
                      </details>
                      <div className="md:col-span-2 flex justify-end">
                        <Button
                          variant="outline"
                          onClick={previewTicketCount}
                          disabled={
                            !!busy ||
                            editing.scope.project_keys.length === 0
                          }
                        >
                          {busy === "count" && (
                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          )}
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

              {connectionVerified && projectSelectionLoaded && saveFeedback && (
                <ActionStatus feedback={saveFeedback} />
              )}

              <div className="flex justify-end gap-2">
                <Button
                  variant="ghost"
                  onClick={() => {
                    setEditing(null);
                    setConnectionVerified(false);
                    setProjectSelectionLoaded(false);
                    setProjects([]);
                    setDirectProjectKey("");
                    setProjectSearch("");
                    setConnectionFeedback(null);
                    setScopeFeedback(null);
                    setSaveFeedback(null);
                    setCountFeedback(null);
                    sessionStorage.setItem(
                      editingSourceKey(selectedGraph),
                      "__closed__"
                    );
                  }}
                  disabled={!!busy}
                >
                  Cancel
                </Button>
                {connectionVerified && projectSelectionLoaded && (
                  <Button
                    onClick={save}
                    disabled={
                      !!busy || editing.scope.project_keys.length === 0
                    }
                  >
                    {busy === "save" ? (
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    ) : (
                      <Save className="h-4 w-4 mr-2" />
                    )}
                    Save project scope
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
                    2. Jira graph schema
                  </div>
                  <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
                    {schema?.status === "installed"
                      ? "Installed"
                      : schema?.status === "not_initialized"
                        ? "Initialize this graph before installing the Jira schema."
                        : schema?.status === "conflict"
                          ? "Existing graph schema conflicts with the Jira connector."
                          : schema?.status === "incomplete"
                            ? "The Jira schema is incomplete."
                            : sources.some(
                                  (source) =>
                                    source.sync.last_tested_at &&
                                    source.scope.project_keys.length > 0
                                )
                              ? "Ready to install."
                              : sources.some(
                                    (source) => source.sync.last_tested_at
                                  )
                                ? "Select and save at least one project first."
                                : "Test a saved connection before installing the schema."}
                  </p>
                </div>
                <div className="flex gap-2">
                  {schema &&
                    schema.status !== "installed" &&
                    schema.status !== "conflict" &&
                    schema.status !== "not_initialized" && (
                      <Button
                        onClick={installSchema}
                        disabled={
                          !!busy ||
                          !sources.some(
                            (source) =>
                              source.sync.last_tested_at &&
                              source.scope.project_keys.length > 0
                          )
                        }
                      >
                        {busy === "schema" && (
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        )}
                        Install Jira schema
                      </Button>
                    )}
                </div>
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
              <h3 className="font-medium text-black dark:text-white">
                3. Ingest and build
              </h3>
              <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 mb-4">
                Import Jira issues into this graph. The existing GraphRAG loading
                pipeline creates chunks and embeddings after structural ingestion.
              </p>
              {syncFeedback && (
                <div className="mb-4">
                  <ActionStatus feedback={syncFeedback} />
                </div>
              )}
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
                        Last ingestion: {source.sync.last_completed_at || "Never"} ·
                        Issues: {source.sync.last_issue_count || 0}
                      </div>
                      {source.sync.last_error &&
                        busy !== `sync:${source.id}` && (
                        <div className="text-xs text-red-600 mt-1">
                          {source.sync.last_error}
                        </div>
                      )}
                    </div>
                    <Button
                      size="sm"
                      onClick={() => sync(source)}
                      disabled={
                        !!busy ||
                        !source.enabled ||
                        !source.sync.last_tested_at ||
                        source.scope.project_keys.length === 0
                      }
                    >
                      {busy === `sync:${source.id}` && (
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      )}
                      Ingest now
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default DataSourcesConfig;
