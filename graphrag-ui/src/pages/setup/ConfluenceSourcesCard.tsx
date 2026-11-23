import React, { useCallback, useEffect, useState } from "react";
import { FileText, Loader2, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { safeJson } from "@/utils/safeJson";
import type { ConfluenceSource, JiraSchemaStatus } from "@/types/dataSources";

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

const authHeaders = () => ({
  Authorization: sessionStorage.getItem("auth") || "",
  "Content-Type": "application/json",
});

const errorDetail = (data: any, fallback: string) =>
  typeof data?.detail === "string" ? data.detail : fallback;

const needsResume = (source: ConfluenceSource) => {
  const failed = source.sync.failed_content_ids?.length || 0;
  const pending = source.sync.pending_comment_content_ids?.length || 0;
  const started = source.sync.last_started_at || "";
  const completed = source.sync.last_completed_at || "";
  const stopped =
    Boolean(started) &&
    Boolean(source.sync.checkpoint || source.sync.last_content_id) &&
    (!completed || completed < started);
  return Boolean(source.sync.last_error) || failed > 0 || pending > 0 || stopped;
};

const ConfluenceSourcesCard: React.FC<{ graph: string }> = ({ graph }) => {
  const [sources, setSources] = useState<ConfluenceSource[]>([]);
  const [editing, setEditing] = useState<ConfluenceSource | null>(null);
  const [spaces, setSpaces] = useState<Array<{ key: string; name: string }>>([]);
  const [schema, setSchema] = useState<JiraSchemaStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState<"success" | "error" | "">("");

  const loadSchema = useCallback(async () => {
    if (!graph) return;
    const response = await fetch(`/ui/${graph}/data-sources/confluence/schema`, {
      headers: { Authorization: sessionStorage.getItem("auth") || "" },
    });
    const data = await safeJson(response);
    if (response.ok) setSchema(data);
  }, [graph]);

  const loadSources = useCallback(async () => {
    if (!graph) {
      setSources([]);
      return;
    }
    setLoading(true);
    try {
      const response = await fetch(`/ui/${graph}/data-sources/confluence`, {
        headers: { Authorization: sessionStorage.getItem("auth") || "" },
      });
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      setSources(Array.isArray(data.sources) ? data.sources : []);
      await loadSchema();
    } catch (error: any) {
      setMessage(`Failed to load Confluence sources: ${error.message}`);
      setMessageType("error");
    } finally {
      setLoading(false);
    }
  }, [graph, loadSchema]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  const testConnection = async () => {
    if (!editing) return;
    setBusy("test");
    setMessage("");
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
      setMessage(
        `Connected as ${data.account?.display_name || data.account?.email || "the current user"}.`
      );
      setMessageType("success");
      setEditing(data.source);
      await loadSources();
      const spacesResponse = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(editing.id)}/spaces`,
        { headers: { Authorization: sessionStorage.getItem("auth") || "" } }
      );
      const spacesData = await safeJson(spacesResponse);
      if (spacesResponse.ok) setSpaces(spacesData.spaces || []);
    } catch (error: any) {
      setMessage(error.message);
      setMessageType("error");
    } finally {
      setBusy("");
    }
  };

  const saveSource = async () => {
    if (!editing?.id) return;
    setBusy("save");
    setMessage("");
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
      setMessage("Confluence source saved.");
      setMessageType("success");
      await loadSources();
    } catch (error: any) {
      setMessage(error.message);
      setMessageType("error");
    } finally {
      setBusy("");
    }
  };

  const installSchema = async () => {
    setBusy("schema");
    setMessage("");
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/schema/install`,
        { method: "POST", headers: authHeaders() }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      setMessage("Schema installation started.");
      setMessageType("success");
      await loadSchema();
    } catch (error: any) {
      setMessage(error.message);
      setMessageType("error");
    } finally {
      setBusy("");
    }
  };

  const startSync = async (source: ConfluenceSource) => {
    setBusy(source.id);
    setMessage("");
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(source.id)}/sync`,
        { method: "POST", headers: authHeaders() }
      );
      const data = await safeJson(response);
      if (!response.ok) throw new Error(errorDetail(data, `HTTP ${response.status}`));
      setMessage(`Sync started for ${source.display_name}.`);
      setMessageType("success");
    } catch (error: any) {
      setMessage(error.message);
      setMessageType("error");
    } finally {
      setBusy("");
    }
  };

  const removeSource = async (source: ConfluenceSource) => {
    setBusy(source.id);
    try {
      const response = await fetch(
        `/ui/${graph}/data-sources/confluence/${encodeURIComponent(source.id)}`,
        { method: "DELETE", headers: authHeaders() }
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

  const toggleSpace = (key: string) => {
    if (!editing) return;
    const current = editing.scope.space_keys || [];
    const space_keys = current.includes(key)
      ? current.filter((item) => item !== key)
      : [...current, key];
    setEditing({ ...editing, scope: { ...editing.scope, space_keys } });
  };

  const toggleType = (value: "page" | "blogpost") => {
    if (!editing) return;
    const current = editing.scope.content_types || [];
    const content_types = current.includes(value)
      ? current.filter((item) => item !== value)
      : [...current, value];
    setEditing({ ...editing, scope: { ...editing.scope, content_types } });
  };

  return (
    <div className="rounded-lg border border-gray-300 dark:border-[#3D3D3D] p-4 bg-white dark:bg-background flex flex-col">
      <div className="flex items-start gap-3">
        <FileText className="h-5 w-5 mt-1" />
        <div>
          <h2 className="text-lg font-semibold text-black dark:text-white">
            Confluence
          </h2>
          <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
            Connect a site, test the API token, install the schema, then ingest
            pages and blog posts.
          </p>
        </div>
      </div>
      {message && (
        <p
          className={`mt-3 text-sm ${
            messageType === "error" ? "text-red-600" : "text-green-700"
          }`}
          role={messageType === "error" ? "alert" : "status"}
        >
          {message}
        </p>
      )}
      <div className="mt-4 space-y-3">
        {loading && <Loader2 className="h-4 w-4 animate-spin" />}
        {sources.map((source) => (
          <div key={source.id} className="rounded-md border p-3 text-sm">
            <div className="flex items-center justify-between gap-2">
              <div>
                <div className="font-medium text-black dark:text-white">
                  {source.display_name}
                </div>
                <div className="text-gray-600 dark:text-[#D9D9D9]">
                  {source.connection.site}
                  {source.sync.last_tested_at ? " · connected" : " · not tested"}
                  {schema?.status === "installed" ? " · schema installed" : ""}
                </div>
              </div>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setEditing(source)}>
                  Edit
                </Button>
                <Button
                  variant="outline"
                  onClick={() => void startSync(source)}
                  disabled={busy === source.id}
                >
                  {needsResume(source) ? "Resume" : "Sync"}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => void removeSource(source)}
                  disabled={busy === source.id}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            </div>
            {source.sync.last_error && (
              <p className="mt-2 text-red-600">{source.sync.last_error}</p>
            )}
            {source.sync.last_link_error && (
              <p className="mt-2 text-amber-700">
                Page saved. Link write failed: {source.sync.last_link_error}
              </p>
            )}
          </div>
        ))}
      </div>
      <div className="mt-4 flex gap-2">
        <Button
          onClick={() => setEditing(emptySource())}
          disabled={!graph}
          className="gradient text-white"
        >
          <Plus className="h-4 w-4 mr-2" />
          Add Confluence
        </Button>
        <Button
          variant="outline"
          onClick={() => void installSchema()}
          disabled={!graph || busy === "schema" || !sources.some((source) => source.sync.last_tested_at)}
        >
          Install schema
        </Button>
      </div>
      {editing && (
        <div className="mt-4 space-y-3 border-t pt-4">
          <Input
            placeholder="Display name"
            value={editing.display_name}
            onChange={(event) =>
              setEditing({ ...editing, display_name: event.target.value })
            }
          />
          <Input
            placeholder="Source id"
            value={editing.id}
            onChange={(event) => setEditing({ ...editing, id: event.target.value })}
          />
          <Input
            placeholder="Site host, for example acme.atlassian.net"
            value={editing.connection.site}
            onChange={(event) =>
              setEditing({
                ...editing,
                connection: { ...editing.connection, site: event.target.value },
              })
            }
          />
          <Input
            placeholder="Atlassian email"
            value={editing.connection.email}
            onChange={(event) =>
              setEditing({
                ...editing,
                connection: { ...editing.connection, email: event.target.value },
              })
            }
          />
          <Input
            placeholder="API token"
            type="password"
            value={editing.connection.api_token || ""}
            onChange={(event) =>
              setEditing({
                ...editing,
                connection: { ...editing.connection, api_token: event.target.value },
              })
            }
          />
          <Input
            type="date"
            value={(editing.scope.updated_after || "").slice(0, 10)}
            onChange={(event) =>
              setEditing({
                ...editing,
                scope: {
                  ...editing.scope,
                  updated_after: event.target.value
                    ? `${event.target.value}T00:00:00Z`
                    : null,
                },
              })
            }
          />
          <div className="flex gap-3 text-sm">
            {(["page", "blogpost"] as const).map((value) => (
              <label key={value} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={(editing.scope.content_types || []).includes(value)}
                  onChange={() => toggleType(value)}
                />
                {value}
              </label>
            ))}
          </div>
          {spaces.length > 0 && (
            <div className="flex flex-wrap gap-3 text-sm">
              {spaces.map((space) => (
                <label key={space.key} className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={(editing.scope.space_keys || []).includes(space.key)}
                    onChange={() => toggleSpace(space.key)}
                  />
                  {space.key} · {space.name}
                </label>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <Button onClick={() => void testConnection()} disabled={busy === "test" || !editing.id}>
              {busy === "test" && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Test connection
            </Button>
            <Button variant="outline" onClick={() => void saveSource()} disabled={busy === "save" || !editing.id}>
              Save
            </Button>
          </div>
        </div>
      )}
    </div>
  );
};

export default ConfluenceSourcesCard;
