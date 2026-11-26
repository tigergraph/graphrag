import React, { useEffect, useState } from "react";
import { Loader2, Search, FileCode, PlusCircle, MinusCircle, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useConfirm } from "@/hooks/useConfirm";

// Chrome / Safari on macOS clip the underscore descender inside <input>.
const INPUT_CLIP_FIX: React.CSSProperties = {
  WebkitAppearance: "none",
  appearance: "none",
  lineHeight: "1.5",
};

type Param = {
  name: string;
  type: string;
  description: string;
  default: unknown;
  has_default: boolean;
};

type Fields = { returns: string; use_for: string; do_not_use_for: string; output: string };

type ToolRow = {
  name?: string;
  query: string;
  description: string;
  params: Param[];
  status?: "registered" | "not_registered" | "changed";
  registered_at?: number;
  registered_by?: string;
};

type Skipped = { name: string; reason: string; registered?: boolean };

const EMPTY_FIELDS: Fields = { returns: "", use_for: "", do_not_use_for: "", output: "" };

const STATUS_LABEL: Record<string, string> = {
  registered: "Registered",
  not_registered: "Not registered",
  changed: "Changed since it was registered",
};

const fieldInput =
  "bg-white dark:bg-shadeA border-gray-300 dark:border-[#3D3D3D] text-black dark:text-white";

function blockPreview(f: Fields): string {
  const lines = ["[GRAPHRAG_TOOL]"];
  if (f.returns.trim()) lines.push(`Returns: ${f.returns.trim()}`);
  if (f.use_for.trim()) lines.push(`Use for: ${f.use_for.trim()}`);
  if (f.do_not_use_for.trim()) lines.push(`Do not use for: ${f.do_not_use_for.trim()}`);
  if (f.output.trim()) lines.push(`Output: ${f.output.trim()}`);
  lines.push("[/GRAPHRAG_TOOL]");
  return lines.join("\n");
}

