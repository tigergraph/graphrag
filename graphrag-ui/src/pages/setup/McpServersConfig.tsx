import React, { useEffect, useState, useCallback, useRef } from "react";
import { Plus, Save, Loader2, Trash2, Pencil, PlugZap, Server, ChevronDown, ChevronRight, Upload } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import ConfigScopeToggle from "@/components/ConfigScopeToggle";

const MASKED_SECRET = "********";

type Transport = "stdio" | "http";

interface McpServer {
  name: string;
  transport: Transport;
  enabled: boolean;
  description: string;
  purpose: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  path: string;
  url: string;
  headers: Record<string, string>;
  forward_user: boolean;
  user_header: string;
  allowed_tools: string[];
}

const emptyServer = (): McpServer => ({
  name: "",
  transport: "http",
  enabled: true,
  description: "",
  purpose: "",
  command: "",
  args: [],
  env: {},
  path: "",
  url: "",
  headers: {},
  forward_user: false,
  user_header: "X-User",
  allowed_tools: ["*"],
});

const fromApi = (raw: any): McpServer => ({
  ...emptyServer(),
  ...raw,
  args: Array.isArray(raw?.args) ? raw.args : [],
  env: (raw?.env && typeof raw.env === "object") ? raw.env : {},
  headers: (raw?.headers && typeof raw.headers === "object") ? raw.headers : {},
  allowed_tools: Array.isArray(raw?.allowed_tools) && raw.allowed_tools.length > 0
    ? raw.allowed_tools : ["*"],
});

const isSpecComplete = (s: McpServer): boolean => {
  if (!s.name.trim()) return false;
  if (s.transport === "stdio") return s.command.trim().length > 0;
  return s.url.trim().length > 0;
};

// Turn raw backend / OS / Pydantic errors into something a user can act on.
const humanizeMcpError = (raw: string): string => {
  if (!raw) return "Unknown error.";
  const notFound = raw.match(/No such file or directory:\s*'([^']+)'/);
  if (notFound || raw.includes("[Errno 2]")) {
    return notFound
      ? `Command not found: "${notFound[1]}". Check the command path or that it's installed.`
      : "Command not found. Check the command path.";
  }
  if (/permission denied/i.test(raw)) return "Permission denied launching the command.";
  if (/Connection refused|ECONNREFUSED|getaddrinfo|Name or service not known|Failed to establish|timed out|timeout/i.test(raw))
    return "Couldn't reach the server URL. Check the address and that the server is running.";
  if (/string_too_short|at least 1 character|[Ff]ield required/.test(raw))
    return "A required field is empty. Fill in the name and the command or URL.";
  if (/validation error/i.test(raw)) return "Some fields are invalid. Check the required fields.";
  // Drop Pydantic's doc-link noise and over-long dumps.
  return raw.split("For further information")[0].trim().slice(0, 300);
};

// ---- KvEditor / ListEditor / EditForm — extracted to module scope so
// they don't get re-created on every parent render (which would unmount +
// remount the inputs and make typing feel slow).

const labelClass = "block text-sm font-medium mb-2 text-black dark:text-white";
const helpClass = "text-xs text-gray-600 dark:text-[#D9D9D9] mt-1";
const inputDark = "dark:border-[#3D3D3D] dark:bg-background";

interface KvEditorProps {
  label: string;
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  hint?: string;
}

const KvEditor: React.FC<KvEditorProps> = ({ label, value, onChange, hint }) => {
  const entries = Object.entries(value);
  return (
    <div>
      <label className={labelClass}>{label}</label>
      {hint && <p className={`${helpClass} mb-2`}>{hint}</p>}
      <div className="space-y-2">
        {entries.length === 0 && (
          <p className="text-xs text-gray-500 dark:text-gray-400 italic">(none)</p>
        )}
        {entries.map(([k, v]) => (
          <div key={k} className="flex gap-2 items-center">
            <Input
              value={k}
              onChange={(e) => {
                const next: Record<string, string> = {};
                for (const [kk, vv] of entries) {
                  next[kk === k ? e.target.value : kk] = vv;
                }
                onChange(next);
              }}
              placeholder="key"
              className={`w-1/3 ${inputDark}`}
            />
            <Input
              value={v}
              onChange={(e) => onChange({ ...value, [k]: e.target.value })}
              placeholder={v === MASKED_SECRET ? "(stored — leave to keep)" : "value"}
              className={`flex-1 ${inputDark}`}
              type={v === MASKED_SECRET ? "password" : "text"}
            />
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                const next = { ...value };
                delete next[k];
                onChange(next);
              }}
            >
              <Trash2 size={14} />
            </Button>
          </div>
        ))}
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            let i = 1; let k = "KEY";
            while (k in value) { i += 1; k = `KEY${i}`; }
            onChange({ ...value, [k]: "" });
          }}
        >
          <Plus size={14} className="mr-1" /> Add
        </Button>
      </div>
    </div>
  );
};

