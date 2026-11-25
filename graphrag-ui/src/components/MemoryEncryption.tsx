import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, ChevronRight, KeyRound, Loader2, Lock, RotateCw, ShieldCheck, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useRoles } from "@/hooks/useRoles";

type KeyInfo = {
  present: boolean;
  source: "managed" | "mounted";
  active_key_id?: string;
  created_at?: string;
  rotation_pending?: boolean;
};

type TargetInfo = {
  target: string;
  state: "ok" | "encrypting" | "locked" | "failed";
  scope: "global" | "graph";
  job: { state: string; done: number; error: string };
  locked?: { reason: string; needed_key_ids: string[]; scope: string };
  removed?: boolean | null;
  deep_agent?: boolean;
};

type GlobalStatus = { key: KeyInfo; targets: TargetInfo[] };
type GraphStatus = {
  graph: string;
  scope: "global" | "graph";
  key: KeyInfo | null;
  global_key: KeyInfo;
  target: TargetInfo;
  deep_agent: boolean;
  stored: boolean;
};

const CARD = "bg-gray-100 dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6";
const BTN = "dark:border-[#3D3D3D] dark:bg-background";
const CHAT = "chat_history";

const keyName = (scope?: string) => (scope === "graph" ? "graph key" : "global key");
const cap = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);

const lockReason = (reason: string, scope?: string) =>
  ({
    master_key_missing: `The ${keyName(scope)} is missing.`,
    wrong_key: `The ${keyName(scope)} does not match the data key of this memory.`,
    data_key_missing: "The data key is missing.",
  })[reason] || reason;

const targetLabel = (t: string) => (t === CHAT ? "Chat history" : t);