async function call(url: string, init?: RequestInit): Promise<any> {
  const auth = sessionStorage.getItem("auth");
  if (!auth) throw new Error("Not signed in.");
  const resp = await fetch(url, {
    ...init,
    headers: { Authorization: auth, "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = data?.detail;
    throw Object.assign(new Error(typeof detail === "string" ? detail : resp.statusText), {
      status: resp.status,
    });
  }
  return data;
}

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  graphs: string[];
}

const QueryToolsDialog: React.FC<Props> = ({ open, onOpenChange, graphs }) => {
  const [confirm, confirmDialog] = useConfirm();
  const [graph, setGraph] = useState("");
  const [tab, setTab] = useState<"describe" | "register">("register");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");

  // Describe
  const [queryName, setQueryName] = useState("");
  const [loadedName, setLoadedName] = useState("");
  const [isNewQuery, setIsNewQuery] = useState(false);
  const [gsql, setGsql] = useState("");
  const [fields, setFields] = useState<Fields>(EMPTY_FIELDS);
  const [otherText, setOtherText] = useState("");
  const [params, setParams] = useState<Param[]>([]);
  const [paramText, setParamText] = useState<Record<string, string>>({});

  // Register
  const [registered, setRegistered] = useState<ToolRow[]>([]);
  const [matching, setMatching] = useState<ToolRow[] | null>(null);
  const [skipped, setSkipped] = useState<Skipped[]>([]);
  const [missing, setMissing] = useState<Skipped[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [showSkipped, setShowSkipped] = useState(false);

  const resetDescribe = () => {
    setQueryName("");
    setLoadedName("");
    setIsNewQuery(false);
    setGsql("");
    setFields(EMPTY_FIELDS);
    setOtherText("");
    setParams([]);
    setParamText({});
  };

  const loadRegistered = async (g: string) => {
    if (!g) return;
    try {
      const data = await call(`/ui/${g}/query_tools`);
      setRegistered(data.tools || []);
    } catch (err: any) {
      setMessage(`Could not load the registered tools: ${err.message}`);
    }
  };

  useEffect(() => {
    if (!open) return;
    const initial = sessionStorage.getItem("selectedGraph") || graphs[0] || "";
    setGraph(initial);
    setTab("register");
    setMessage("");
    setMatching(null);
    setSkipped([]);
    setMissing([]);
    setSelected(new Set());
    resetDescribe();
    if (initial) loadRegistered(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const changeGraph = (g: string) => {
    setGraph(g);
    setMessage("");
    setMatching(null);
    setSkipped([]);
    setMissing([]);
    setSelected(new Set());
    resetDescribe();
    loadRegistered(g);
  };

  // ------------------------------------------------------------- describe

  const loadQuery = async (name?: string) => {
    const n = (name ?? queryName).trim();
    if (!graph || !n) return;
    setQueryName(n);
    setBusy("load");
    setMessage("");
    try {
      const data = await call(`/ui/${graph}/query_descriptions/${encodeURIComponent(n)}`);
      setLoadedName(n);
      setIsNewQuery(false);
      setFields({ ...EMPTY_FIELDS, ...(data.fields || {}) });
      setOtherText(data.other_text || "");
      setParams(data.params || []);
      setParamText(
        Object.fromEntries((data.params || []).map((p: Param) => [p.name, p.description || ""]))
      );
      if (data.problem) setMessage(`The current description is not in the tool format: ${data.problem}.`);
    } catch (err: any) {
      if (err.status === 404) {
        setLoadedName(n);
        setIsNewQuery(true);
        setFields(EMPTY_FIELDS);
        setOtherText("");
        setParams([]);
        setParamText({});
        setMessage(`"${n}" is not on this graph. Paste its GSQL below to create and install it.`);
      } else {
        setMessage(err.message);
      }
    } finally {
      setBusy("");
    }
  };

  const saveDescription = async () => {
    const n = loadedName;
    if (!n) return;
    if (!fields.returns.trim() || !fields.use_for.trim()) {
      setMessage('Fill in "Returns" and "Use for".');
      return;
    }
    if (isNewQuery && !gsql.trim()) {
      setMessage("Paste the GSQL of the new query.");
      return;
    }
    setBusy("save");
    setMessage(isNewQuery ? "Creating and installing the query…" : "Saving the description…");
    try {
      const changedParams = Object.fromEntries(
        params
          .filter((p) => (paramText[p.name] || "") !== (p.description || ""))
          .map((p) => [p.name, paramText[p.name] || ""])
      );
      const data = await call(`/ui/${graph}/query_descriptions`, {
        method: "POST",
        body: JSON.stringify({
          name: n,
          gsql: isNewQuery ? gsql : "",
          ...fields,
          param_descriptions: changedParams,
        }),
      });
      await loadQuery(n);
      const needParams = isNewQuery ? " Describe its parameters below and save again if it has any." : "";
      setMessage(
        `✅ Saved.${data.created ? " The query was created and installed." : ""}${needParams} ` +
          (data.registered
            ? "It is registered; register it again on the Register tab so the agent uses the new description."
            : "It is not a tool yet: register it on the Register tab.")
      );
    } catch (err: any) {
      setMessage(`Save failed: ${err.message}`);
    } finally {
      setBusy("");
    }
  };

  const removeDescription = async () => {
    const n = loadedName;
    if (!n) return;
    const ok = await confirm(
      `Remove the tool description of "${n}"? Other description text in TigerGraph is kept. A registered tool keeps working until it is unregistered.`
    );
    if (!ok) return;
    setBusy("remove");
    try {
      await call(`/ui/${graph}/query_descriptions/remove`, {
        method: "POST",
        body: JSON.stringify({ name: n }),
      });
      await loadQuery(n);
      setMessage("✅ Tool description removed.");
    } catch (err: any) {
      setMessage(`Remove failed: ${err.message}`);
    } finally {
      setBusy("");
    }
  };

  const describeQuery = (name: string) => {
    setTab("describe");
    loadQuery(name);
  };

  // ------------------------------------------------------------- register

  const runDiscover = async () => {
    if (!graph) return;
    setBusy("discover");
    setMessage("");
    try {
      const data = await call(`/ui/${graph}/query_tools/discover`, { method: "POST" });
      setMatching(data.matching || []);
      setSkipped(data.skipped || []);
      setMissing(data.missing || []);
      setSelected(new Set());
      await loadRegistered(graph);
    } catch (err: any) {
      setMessage(err.message);
    } finally {
      setBusy("");
    }
  };

  const toggle = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const runRegister = async () => {
    const names = [...selected];
    if (!names.length) return;
    setBusy("register");
    try {
      const data = await call(`/ui/${graph}/query_tools/register`, {
        method: "POST",
        body: JSON.stringify({ names }),
      });
      const failed = (data.failed || [])
        .map((f: Skipped) => `${f.name}: ${f.reason}`)
        .join("; ");
      setMessage(
        `✅ Registered ${data.registered?.length || 0}.` + (failed ? ` Not registered: ${failed}.` : "")
      );
      await runDiscover();
    } catch (err: any) {
      setMessage(`Register failed: ${err.message}`);
    } finally {
      setBusy("");
    }
  };

  const runUnregister = async (namesArg?: string[]) => {
    const names = namesArg || [...selected];
    if (!names.length) return;
    const ok = await confirm(
      `Unregister ${names.join(", ")}? The agent stops using ${
        names.length === 1 ? "it" : "them"
      }. The queries and their descriptions in TigerGraph are not changed.`
    );
    if (!ok) return;
    setBusy("unregister");
    try {
      const data = await call(`/ui/${graph}/query_tools/unregister`, {
        method: "POST",
        body: JSON.stringify({ names }),
      });
      setMessage(`✅ Unregistered ${data.unregistered?.length || 0}.`);
      if (matching) await runDiscover();
      else await loadRegistered(graph);
    } catch (err: any) {
      setMessage(`Unregister failed: ${err.message}`);
    } finally {
      setBusy("");
    }
  };

  const selectedRegistered = [...selected].some(
    (n) => registered.some((r) => r.query === n) || missing.some((m) => m.name === n)
  );
  const selectedRegisterable = [...selected].some((n) => matching?.some((m) => m.query === n));
  const disabled = !!busy;

  // ------------------------------------------------------------- render

  const statusBadge = (status?: string) => (
    <span
      className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
        status === "registered"
          ? "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300"
          : status === "changed"
          ? "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300"
          : "bg-gray-100 text-gray-700 dark:bg-[#3D3D3D] dark:text-[#D9D9D9]"
      }`}
    >
      {STATUS_LABEL[status || "not_registered"]}
    </span>
  );

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          className="sm:max-w-[760px] max-h-[90vh] overflow-y-auto bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]"
          onInteractOutside={(e) => e.preventDefault()}
        >
          <DialogHeader>
            <DialogTitle className="text-black dark:text-white">Query Tools</DialogTitle>
            <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
              Give installed queries a tool description, then choose which ones the chat agent
              can use. Only a graph admin can make these changes.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <label className="text-sm font-medium text-black dark:text-white">Graph</label>
            <Select value={graph} onValueChange={changeGraph} disabled={disabled}>
              <SelectTrigger className={fieldInput}>
                <SelectValue placeholder="Pick a graph" />
              </SelectTrigger>
              <SelectContent>
                {graphs.map((g) => (
                  <SelectItem key={g} value={g}>
                    {g}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <Tabs value={tab} onValueChange={(v) => setTab(v as "describe" | "register")}>
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="register" disabled={disabled}>
                <Search className="h-4 w-4 mr-2" />
                Register
              </TabsTrigger>
              <TabsTrigger value="describe" disabled={disabled}>
                <FileCode className="h-4 w-4 mr-2" />
                Describe
              </TabsTrigger>
            </TabsList>

            {/* ------------------------------------------------ Register */}
            <TabsContent value="register" className="space-y-4">
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                Discover reads the installed queries of the graph and lists those with a tool
                description. Select queries and register them; the agent uses only registered
                queries, as they were when registered.
              </p>

              <div className="space-y-2">
                <div className="text-sm font-medium text-black dark:text-white">
                  Registered tools ({registered.length})
                </div>
                {registered.length === 0 ? (
                  <div className="text-xs text-gray-500 dark:text-gray-400">None yet.</div>
                ) : (
                  registered.map((t) => (
                    <div
                      key={t.query}
                      className="flex items-start justify-between gap-3 p-2 rounded border border-gray-300 dark:border-[#3D3D3D]"
                    >
                      <div className="min-w-0">
                        <div className="font-mono text-sm text-black dark:text-white">{t.query}</div>
                        <div className="text-xs text-gray-600 dark:text-[#D9D9D9]">{t.description}</div>
                        {t.registered_at ? (
                          <div className="text-[11px] text-gray-500 dark:text-gray-400">
                            Registered {new Date(t.registered_at * 1000).toLocaleString()}
                            {t.registered_by ? ` by ${t.registered_by}` : ""}
                          </div>
                        ) : null}
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={disabled}
                        onClick={() => runUnregister([t.query])}
                      >
                        <MinusCircle className="h-3 w-3 mr-1" />
                        Unregister
                      </Button>
                    </div>
                  ))
                )}
              </div>

              <Button onClick={runDiscover} disabled={disabled || !graph} className="gradient text-white">
                {busy === "discover" ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Search className="h-4 w-4 mr-2" />
                )}
                Discover queries
              </Button>

              {matching !== null && (
                <div className="space-y-3">
                  <div className="text-sm font-medium text-black dark:text-white">
                    Queries with a tool description ({matching.length})
                  </div>
                  {matching.length === 0 && (
                    <div className="text-xs text-gray-500 dark:text-gray-400">
                      None. Write a tool description on the Describe tab, or in TigerGraph in the
                      format the README describes.
                    </div>
                  )}
                  {matching.map((m) => (
                    <label
                      key={m.query}
                      className="flex items-start gap-3 p-2 rounded border border-gray-300 dark:border-[#3D3D3D] cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        className="mt-1"
                        checked={selected.has(m.query)}
                        onChange={() => toggle(m.query)}
                        disabled={disabled}
                      />
                      <div className="min-w-0 space-y-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-sm text-black dark:text-white">{m.query}</span>
                          {statusBadge(m.status)}
                        </div>
                        <div className="text-xs text-gray-600 dark:text-[#D9D9D9]">{m.description}</div>
                        {m.params.length > 0 && (
                          <div className="text-[11px] text-gray-500 dark:text-gray-400">
                            Parameters:{" "}
                            {m.params
                              .map((p) => `${p.name} (${p.type}${p.has_default ? ", optional" : ""})`)
                              .join(", ")}
                          </div>
                        )}
                      </div>
                    </label>
                  ))}

                  {missing.length > 0 && (
                    <div className="space-y-2">
                      <div className="text-sm font-medium text-red-700 dark:text-red-400">
                        Registered, but the query is not installed ({missing.length})
                      </div>
                      {missing.map((m) => (
                        <label
                          key={m.name}
                          className="flex items-center gap-3 p-2 rounded border border-red-300 dark:border-red-800 cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            checked={selected.has(m.name)}
                            onChange={() => toggle(m.name)}
                            disabled={disabled}
                          />
                          <span className="font-mono text-sm text-black dark:text-white">{m.name}</span>
                        </label>
                      ))}
                    </div>
                  )}

                  <div className="flex gap-2">
                    <Button
                      onClick={runRegister}
                      disabled={disabled || !selectedRegisterable}
                      className="gradient text-white"
                    >
                      {busy === "register" ? (
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      ) : (
                        <PlusCircle className="h-4 w-4 mr-2" />
                      )}
                      Register selected
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => runUnregister()}
                      disabled={disabled || !selectedRegistered}
                    >
                      <MinusCircle className="h-4 w-4 mr-2" />
                      Unregister selected
                    </Button>
                  </div>

                  {skipped.length > 0 && (
                    <div className="space-y-2">
                      <button
                        type="button"
                        className="text-sm text-gray-700 dark:text-[#D9D9D9] underline"
                        onClick={() => setShowSkipped((s) => !s)}
                      >
                        {showSkipped ? "Hide" : "Show"} skipped queries ({skipped.length})
                      </button>
                      {showSkipped &&
                        skipped.map((s) => (
                          <div
                            key={s.name}
                            className="flex items-center justify-between gap-3 p-2 rounded border border-gray-200 dark:border-[#3D3D3D]"
                          >
                            <div className="min-w-0">
                              <div className="font-mono text-sm text-black dark:text-white">{s.name}</div>
                              <div className="text-xs text-gray-500 dark:text-gray-400">{s.reason}</div>
                            </div>
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={disabled}
                              onClick={() => describeQuery(s.name)}
                            >
                              Describe
                            </Button>
                          </div>
                        ))}
                    </div>
                  )}
                </div>
              )}
            </TabsContent>

            {/* ------------------------------------------------ Describe */}
            <TabsContent value="describe" className="space-y-4">
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                Write what a query returns and when the agent should use it, and describe each
                parameter. Saving a description does not make the query a tool; register it on the
                Register tab.
              </p>

              <div className="flex gap-2 items-end">
                <div className="flex-1 space-y-1">
                  <label className="text-sm font-medium text-black dark:text-white">Query name</label>
                  <Input
                    value={queryName}
                    onChange={(e) => setQueryName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") loadQuery();
                    }}
                    placeholder="e.g. orders_per_month"
                    className={fieldInput}
                    style={INPUT_CLIP_FIX}
                    disabled={disabled}
                  />
                </div>
                <Button
                  variant="outline"
                  onClick={() => loadQuery()}
                  disabled={disabled || !graph || !queryName.trim()}
                >
                  {busy === "load" ? <Loader2 className="h-4 w-4 animate-spin" /> : "Load"}
                </Button>
              </div>

              {loadedName && (
                <div className="space-y-4">
                  {isNewQuery && (
                    <div className="space-y-1">
                      <label className="text-sm font-medium text-black dark:text-white">GSQL</label>
                      <textarea
                        value={gsql}
                        onChange={(e) => setGsql(e.target.value)}
                        rows={6}
                        placeholder={`CREATE OR REPLACE QUERY ${loadedName}() FOR GRAPH ${graph} {\n  PRINT 1 AS result;\n}`}
                        className="w-full rounded-md border px-3 py-2 font-mono text-sm bg-white dark:bg-shadeA border-gray-300 dark:border-[#3D3D3D] text-black dark:text-white"
                        disabled={disabled}
                      />
                      <div className="text-xs text-gray-500 dark:text-gray-400">
                        A full CREATE QUERY statement, or only the query body.
                      </div>
                    </div>
                  )}

                  {(
                    [
                      ["returns", "Returns", "e.g. the number of orders placed in each month", true],
                      ["use_for", "Use for", "e.g. questions about order volume over time", true],
                      ["do_not_use_for", "Do not use for (optional)", "e.g. revenue or refund amounts", false],
                      ["output", "Output fields and units (optional)", "e.g. month (YYYY-MM), orders (count)", false],
                    ] as [keyof Fields, string, string, boolean][]
                  ).map(([key, label, ph]) => (
                    <div key={key} className="space-y-1">
                      <label className="text-sm font-medium text-black dark:text-white">{label}</label>
                      <Input
                        value={fields[key]}
                        onChange={(e) => setFields((f) => ({ ...f, [key]: e.target.value }))}
                        placeholder={ph}
                        className={fieldInput}
                        style={INPUT_CLIP_FIX}
                        disabled={disabled}
                      />
                    </div>
                  ))}

                  {params.length > 0 && (
                    <div className="space-y-2">
                      <div className="text-sm font-medium text-black dark:text-white">Parameters</div>
                      <div className="text-xs text-gray-500 dark:text-gray-400">
                        Say what each parameter means, its format and an example value. A parameter
                        without a default needs a description.
                      </div>
                      {params.map((p) => (
                        <div key={p.name} className="space-y-1">
                          <div className="text-xs text-black dark:text-white">
                            <span className="font-mono">{p.name}</span>{" "}
                            <span className="text-gray-500 dark:text-gray-400">
                              {p.type}
                              {p.has_default ? `, default ${JSON.stringify(p.default)}` : ", required"}
                            </span>
                          </div>
                          <Input
                            value={paramText[p.name] || ""}
                            onChange={(e) => setParamText((t) => ({ ...t, [p.name]: e.target.value }))}
                            placeholder="e.g. First day of the period, YYYY-MM-DD, such as 2024-01-31"
                            className={fieldInput}
                            style={INPUT_CLIP_FIX}
                            disabled={disabled}
                          />
                        </div>
                      ))}
                    </div>
                  )}

                  <div className="space-y-1">
                    <div className="text-xs text-gray-500 dark:text-gray-400">
                      Saved in the query description as
                    </div>
                    <pre className="text-xs whitespace-pre-wrap p-2 rounded bg-gray-50 dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] text-black dark:text-white">
                      {(otherText ? `${otherText}\n` : "") + blockPreview(fields)}
                    </pre>
                  </div>

                  <div className="flex gap-2">
                    <Button onClick={saveDescription} disabled={disabled} className="gradient text-white">
                      {busy === "save" ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
                      {isNewQuery ? "Create query and save description" : "Save description"}
                    </Button>
                    {!isNewQuery && (
                      <Button variant="outline" onClick={removeDescription} disabled={disabled}>
                        <Trash2 className="h-4 w-4 mr-2" />
                        Remove tool description
                      </Button>
                    )}
                  </div>
                </div>
              )}
            </TabsContent>
          </Tabs>

          {message && (
            <div className="text-sm p-3 rounded border bg-gray-50 dark:bg-shadeA border-gray-300 dark:border-[#3D3D3D] text-black dark:text-white whitespace-pre-wrap">
              {message}
            </div>
          )}

          <div className="flex justify-end">
            <Button variant="outline" onClick={() => onOpenChange(false)} disabled={disabled}>
              Close
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      {confirmDialog}
    </>
  );
};

export default QueryToolsDialog;