interface ListEditorProps {
  label: string;
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
}

const ListEditor: React.FC<ListEditorProps> = ({ label, value, onChange, placeholder }) => (
  <div>
    <label className={labelClass}>{label}</label>
    <Input
      value={value.join(", ")}
      onChange={(e) =>
        onChange(
          e.target.value
            .split(",")
            .map((s) => s.trim())
            .filter((s) => s.length > 0)
        )
      }
      placeholder={placeholder}
      className={inputDark}
    />
  </div>
);

interface EditFormProps {
  server: McpServer;
  onPatch: (patch: Partial<McpServer>) => void;
  onSave: () => void;
  onCancel: () => void;
  isSaving: boolean;
  testPassed: boolean;
}

const EditForm: React.FC<EditFormProps> = ({ server: s, onPatch, onSave, onCancel, isSaving, testPassed }) => {
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const handleUploadLibrary = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const creds = sessionStorage.getItem("auth");
      const fd = new FormData();
      fd.append("file", file);
      const resp = await fetch("/ui/mcp_servers/library", {
        method: "POST",
        headers: { Authorization: creds! },   // let the browser set the multipart boundary
        body: fd,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
      onPatch({ path: data.path });            // auto-fill the field with the stored filename
    } catch (err: any) {
      alert(`Upload failed: ${err.message}`);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div className="mt-4 p-4 bg-white dark:bg-shadeA rounded-md border border-gray-300 dark:border-[#3D3D3D] space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className={labelClass}>Name</label>
          <Input
            value={s.name}
            onChange={(e) => onPatch({ name: e.target.value })}
            placeholder="e.g. sales_tg"
            className={inputDark}
          />
        </div>
        <div>
          <label className={labelClass}>Transport</label>
          <Select
            value={s.transport}
            onValueChange={(v: Transport) => onPatch({ transport: v })}
          >
            <SelectTrigger className={inputDark}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="http">http (streamable — recommended)</SelectItem>
              <SelectItem value="stdio">stdio (subprocess in the container)</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div>
        <label className={labelClass}>Description</label>
        <Input
          value={s.description}
          onChange={(e) => onPatch({ description: e.target.value })}
          placeholder="Short label shown in tool catalogs"
          className={inputDark}
        />
      </div>

      <div>
        <label className={labelClass}>Purpose</label>
        <textarea
          value={s.purpose}
          onChange={(e) => onPatch({ purpose: e.target.value })}
          placeholder="What data lives here and when to use it. Used by the planner's tool-selection filter."
          className={`w-full p-2 rounded-md border border-gray-300 ${inputDark} text-sm text-black dark:text-white`}
          rows={2}
        />
      </div>

      {s.transport === "stdio" ? (
        <>
          <div className="flex items-start gap-2 p-2 rounded-md bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 text-xs text-amber-700 dark:text-amber-300">
            <span className="mt-0.5 shrink-0">ℹ️</span>
            <span>
              stdio runs the server <strong>inside GraphRAG</strong>. Give the path to its source
              tarball below — GraphRAG installs it so the <strong>Command</strong> (the package's
              console script) is available. See the MCP server setup guide for more details. To run
              the server yourself instead, use <strong>HTTP</strong>.
            </span>
          </div>
          <div>
            <label className={labelClass}>Library tarball</label>
            <div className="flex gap-2">
              <Input
                value={s.path}
                onChange={(e) => onPatch({ path: e.target.value })}
                placeholder="my_server-1.0.tar.gz"
                className={`${inputDark} flex-1`}
              />
              <input
                ref={fileRef}
                type="file"
                accept=".tar.gz,.tgz,application/gzip"
                className="hidden"
                onChange={handleUploadLibrary}
              />
              <Button
                type="button"
                size="sm"
                disabled={uploading}
                onClick={() => fileRef.current?.click()}
                className="shrink-0 gradient text-white"
              >
                {uploading
                  ? <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                  : <Upload className="h-4 w-4 mr-1" />}
                {uploading ? "Uploading…" : "Upload"}
              </Button>
            </div>
            <p className={helpClass}>
              A <code>.tar.gz</code> GraphRAG installs so the command below exists. Upload one (the
              field fills in) or enter a filename already in the server's library folder. Leave blank
              if the command is already installed.
            </p>
          </div>
          <div>
            <label className={labelClass}>Command</label>
            <Input
              value={s.command}
              onChange={(e) => onPatch({ command: e.target.value })}
              placeholder="e.g. tigergraph-mcp (console command the package provides)"
              className={inputDark}
            />
          </div>
          <ListEditor
            label="Args (comma-separated)"
            value={s.args}
            onChange={(next) => onPatch({ args: next })}
            placeholder="e.g. -vv"
          />
          <KvEditor
            label="Env"
            value={s.env}
            onChange={(next) => onPatch({ env: next })}
            hint="Environment variables for the subprocess (secrets stay server-side)."
          />
        </>
      ) : (
        <>
          <div>
            <label className={labelClass}>URL</label>
            <Input
              value={s.url}
              onChange={(e) => onPatch({ url: e.target.value })}
              placeholder="https://mcp.example/server"
              className={inputDark}
            />
          </div>
          <KvEditor
            label="Headers"
            value={s.headers}
            onChange={(next) => onPatch({ headers: next })}
            hint="Sent with every MCP request (e.g. Authorization)."
          />
        </>
      )}

      <div className="grid grid-cols-2 gap-4">
        <div className="flex items-center gap-2 mt-2">
          <input
            type="checkbox"
            checked={s.enabled}
            onChange={(e) => onPatch({ enabled: e.target.checked })}
            id="enabled-row"
            className="rounded border-gray-300 dark:border-[#3D3D3D]"
          />
          <label htmlFor="enabled-row" className="text-sm font-medium text-black dark:text-white">
            Enabled
          </label>
        </div>
        <div className="flex items-center gap-2 mt-2">
          <input
            type="checkbox"
            checked={s.forward_user}
            onChange={(e) => onPatch({ forward_user: e.target.checked })}
            id="fwd-row"
            className="rounded border-gray-300 dark:border-[#3D3D3D]"
          />
          <label htmlFor="fwd-row" className="text-sm font-medium text-black dark:text-white">
            Forward logged-in user
          </label>
        </div>
      </div>

      {s.forward_user && (
        <div>
          <label className={labelClass}>User header / meta key</label>
          <Input
            value={s.user_header}
            onChange={(e) => onPatch({ user_header: e.target.value })}
            placeholder="X-User"
            className={inputDark}
          />
        </div>
      )}

      <ListEditor
        label='Allowed tools (globs, e.g. "get_*, list_*"; default "*")'
        value={s.allowed_tools}
        onChange={(next) => onPatch({ allowed_tools: next.length ? next : ["*"] })}
      />

      <div className="flex items-center justify-end gap-3 pt-2 border-t border-gray-300 dark:border-[#3D3D3D]">
        {!testPassed && (
          <span className="mr-auto text-xs text-gray-500 dark:text-gray-400">
            Run a successful Test before saving.
          </span>
        )}
        <Button variant="outline" onClick={onCancel} className="dark:border-[#3D3D3D]">
          Cancel
        </Button>
        <Button
          onClick={onSave}
          disabled={isSaving || !testPassed}
          title={!testPassed ? "Test the connection successfully before saving" : undefined}
          className="gradient text-white"
        >
          {isSaving ? (
            <>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              Saving...
            </>
          ) : (
            <>
              <Save className="h-4 w-4 mr-2" />
              Save
            </>
          )}
        </Button>
      </div>
    </div>
  );
};

// ---- Main page ----

const McpServersConfig: React.FC = () => {
  const [configScope, setConfigScope] = useState<"global" | "graph">("global");
  const [selectedGraph, setSelectedGraph] = useState<string>(
    sessionStorage.getItem("selectedGraph") || ""
  );
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);

  const [servers, setServers] = useState<McpServer[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState<"" | "success" | "error">("");

  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [testResults, setTestResults] = useState<Record<number, { ok: boolean; tools?: any[]; error?: string }>>({});
  const [testingIndex, setTestingIndex] = useState<number | null>(null);

  // -- graph list ------------------------------------------------------------

  useEffect(() => {
    const creds = sessionStorage.getItem("auth");
    if (!creds) return;
    fetch("/ui/list_graphs", { headers: { Authorization: creds } })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (Array.isArray(data?.graphs)) setAvailableGraphs(data.graphs);
        else if (Array.isArray(data)) setAvailableGraphs(data);
      })
      .catch(() => {});
  }, []);

  // -- load on scope/graph change -------------------------------------------

  const loadServers = useCallback(async (scope: "global" | "graph", graph: string) => {
    setIsLoading(true);
    setMessage("");
    setMessageType("");
    setEditingIndex(null);
    setTestResults({});
    try {
      const creds = sessionStorage.getItem("auth");
      if (!creds) {
        setMessage("Not logged in.");
        setMessageType("error");
        return;
      }
      const url = scope === "graph" && graph
        ? `/ui/${graph}/mcp_servers`
        : "/ui/mcp_servers";
      const resp = await fetch(url, { headers: { Authorization: creds } });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const list = Array.isArray(data?.data) ? data.data : [];
      setServers(list.map(fromApi));
    } catch (e: any) {
      setMessage(`Failed to load MCP servers: ${e.message}`);
      setMessageType("error");
      setServers([]);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (configScope === "graph" && !selectedGraph) {
      setServers([]);
      return;
    }
    loadServers(configScope, selectedGraph);
  }, [configScope, selectedGraph, loadServers]);

  // -- mutations -------------------------------------------------------------

  const patchRow = useCallback((idx: number, patch: Partial<McpServer>) => {
    setServers((prev) => prev.map((s, i) => (i === idx ? { ...s, ...patch } : s)));
    // Editing invalidates any prior test result, so the user must re-test
    // before saving.
    setTestResults((p) => {
      if (!(idx in p)) return p;
      const c = { ...p };
      delete c[idx];
      return c;
    });
  }, []);

  const removeRow = useCallback((idx: number) => {
    setServers((prev) => prev.filter((_, i) => i !== idx));
    setEditingIndex(null);
    setTestResults((p) => { const c = { ...p }; delete c[idx]; return c; });
  }, []);

  const addRow = useCallback(() => {
    setServers((prev) => {
      const next = [...prev, emptyServer()];
      setEditingIndex(next.length - 1);
      return next;
    });
  }, []);

  // -- save ------------------------------------------------------------------

  const handleSave = async () => {
    // Validate up front so the user gets a clear message instead of a raw
    // backend validation dump.
    const incomplete = servers
      .map((s, i) => ({ s, i }))
      .filter(({ s }) => !isSpecComplete(s));
    if (incomplete.length) {
      const who = incomplete
        .map(({ s, i }) => (s.name.trim() ? `"${s.name.trim()}"` : `#${i + 1}`))
        .join(", ");
      setMessage(
        `Please complete the required fields (name, and command or URL) for ` +
          `${incomplete.length === 1 ? "server" : "servers"}: ${who}`
      );
      setMessageType("error");
      return false;
    }
    setIsSaving(true);
    setMessage("");
    setMessageType("");
    try {
      const creds = sessionStorage.getItem("auth");
      const url = configScope === "graph" && selectedGraph
        ? `/ui/${selectedGraph}/mcp_servers`
        : "/ui/mcp_servers";
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: creds! },
        body: JSON.stringify(servers),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => null);
        throw new Error(err?.detail || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      setMessage(data.message || "Saved.");
      setMessageType("success");
      await loadServers(configScope, selectedGraph);
      setTimeout(() => { setMessage(""); setMessageType(""); }, 3000);
      return true;
    } catch (e: any) {
      setMessage(`Save failed: ${humanizeMcpError(e.message)}`);
      setMessageType("error");
      return false;
    } finally {
      setIsSaving(false);
    }
  };

  // -- test ------------------------------------------------------------------

  const handleTest = async (idx: number) => {
    setTestingIndex(idx);
    try {
      const creds = sessionStorage.getItem("auth");
      const resp = await fetch("/ui/mcp_servers/test", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: creds! },
        body: JSON.stringify(servers[idx]),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => null);
        throw new Error(err?.detail || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      const result = data?.data || { ok: false, error: "no data" };
      if (!result.ok && result.error) result.error = humanizeMcpError(result.error);
      setTestResults((p) => ({ ...p, [idx]: result }));
    } catch (e: any) {
      setTestResults((p) => ({ ...p, [idx]: { ok: false, error: humanizeMcpError(e.message) } }));
    } finally {
      setTestingIndex(null);
    }
  };

  // -- render ----------------------------------------------------------------

  return (
    <div className="p-6 max-w-5xl mx-auto">
      {/* Header — matches GraphRAGConfig / LLMConfig pattern */}
      <div className="flex items-center gap-4 mb-6">
        <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center">
          <Server className="h-6 w-6 text-tigerOrange" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-black dark:text-white">MCP Servers</h1>
          <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
            External Model Context Protocol servers the agentic engine can call as
            extra tools. Per-graph entries override global by name; <i>enabled</i> off
            on a per-graph entry suppresses a same-named global one.
          </p>
        </div>
      </div>

      <ConfigScopeToggle
        configScope={configScope}
        selectedGraph={selectedGraph}
        availableGraphs={availableGraphs}
        onScopeChange={(s) => setConfigScope(s)}
        onGraphChange={(g) => { setSelectedGraph(g); sessionStorage.setItem("selectedGraph", g); }}
      />

      {configScope === "graph" && !selectedGraph && (
        <div className="text-sm text-gray-600 dark:text-[#D9D9D9] italic mb-4">
          Select a graph above to manage its overrides.
        </div>
      )}

      {(configScope === "global" || selectedGraph) && (
        <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
          <div className="flex items-center justify-between mb-4">
            <div className="text-sm text-gray-600 dark:text-[#D9D9D9]">
              {isLoading
                ? "Loading…"
                : `${servers.length} server${servers.length === 1 ? "" : "s"} configured`}
            </div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={addRow} disabled={isLoading} className="dark:border-[#3D3D3D]">
                <Plus className="h-4 w-4 mr-2" /> Add server
              </Button>
            </div>
          </div>

          {message && (
            <div
              className={`mb-4 p-3 rounded-md text-sm border ${
                messageType === "success"
                  ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 border-green-200 dark:border-green-800"
                  : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 border-red-200 dark:border-red-800"
              }`}
            >
              {message}
            </div>
          )}

          {servers.length === 0 && !isLoading && (
            <div className="text-sm text-gray-600 dark:text-[#D9D9D9] italic">
              No servers configured at this scope yet. Click <b>Add server</b> to add one.
            </div>
          )}

          {servers.map((s, idx) => {
            const isOpen = editingIndex === idx;
            const tr = testResults[idx];
            const complete = isSpecComplete(s);
            const summaryDetail =
              s.transport === "stdio"
                ? (s.command ? `${s.command}${s.args.length ? " " + s.args.join(" ") : ""}` : "(no command)")
                : (s.url || "(no url)");
            return (
              <div
                key={idx}
                className="border-t border-gray-200 dark:border-[#3D3D3D] py-3 first:border-t-0"
              >
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => setEditingIndex(isOpen ? null : idx)}
                    className="text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
                  >
                    {isOpen ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                  </button>
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-black dark:text-white truncate">
                      {s.name || <span className="italic text-gray-500 dark:text-gray-400">(unnamed)</span>}
                      {!s.enabled && (
                        <span className="ml-2 text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400">disabled</span>
                      )}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-[#D9D9D9] truncate">
                      {summaryDetail}
                    </div>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleTest(idx)}
                    disabled={!complete || testingIndex === idx}
                    title={complete ? "Connect and list tools" : "Fill required fields first"}
                  >
                    {testingIndex === idx
                      ? <Loader2 size={14} className="animate-spin" />
                      : <PlugZap size={14} />}
                    <span className="ml-1">Test</span>
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setEditingIndex(isOpen ? null : idx)}>
                    <Pencil size={14} />
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => removeRow(idx)}>
                    <Trash2 size={14} />
                  </Button>
                </div>
                {tr && (
                  <div
                    className={`mt-2 ml-7 text-xs p-2 rounded border ${
                      tr.ok
                        ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 border-green-200 dark:border-green-800"
                        : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 border-red-200 dark:border-red-800"
                    }`}
                  >
                    {tr.ok ? (
                      <>
                        <div>Connected. Tools discovered:</div>
                        {tr.tools && tr.tools.length > 0 ? (
                          <ul className="list-disc ml-5 mt-1">
                            {tr.tools.map((t: any) => (
                              <li key={t.qualified_name}>
                                <code>{t.name}</code>
                                {t.description ? ` — ${t.description}` : ""}
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <div className="italic">(server reports no tools)</div>
                        )}
                      </>
                    ) : (
                      <div>Failed: {tr.error}</div>
                    )}
                  </div>
                )}
                {isOpen && (
                  <EditForm
                    server={s}
                    onPatch={(patch) => patchRow(idx, patch)}
                    onSave={async () => { const ok = await handleSave(); if (ok) setEditingIndex(null); }}
                    onCancel={() => removeRow(idx)}
                    isSaving={isSaving}
                    testPassed={!!testResults[idx]?.ok}
                  />
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default McpServersConfig;