class ApiError extends Error {
  status: number;
  detail: any;
  constructor(status: number, detail: any) {
    super(typeof detail === "string" ? detail : detail?.message || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

async function call(path: string, method = "GET", body?: unknown) {
  const res = await fetch(`/ui/encryption${path}`, {
    method,
    headers: {
      Authorization: sessionStorage.getItem("auth") || "",
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data?.detail);
  return data;
}

async function downloadKey(graph?: string) {
  const ring = await call(`/key/download${graph ? `?graphname=${encodeURIComponent(graph)}` : ""}`);
  const blob = new Blob([JSON.stringify(ring, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = graph ? `graphrag-${graph}-graph-key.json` : "graphrag-global-key.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

function StatusBadge({ t }: { t: TargetInfo }) {
  if (t.state === "locked")
    return (
      <span className="inline-flex items-center gap-1 text-red-700 dark:text-red-300">
        <Lock className="h-3.5 w-3.5" /> Locked
      </span>
    );
  if (t.state === "encrypting")
    return (
      <span className="inline-flex items-center gap-1 text-amber-700 dark:text-amber-300">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Encrypting ({t.job.done})
      </span>
    );
  if (t.state === "failed") return <span className="text-red-700 dark:text-red-300">Failed: {t.job.error}</span>;
  return (
    <span className="inline-flex items-center gap-1 text-green-700 dark:text-green-300">
      <ShieldCheck className="h-3.5 w-3.5" /> Encrypted
    </span>
  );
}

function KeyDetails({ info, name }: { info: KeyInfo; name: string }) {
  if (!info.present) return <p className="text-sm text-red-700 dark:text-red-300">The {name} is missing.</p>;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-sm">
      <dt className="text-gray-600 dark:text-[#D9D9D9]">Source</dt>
      <dd className="text-black dark:text-white">{info.source === "mounted" ? "Mounted secret" : "Managed by GraphRAG"}</dd>
      <dt className="text-gray-600 dark:text-[#D9D9D9]">Key ID</dt>
      <dd className="font-mono text-black dark:text-white">{info.active_key_id}</dd>
      {info.created_at && (
        <>
          <dt className="text-gray-600 dark:text-[#D9D9D9]">Created</dt>
          <dd className="text-black dark:text-white">{new Date(info.created_at).toLocaleString()}</dd>
        </>
      )}
    </dl>
  );
}

function Modal({ title, children, onClose, action }: { title: string; children: React.ReactNode; onClose: () => void; action?: React.ReactNode }) {
  return createPortal(
    <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-[9999]">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="bg-white dark:bg-background p-6 rounded-xl shadow-lg w-[32rem] max-w-[90vw] border border-gray-300 dark:border-[#3D3D3D]"
      >
        <h3 className="text-lg font-semibold mb-4 text-black dark:text-white">{title}</h3>
        {children}
        <div className="flex justify-end gap-2 mt-4">
          <Button variant="outline" className={BTN} onClick={onClose}>
            Cancel
          </Button>
          {action}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function KeyInput({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  const fileRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={fileRef}
        type="file"
        accept=".json,.key,.txt"
        className="hidden"
        onChange={async (e) => {
          const f = e.target.files?.[0];
          if (f) onChange(await f.text());
        }}
      />
      <Button variant="outline" size="sm" className={`${BTN} mb-2`} onClick={() => fileRef.current?.click()}>
        Choose file
      </Button>
      <textarea
        aria-label={label}
        placeholder={`${label} file from GraphRAG, or a 256-bit key in base64`}
        className="w-full h-24 rounded-md border border-gray-300 dark:border-[#3D3D3D] bg-white dark:bg-background p-2 font-mono text-xs text-black dark:text-white"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </>
  );
}

/** Generate a key, or use one the admin provides. */
function NewKeyDialog({
  title,
  name,
  confirmLabel,
  onSubmit,
  onClose,
}: {
  title: string;
  name: string;
  confirmLabel: string;
  onSubmit: (key: string | null) => Promise<void>;
  onClose: () => void;
}) {
  const [own, setOwn] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      await onSubmit(own ? text : null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={title} onClose={onClose}
      action={
        <Button className="gradient text-white" disabled={(own && !text.trim()) || busy} onClick={submit}>
          {busy && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          {confirmLabel}
        </Button>
      }
    >
      <div className="space-y-2 text-sm text-black dark:text-white mb-3">
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="radio" checked={!own} onChange={() => setOwn(false)} /> Generate a new {name}
        </label>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="radio" checked={own} onChange={() => setOwn(true)} /> Use my own {name}
        </label>
      </div>
      {own && <KeyInput value={text} onChange={setText} label={cap(name)} />}
      <p className="text-xs text-gray-600 dark:text-[#D9D9D9] mt-2">The stored memory stays readable.</p>
      {error && <p className="text-sm text-red-700 dark:text-red-300 mt-2">{error}</p>}
    </Modal>
  );
}

/** Upload a key: the missing key of locked data, or the previous mounted key. */
function UploadDialog({
  title,
  name,
  hint,
  onSubmit,
  onClose,
}: {
  title: string;
  name: string;
  hint: string;
  onSubmit: (key: string) => Promise<void>;
  onClose: () => void;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      await onSubmit(text);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={title} onClose={onClose}
      action={
        <Button className="gradient text-white" disabled={!text.trim() || busy} onClick={submit}>
          {busy && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          Upload {name}
        </Button>
      }
    >
      <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-3">{hint}</p>
      <KeyInput value={text} onChange={setText} label={cap(name)} />
      {error && <p className="text-sm text-red-700 dark:text-red-300 mt-2">{error}</p>}
    </Modal>
  );
}

/** A delete that cannot be undone: the admin types a word to confirm. */
function TypedDeleteDialog({
  title,
  body,
  word,
  confirmLabel,
  onSubmit,
  onClose,
}: {
  title: string;
  body: React.ReactNode;
  word: string;
  confirmLabel: string;
  onSubmit: (typed: string) => Promise<void>;
  onClose: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      await onSubmit(typed);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={title} onClose={onClose}
      action={
        <Button variant="destructive" disabled={typed !== word || busy} onClick={submit}>
          {busy && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          {confirmLabel}
        </Button>
      }
    >
      <div className="text-sm text-black dark:text-white mb-3">{body}</div>
      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
        Type <span className="font-mono">{word}</span> to confirm
      </label>
      <Input className={BTN} value={typed} onChange={(e) => setTyped(e.target.value)} />
      {error && <p className="text-sm text-red-700 dark:text-red-300 mt-2">{error}</p>}
    </Modal>
  );
}

const BackupHint = ({ graph, canDownload }: { graph?: string; canDownload: boolean }) => (
  <div className="flex items-center justify-between gap-4 mt-4 p-3 rounded-md border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 text-sm text-amber-800 dark:text-amber-200">
    <span>Keep a backup of the {graph ? "graph key" : "global key"} apart from the data. Without it, the stored memory cannot be read.</span>
    {canDownload && (
      <Button variant="outline" size="sm" className={`${BTN} shrink-0`} onClick={() => downloadKey(graph)}>
        Download {graph ? "graph key" : "global key"}
      </Button>
    )}
  </div>
);

function Section({
  title,
  hint,
  open,
  onToggle,
  action,
  children,
}: {
  title: string;
  hint?: string;
  open: boolean;
  onToggle: () => void;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button type="button" onClick={onToggle} aria-expanded={open}
          className="flex items-center gap-1 text-sm font-medium text-black dark:text-white">
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          {title}
        </button>
        {action}
      </div>
      {hint && <p className="text-xs text-gray-600 dark:text-[#D9D9D9] ml-5 mb-1">{hint}</p>}
      {open && <div className="mt-2">{children}</div>}
    </div>
  );
}

type Dialog =
  | { kind: "rotate"; graph?: string }
  | { kind: "own"; graph: string }
  | { kind: "upload"; graph?: string; scope: string; previous: boolean; needed: string[] }
  | { kind: "reset"; target: string }
  | { kind: "delete"; graphs: string[]; removed: boolean; locked?: boolean }
  | { kind: "confirm"; text: string; run: () => void }
  | null;

type Props = {
  scope: "global" | "graph";
  graph: string;
  /** False when the page around it already shows the title. */
  showHeader?: boolean;
};

const MemoryEncryption = ({ scope, graph, showHeader = true }: Props) => {
  const { isSuperuser, isGlobalDesigner, rolesLoaded } = useRoles();
  const [globalStatus, setGlobalStatus] = useState<GlobalStatus | null>(null);
  const [graphStatus, setGraphStatus] = useState<GraphStatus | null>(null);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [showActive, setShowActive] = useState(true);
  const [showRemoved, setShowRemoved] = useState(false);
  const isGraph = scope === "graph" && !!graph;
  const allowed = isSuperuser || isGlobalDesigner;

  const load = useCallback(async () => {
    try {
      if (isGraph) setGraphStatus(await call(`/status?graphname=${encodeURIComponent(graph)}`));
      else setGlobalStatus(await call("/status"));
    } catch (e: any) {
      setMsg({ text: e.message, ok: false });
    }
  }, [isGraph, graph]);

  useEffect(() => {
    if (allowed) load();
  }, [allowed, load]);

  const running = isGraph
    ? graphStatus?.target.state === "encrypting"
    : globalStatus?.targets.some((t) => t.state === "encrypting");
  useEffect(() => {
    if (!running) return;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [running, load]);

  const done = (text: string, ok = true) => {
    setDialog(null);
    setMsg({ text, ok });
    load();
  };

  const act = async (fn: () => Promise<unknown>, okText: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      done(okText);
    } catch (e: any) {
      done(e.message, false);
    } finally {
      setBusy(false);
    }
  };

  const ask = (text: string, fn: () => Promise<unknown>, okText: string) =>
    setDialog({ kind: "confirm", text, run: () => act(fn, okText) });

  // A mounted key: Rotate key checks the secret and asks for the previous
  // key only when GraphRAG cannot open the data keys itself.
  const rotateMounted = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await call("/rotate_master", "POST", {});
      if (r.result === "unchanged")
        done("The mounted global key did not change. Back up the current global key, put the new global key into the secret, then click Rotate global key again.");
      else done("The data keys now use the mounted global key.");
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 409)
        setDialog({ kind: "upload", scope: "global", previous: true, needed: e.detail?.needed_key_ids || [] });
      else done(e.message, false);
    } finally {
      setBusy(false);
    }
  };

  if (!rolesLoaded || !allowed) return null;

  const header = showHeader && (
    <>
      <h2 className="text-lg font-semibold mb-1 text-black dark:text-white flex items-center gap-2">
        <KeyRound className="h-5 w-5 text-tigerOrange" /> Memory Encryption
      </h2>
      <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
        {isGraph
          ? "A data key encrypts the agent memory of this graph on disk. The global key, or a graph key, protects the data key."
          : "Data keys encrypt the agent memory and chat history stored on disk. The global key, or a graph key, protects the data keys."}
      </p>
    </>
  );

  const message = msg && (
    <p className={`text-sm mb-3 ${msg.ok ? "text-green-700 dark:text-green-300" : "text-red-700 dark:text-red-300"}`}>
      {msg.text}
    </p>
  );

  const lockedHelp = (t: TargetInfo) =>
    t.locked && (
      <p className="text-xs text-red-700 dark:text-red-300 mt-1">
        {lockReason(t.locked.reason, t.locked.scope)}
        {t.locked.needed_key_ids.length > 0 && <> Needs {keyName(t.locked.scope)} {t.locked.needed_key_ids.join(", ")}.</>}
      </p>
    );

  const dialogs = (
    <>
      {dialog?.kind === "rotate" && (
        <NewKeyDialog
          title={dialog.graph ? `Rotate the graph key of ${dialog.graph}` : "Rotate the global key"}
          name={dialog.graph ? "graph key" : "global key"}
          confirmLabel={dialog.graph ? "Rotate graph key" : "Rotate global key"}
          onClose={() => setDialog(null)}
          onSubmit={async (key) => {
            await call("/rotate_master", "POST", { graphname: dialog.graph || null, key });
            done(dialog.graph ? "The graph key is rotated." : "The global key is rotated.");
          }}
        />
      )}
      {dialog?.kind === "own" && (
        <NewKeyDialog
          title={`Use a graph key for ${dialog.graph}`}
          name="graph key"
          confirmLabel="Use graph key"
          onClose={() => setDialog(null)}
          onSubmit={async (key) => {
            await call("/graph_key", "POST", { graphname: dialog.graph, key });
            done("This graph uses a graph key.");
          }}
        />
      )}
      {dialog?.kind === "upload" && (
        <UploadDialog
          title={dialog.previous ? "Upload the previous global key" : `Upload the ${keyName(dialog.scope)}`}
          name={keyName(dialog.scope)}
          hint={
            dialog.previous
              ? `The mounted global key is new. Upload the global key it replaces${dialog.needed.length ? ` (${dialog.needed.join(", ")})` : ""} to finish the rotation. The previous global key is not stored.`
              : `Upload the ${keyName(dialog.scope)} of the locked memory${dialog.needed.length ? ` (${dialog.needed.join(", ")})` : ""}.`
          }
          onClose={() => setDialog(null)}
          onSubmit={async (key) => {
            if (dialog.previous) await call("/rotate_master", "POST", { previous_key: key });
            else await call("/key", "POST", { graphname: dialog.graph || null, key });
            done(dialog.previous ? "The data keys now use the mounted global key." : `The ${keyName(dialog.scope)} is restored.`);
          }}
        />
      )}
      {dialog?.kind === "reset" && (
        <TypedDeleteDialog
          title={`Reset ${targetLabel(dialog.target)}`}
          word={dialog.target}
          confirmLabel="Delete"
          body={`This deletes the stored ${dialog.target === CHAT ? "chat messages" : "conversations and memories"} that cannot be read without the lost keys. It cannot be undone.`}
          onClose={() => setDialog(null)}
          onSubmit={async (typed) => {
            await call("/reset", "POST", { target: dialog.target, confirm: typed });
            done(`The locked data of ${targetLabel(dialog.target)} is deleted.`);
          }}
        />
      )}
      {dialog?.kind === "delete" && (
        <TypedDeleteDialog
          title={dialog.graphs.length === 1 ? `Delete memory of ${dialog.graphs[0]}` : "Delete memory of removed graphs"}
          word={dialog.graphs.length === 1 ? dialog.graphs[0] : "delete"}
          confirmLabel="Delete"
          body={
            dialog.removed ? (
              <>
                <p className="mb-2">
                  {dialog.graphs.length === 1 ? "This graph no longer exists" : "These graphs no longer exist"} in
                  TigerGraph. This deletes the stored conversations, memories and keys. It cannot be undone.
                </p>
                {dialog.graphs.length > 1 && (
                  <ul className="list-disc pl-5 max-h-32 overflow-auto text-gray-700 dark:text-[#D9D9D9]">
                    {dialog.graphs.map((g) => <li key={g}>{g}</li>)}
                  </ul>
                )}
              </>
            ) : (
              <p>
                This deletes the stored conversations and memories of {dialog.graphs[0]} for all users
                {dialog.locked ? ", and replaces its data key" : ""}. Agent mode starts fresh on this graph. The graph
                and its documents do not change. It cannot be undone.
              </p>
            )
          }
          onClose={() => setDialog(null)}
          onSubmit={async (typed) => {
            await call("/delete_memory", "POST", { graphs: dialog.graphs, confirm: typed });
            done(dialog.graphs.length === 1 ? `The memory of ${dialog.graphs[0]} is deleted.` : `The memory of ${dialog.graphs.length} removed graphs is deleted.`);
          }}
        />
      )}
      {dialog?.kind === "confirm" && (
        <ConfirmDialog
          message={dialog.text}
          onCancel={() => setDialog(null)}
          onConfirm={() => {
            const run = dialog.run;
            setDialog(null);
            run();
          }}
        />
      )}
    </>
  );

  if (isGraph) {
    const s = graphStatus;
    if (!s) return <div className={CARD}>{header}<Loader2 className="h-4 w-4 animate-spin" /></div>;
    const t = s.target;
    const own = s.scope === "graph";
    const mountedGlobal = !own && s.global_key.source === "mounted";
    const locked = t.state === "locked";
    if (!s.deep_agent)
      return (
        <div className={CARD}>
          {header}
          {message}
          <p className="text-sm text-black dark:text-white">
            This graph does not use the Deep Agent, so it keeps no agent memory. Its chat history is protected by the
            global key.
          </p>
          {s.stored && (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2 mt-6 mb-2">
                <h3 className="text-sm font-semibold text-black dark:text-white">Stored memory</h3>
                <StatusBadge t={t} />
              </div>
              <p className="text-xs text-gray-600 dark:text-[#D9D9D9]">Agent memory from an earlier use of the Deep Agent.</p>
              <div className="flex flex-wrap gap-2 mt-2">
                <Button variant="destructive" disabled={busy} onClick={() => setDialog({ kind: "delete", graphs: [graph], removed: false, locked })}>
                  <Trash2 className="h-4 w-4 mr-2" />
                  Delete
                </Button>
              </div>
            </>
          )}
          {dialogs}
        </div>
      );
    const choice = (label: string, selected: boolean, onPick: () => void) => (
      <button type="button" role="radio" aria-checked={selected} disabled={busy || locked || selected} onClick={onPick}
        className={`px-3 py-1.5 text-sm ${selected
          ? "bg-tigerOrange text-white"
          : "bg-white dark:bg-background text-black dark:text-white hover:bg-gray-200 dark:hover:bg-shadeA disabled:opacity-50"}`}>
        {label}
      </button>
    );
    return (
      <div className={CARD}>
        {header}
        {message}
        <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
          <h3 className="text-sm font-semibold text-black dark:text-white">Protected by</h3>
          <div className="flex flex-wrap gap-2">
            {locked && (
              <Button variant="outline" size="sm" className={BTN} disabled={busy}
                onClick={() => (mountedGlobal ? rotateMounted() : setDialog({ kind: "upload", graph, scope: s.scope, previous: false, needed: t.locked?.needed_key_ids || [] }))}>
                Upload {keyName(s.scope)}
              </Button>
            )}
            {own && !locked && (
              <Button variant="outline" size="sm" className={BTN} disabled={busy} onClick={() => setDialog({ kind: "rotate", graph })}>
                <RotateCw className="h-4 w-4 mr-2" />
                Rotate graph key
              </Button>
            )}
          </div>
        </div>
        <div role="radiogroup" aria-label="Protected by"
          className="inline-flex rounded-md overflow-hidden border border-gray-300 dark:border-[#3D3D3D] divide-x divide-gray-300 dark:divide-[#3D3D3D] mb-3">
          {choice("Global key", !own, () =>
            ask("Use the global key for this graph? The graph key is then deleted.",
              () => call("/global_key", "POST", { graphname: graph }), "This graph uses the global key."))}
          {choice("Graph key", own, () => setDialog({ kind: "own", graph }))}
        </div>
        {own ? <KeyDetails info={s.key || { present: false, source: "managed" }} name="graph key" />
             : <KeyDetails info={s.global_key} name="global key" />}
        {own && s.key?.present && <BackupHint graph={graph} canDownload={isSuperuser} />}

        <div className="flex flex-wrap items-center justify-between gap-2 mt-6 mb-2">
          <h3 className="text-sm font-semibold text-black dark:text-white">Stored memory</h3>
          <StatusBadge t={t} />
        </div>
        {lockedHelp(t)}
        <div className="flex flex-wrap gap-2 mt-2">
          {!locked && (
            <Button variant="outline" className={BTN} disabled={busy || t.state === "encrypting"}
              onClick={() => ask("Encrypt the stored memory of this graph again with a new data key?",
                () => call("/rotate_data_keys", "POST", { target: graph }), "Data key rotation started.")}>
              <RotateCw className="h-4 w-4 mr-2" />
              Rotate data key
            </Button>
          )}
          <Button variant="destructive" disabled={busy} onClick={() => setDialog({ kind: "delete", graphs: [graph], removed: false, locked })}>
            <Trash2 className="h-4 w-4 mr-2" />
            Delete
          </Button>
        </div>
        {dialogs}
      </div>
    );
  }

  const s = globalStatus;
  if (!s) return <div className={CARD}>{header}<Loader2 className="h-4 w-4 animate-spin" /></div>;
  const mounted = s.key.source === "mounted";
  const globalLocked = s.targets.filter((t) => t.state === "locked" && t.scope === "global" && !t.removed);
  const chat = s.targets.find((t) => t.target === CHAT);
  const active = s.targets.filter((t) => t.target !== CHAT && !t.removed);
  const removedRows = s.targets.filter((t) => t.removed);

  const targetList = (rows: TargetInfo[]) => (
    <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-md divide-y divide-gray-300 dark:divide-[#3D3D3D] bg-white dark:bg-background">
      {rows.map((t) => (
        <div key={t.target} className="flex items-start justify-between gap-4 px-3 py-2 text-sm">
          <div className="min-w-0">
            <div className="text-black dark:text-white truncate">{targetLabel(t.target)}</div>
            <div className="text-xs text-gray-600 dark:text-[#D9D9D9]">
              {t.scope === "graph" ? "Graph key" : "Global key"}
              {t.deep_agent === false && !t.removed && " · Deep Agent not used"}
            </div>
            {lockedHelp(t)}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <StatusBadge t={t} />
            {!t.removed && t.state !== "locked" && t.deep_agent !== false ? (
              <Button variant="ghost" size="sm" disabled={busy || t.state === "encrypting"} title="Rotate data key"
                aria-label={`Rotate data key of ${targetLabel(t.target)}`}
                onClick={() => ask(`Encrypt the stored memory of ${targetLabel(t.target)} again with a new data key?`,
                  () => call("/rotate_data_keys", "POST", { target: t.target }), "Data key rotation started.")}>
                <RotateCw size={14} />
              </Button>
            ) : (
              t.target !== CHAT && <span aria-hidden="true" className="inline-block w-[38px]" />
            )}
            {t.target === CHAT ? (
              t.state === "locked" ? (
                <Button variant="ghost" size="sm" disabled={busy} title="Reset" aria-label="Reset chat history"
                  onClick={() => setDialog({ kind: "reset", target: t.target })}>
                  <Trash2 size={14} />
                </Button>
              ) : (
                <span aria-hidden="true" className="inline-block w-[38px]" />
              )
            ) : (
              <Button variant="ghost" size="sm" disabled={busy} title="Delete" aria-label={`Delete memory of ${t.target}`}
                onClick={() => setDialog({ kind: "delete", graphs: [t.target], removed: !!t.removed, locked: t.state === "locked" })}>
                <Trash2 size={14} />
              </Button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
  return (
    <div className={CARD}>
      {header}
      {message}
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
        <h3 className="text-sm font-semibold text-black dark:text-white">Global key</h3>
        <div className="flex flex-wrap gap-2">
          {globalLocked.length > 0 && (
            <Button variant="outline" size="sm" className={BTN} disabled={busy}
              onClick={() => (mounted ? rotateMounted() : setDialog({ kind: "upload", scope: "global", previous: false,
                needed: Array.from(new Set(globalLocked.flatMap((t) => t.locked?.needed_key_ids || []))) }))}>
              Upload global key
            </Button>
          )}
          <Button variant="outline" size="sm" className={BTN} disabled={busy}
            onClick={() => (mounted ? rotateMounted() : setDialog({ kind: "rotate" }))}>
            <RotateCw className="h-4 w-4 mr-2" />
            Rotate global key
          </Button>
        </div>
      </div>
      <KeyDetails info={s.key} name="global key" />
      {mounted && s.key.rotation_pending && (
        <p className="text-sm text-amber-700 dark:text-amber-300 mt-2">The mounted global key is new. Click Rotate global key to use it.</p>
      )}
      {s.key.present && <BackupHint canDownload={isSuperuser && !mounted} />}

      <div className="flex flex-wrap items-center justify-between gap-2 mt-6 mb-2">
        <h3 className="text-sm font-semibold text-black dark:text-white">Stored memory</h3>
        <Button variant="outline" size="sm" className={BTN} disabled={busy}
          onClick={() => ask("Encrypt the stored memory of all graphs and the chat history again with new data keys?",
            () => call("/rotate_data_keys", "POST", { target: "all" }), "Data key rotation started.")}>
          <RotateCw className="h-4 w-4 mr-2" />
          Rotate data keys
        </Button>
      </div>
      <div className="space-y-3">
        {chat && targetList([chat])}
        <Section
          title={`Active graphs (${active.length})`}
          open={showActive}
          onToggle={() => setShowActive(!showActive)}
        >
          {targetList(active)}
        </Section>
        {removedRows.length > 0 && (
          <Section
            title={`Removed graphs (${removedRows.length})`}
            hint="These graphs no longer exist in TigerGraph."
            open={showRemoved}
            onToggle={() => setShowRemoved(!showRemoved)}
            action={
              <Button variant="ghost" size="sm" disabled={busy} title="Delete all"
                onClick={() => setDialog({ kind: "delete", graphs: removedRows.map((t) => t.target), removed: true })}>
                <Trash2 size={14} className="mr-1" /> Delete all
              </Button>
            }
          >
            {targetList(removedRows)}
          </Section>
        )}
      </div>
      {dialogs}
    </div>
  );
};

export default MemoryEncryption;
