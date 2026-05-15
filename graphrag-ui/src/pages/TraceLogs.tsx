import { FC, useState, useMemo, useEffect } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  LuArrowLeft,
  LuChevronDown,
  LuChevronUp,
  LuCopy,
  LuDownload,
  LuWrench,
  LuBookOpen,
  LuActivity,
  LuCoins,
  LuInfo,
} from "react-icons/lu";

// ─── Types ────────────────────────────────────────────────────────────────────

interface TraceLogEntry {
  id: number;
  type: "tool_call" | "citation";
  timestamp: string;
  label: string;
  detail?: string;
  durationMs?: number;
  content?: string;
  step?: number;
}

interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost: number;
}

interface LlmCall extends TokenUsage {
  caller_name: string;
}

interface ToolCallEntry {
  id: number;
  name: string;
  timestamp: string;
  durationMs: number;
  input?: string;
  output?: string;
  usage?: TokenUsage & { calls?: LlmCall[] };
}

interface CitationEntry {
  id: number;
  source: string;
  cited: boolean;
  text: string;
}

interface TimelineStep {
  step: number;
  name: string;
  durationMs: number;
}

interface TraceData {
  originalQuery: string;
  conversationContext: string[];
  status: "completed" | "in_progress" | "failed";
  sessionId: string;
  timing: {
    totalDuration: number;
    toolExecution: number;
    llmThinking: number;
    startTime: string;
    endTime: string;
  };
  logs: TraceLogEntry[];
  toolCalls: ToolCallEntry[];
  citations: CitationEntry[];
  timeline: TimelineStep[];
  tokenUsage: TokenUsage;
  finalResponse: string;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatDuration(seconds: number): string {
  if (seconds < 0.01) return `${Math.round(seconds * 1000)}ms`;
  return `${seconds.toFixed(2)}s`;
}

function safeJson(obj: any): string {
  if (obj == null) return "N/A";
  if (typeof obj === "string") {
    try {
      return JSON.stringify(JSON.parse(obj), null, 2);
    } catch {
      return obj;
    }
  }
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}

const NODE_LABELS: Record<string, string> = {
  entry: "Entry",
  supportai: "SupportAI",
  map_question_to_schema: "Map Question to Schema",
  generate_function: "Generate Function",
  generate_cypher: "Generate Cypher",
  generate_answer: "Generate Answer",
  lookup_history: "Lookup History",
  merge_history_context: "Merge History Context",
  rewrite_question: "Rewrite Question",
  apologize: "Apologize",
  greet: "Greet",
};

function buildTraceFromMessage(message: any, userQuery?: string): TraceData {
  const now = new Date();
  const sessionTs = now.toISOString().replace(/[-:T]/g, "").slice(0, 15);
  const sessionId = `chat_${sessionTs}`;

  const query = userQuery || message?.originalQuery || message?.query || "N/A";
  const qs = message?.query_sources || {};
  const totalResponseTime = message?.response_time || 0;
  const ts = now.toLocaleTimeString();

  // ── Tool Calls ──────────────────────────────────────────────────────────
  const toolCalls: ToolCallEntry[] = [];
  const agentSteps: {
    node: string;
    duration_s: number;
    input?: string;
    output?: string;
    usage?: TokenUsage & { calls?: LlmCall[] };
  }[] = qs.agent_steps || [];

  if (agentSteps.length > 0) {
    agentSteps.forEach((step, i: number) => {
      toolCalls.push({
        id: i + 1,
        name: NODE_LABELS[step.node] || step.node,
        timestamp: ts,
        durationMs: Math.round(step.duration_s * 1000),
        input: safeJson(step.input),
        output: safeJson(step.output),
        usage: step.usage,
      });
    });
  }

  // ── Citations ───────────────────────────────────────────────────────────
  const rawReasoning = qs.reasoning;
  const citations: CitationEntry[] = [];

  if (rawReasoning && Array.isArray(rawReasoning)) {
    rawReasoning.forEach((src: any, i: number) => {
      if (src == null) return;
      const raw = typeof src === "string" ? src : String(src);
      const cited = raw.startsWith("* ");
      const chunkName = raw.replace(/^\*\s*/, "");

      citations.push({
        id: i + 1,
        source: chunkName,
        cited,
        text: "",
      });
    });
  }

  // ── Logs ────────────────────────────────────────────────────────────────
  const logs: TraceLogEntry[] = [];
  let logId = 0;
  toolCalls.forEach((tc) => {
    logs.push({
      id: logId++,
      type: "tool_call",
      timestamp: tc.timestamp,
      label: `${tc.name} — Input`,
      content: tc.input,
      durationMs: tc.durationMs,
    });
    logs.push({
      id: logId++,
      type: "citation",
      timestamp: tc.timestamp,
      label: `${tc.name} — Output`,
      content: tc.output,
    });
  });

  // ── Timeline ────────────────────────────────────────────────────────────
  const timeline: TimelineStep[] = toolCalls.map((tc, i) => ({
    step: i + 1,
    name: tc.name,
    durationMs: tc.durationMs,
  }));

  const totalToolSec = agentSteps.reduce(
    (sum: number, s: { duration_s: number }) => sum + s.duration_s,
    0
  );
  const llmThinking = Math.max(0, totalResponseTime - totalToolSec);
  const endTime = new Date(now.getTime() + totalResponseTime * 1000);

  // ── Token usage totals ─────────────────────────────────────────────────
  const serverTotal = qs.token_usage as TokenUsage | undefined;
  const tokenUsage: TokenUsage = serverTotal || agentSteps.reduce(
    (acc, s) => {
      const u = s.usage;
      if (!u) return acc;
      return {
        input_tokens: acc.input_tokens + (u.input_tokens || 0),
        output_tokens: acc.output_tokens + (u.output_tokens || 0),
        total_tokens: acc.total_tokens + (u.total_tokens || 0),
        cost: acc.cost + (u.cost || 0),
      };
    },
    { input_tokens: 0, output_tokens: 0, total_tokens: 0, cost: 0 } as TokenUsage
  );

  return {
    originalQuery: query,
    conversationContext: [`user: ${query}`],
    status: "completed",
    sessionId,
    timing: {
      totalDuration: totalResponseTime,
      toolExecution: totalToolSec,
      llmThinking,
      startTime: now.toLocaleTimeString(),
      endTime: endTime.toLocaleTimeString(),
    },
    logs,
    toolCalls,
    citations,
    timeline,
    tokenUsage,
    finalResponse: message?.content || "",
  };
}

function formatCost(cost: number): string {
  if (!cost) return "$0.00";
  if (cost < 0.01) return `$${cost.toFixed(6)}`;
  return `$${cost.toFixed(4)}`;
}

function formatNumber(n: number): string {
  return (n || 0).toLocaleString();
}

function formatCallerNames(calls: { caller_name: string }[]): string {
  if (!calls || calls.length === 0) return "—";
  const counts: Record<string, number> = {};
  calls.forEach((c) => {
    counts[c.caller_name] = (counts[c.caller_name] || 0) + 1;
  });
  return Object.entries(counts)
    .map(([name, count]) => (count > 1 ? `${name} ×${count}` : name))
    .join(", ");
}

// ─── Sub-components ───────────────────────────────────────────────────────────

const StatusBadge: FC<{ status: string }> = ({ status }) => {
  const color =
    status === "completed"
      ? "bg-emerald-500"
      : status === "in_progress"
        ? "bg-blue-500"
        : "bg-red-500";
  return (
    <span
      className={`${color} text-white text-xs font-medium px-3 py-1 rounded-full`}
    >
      {status}
    </span>
  );
};

const TimingRow: FC<{
  items: { value: string; label: string; color: string }[];
}> = ({ items }) => (
  <div className="flex border border-border rounded-lg overflow-hidden bg-card">
    {items.map((item, i) => (
      <div
        key={item.label}
        className={`flex-1 flex flex-col items-center justify-center py-5 ${
          i < items.length - 1 ? "border-r border-border" : ""
        }`}
      >
        <span className={`text-2xl font-bold ${item.color}`}>
          {item.value}
        </span>
        <span className="text-xs text-muted-foreground mt-1">
          {item.label}
        </span>
      </div>
    ))}
  </div>
);

const ExpandableRow: FC<{
  children: React.ReactNode;
  content?: string;
  defaultOpen?: boolean;
}> = ({ children, content, defaultOpen = false }) => {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border border-border rounded-lg mb-2 overflow-hidden">
      <div
        className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-muted/50 transition-colors"
        onClick={() => setOpen((p) => !p)}
      >
        <div className="flex items-center gap-3 flex-1 min-w-0">
          {children}
        </div>
        <div className="flex items-center gap-2 ml-2 shrink-0">
          {content && (
            <button
              className="p-1 hover:bg-muted rounded"
              onClick={(e) => {
                e.stopPropagation();
                navigator.clipboard.writeText(content);
              }}
              title="Copy"
            >
              <LuCopy className="w-4 h-4 text-muted-foreground" />
            </button>
          )}
          {open ? (
            <LuChevronUp className="w-4 h-4 text-muted-foreground" />
          ) : (
            <LuChevronDown className="w-4 h-4 text-muted-foreground" />
          )}
        </div>
      </div>
      {open && content && (
        <div className="px-4 pb-3 text-sm text-muted-foreground border-t border-border pt-3">
          <pre className="whitespace-pre-wrap font-sans overflow-auto max-h-[500px]">{content}</pre>
        </div>
      )}
    </div>
  );
};

// ─── Tab Panels ───────────────────────────────────────────────────────────────

const LogsPanel: FC<{ trace: TraceData }> = ({ trace }) => {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div>
      <div className="flex items-center justify-between mb-4 text-sm">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-muted-foreground">
            {trace.logs.length} agent steps
          </span>
          <span className="bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 text-xs px-2 py-0.5 rounded-full">
            Nodes ({trace.toolCalls.length})
          </span>
          <span className="bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 text-xs px-2 py-0.5 rounded-full">
            Citations ({trace.citations.length})
          </span>
        </div>
        <button
          className="text-blue-600 dark:text-blue-400 text-xs hover:underline"
          onClick={() => setCollapsed((p) => !p)}
        >
          {collapsed ? "Expand All" : "Collapse All"}
        </button>
      </div>

      <div className="space-y-0">
        {trace.logs.map((log) => (
          <div key={log.id} className="flex items-start gap-3">
            <div className="flex flex-col items-center pt-5">
              <div className="w-2.5 h-2.5 rounded-full bg-blue-500" />
              <div className="w-px h-full bg-border min-h-[20px]" />
            </div>
            <div className="flex-1 min-w-0">
              <ExpandableRow
                content={log.content}
                defaultOpen={!collapsed && log.id === 0}
              >
                <span className="text-xs font-medium text-blue-600 dark:text-blue-400">
                  <LuWrench className="inline w-3.5 h-3.5 mr-1" />
                  Node
                </span>
                <span className="text-xs text-muted-foreground">
                  {log.timestamp}
                </span>
                <span className="text-sm font-medium truncate">
                  {log.label}
                </span>
                {log.durationMs != null && log.durationMs > 0 && (
                  <span className="text-xs text-emerald-600 dark:text-emerald-400">
                    ({formatDuration(log.durationMs / 1000)})
                  </span>
                )}
              </ExpandableRow>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

const ToolCallExpandable: FC<{ tc: ToolCallEntry }> = ({ tc }) => {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-border rounded-lg mb-2 overflow-hidden">
      <div
        className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-muted/50 transition-colors"
        onClick={() => setOpen((p) => !p)}
      >
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <span className="flex items-center justify-center w-7 h-7 rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 text-xs font-bold shrink-0">
            {tc.id}
          </span>
          <span className="text-sm font-semibold truncate">{tc.name}</span>
          <span className="text-xs text-muted-foreground">{tc.timestamp}</span>
        </div>
        <div className="flex items-center gap-2 ml-2 shrink-0">
          {tc.usage && tc.usage.total_tokens > 0 && (
            <span
              className="bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300 text-xs font-medium px-2 py-0.5 rounded-full"
              title={`Input ${tc.usage.input_tokens} / Output ${tc.usage.output_tokens} / Cost ${formatCost(tc.usage.cost)}`}
            >
              {formatNumber(tc.usage.total_tokens)} tokens
            </span>
          )}
          {tc.durationMs > 0 && (
            <span className="bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 text-xs font-medium px-2 py-0.5 rounded-full">
              {formatDuration(tc.durationMs / 1000)}
            </span>
          )}
          {open ? (
            <LuChevronUp className="w-4 h-4 text-muted-foreground" />
          ) : (
            <LuChevronDown className="w-4 h-4 text-muted-foreground" />
          )}
        </div>
      </div>
      {open && (
        <div className="px-4 pb-4 space-y-3">
          {tc.usage && tc.usage.total_tokens > 0 && (
            <div>
              <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
                LLM Usage
              </p>
              <div className="grid grid-cols-4 gap-2">
                <div className="bg-muted rounded-md p-2">
                  <div className="text-[10px] text-muted-foreground uppercase">Input</div>
                  <div className="text-sm font-semibold">{formatNumber(tc.usage.input_tokens)}</div>
                </div>
                <div className="bg-muted rounded-md p-2">
                  <div className="text-[10px] text-muted-foreground uppercase">Output</div>
                  <div className="text-sm font-semibold">{formatNumber(tc.usage.output_tokens)}</div>
                </div>
                <div className="bg-muted rounded-md p-2">
                  <div className="text-[10px] text-muted-foreground uppercase">Total</div>
                  <div className="text-sm font-semibold">{formatNumber(tc.usage.total_tokens)}</div>
                </div>
                <div className="bg-muted rounded-md p-2">
                  <div className="text-[10px] text-muted-foreground uppercase">Cost</div>
                  <div className="text-sm font-semibold">{formatCost(tc.usage.cost)}</div>
                </div>
              </div>
                {tc.usage.calls && tc.usage.calls.length > 0 && (
                  <div className="mt-2 text-xs text-muted-foreground">
                    {tc.usage.calls.length} LLM call{tc.usage.calls.length !== 1 ? "s" : ""}:{" "}
                    {formatCallerNames(tc.usage.calls)}
                  </div>
                )}
            </div>
          )}
          <div>
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1">
              Input
            </p>
            <pre className="bg-[#1e1e2e] dark:bg-[#0d1117] text-emerald-300 text-xs rounded-lg p-4 overflow-auto max-h-[500px] whitespace-pre-wrap">
              {tc.input || "N/A"}
            </pre>
          </div>
          <div>
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1">
              Output
            </p>
            <pre className="bg-[#1e1e2e] dark:bg-[#0d1117] text-blue-300 text-xs rounded-lg p-4 overflow-auto max-h-[500px] whitespace-pre-wrap">
              {tc.output || "N/A"}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
};

const ToolCallsPanel: FC<{ trace: TraceData }> = ({ trace }) => (
  <div className="space-y-2">
    {trace.toolCalls.map((tc) => (
      <ToolCallExpandable key={tc.id} tc={tc} />
    ))}
  </div>
);


const CitationRow: FC<{ c: CitationEntry }> = ({ c }) => (
  <div
    className={`rounded-lg mb-2 ${
      c.cited
        ? "bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800"
        : "bg-orange-50 dark:bg-orange-900/15 border border-orange-200 dark:border-orange-800"
    }`}
  >
    <div className="flex items-center gap-3 px-4 py-3">
      <LuBookOpen className="w-4 h-4 text-amber-700 dark:text-amber-400 shrink-0" />
      <span className="text-sm font-semibold truncate">[{c.source}]</span>
      {c.cited && (
        <span className="bg-red-500 text-white text-xs font-medium px-2.5 py-0.5 rounded-full shrink-0">
          Cited
        </span>
      )}
    </div>
  </div>
);

const CitationsPanel: FC<{ trace: TraceData }> = ({ trace }) => (
  <div className="space-y-2">
    {trace.citations.length === 0 ? (
      <p className="text-sm text-muted-foreground py-4">
        No citations available for this trace.
      </p>
    ) : (
      trace.citations.map((c) => <CitationRow key={c.id} c={c} />)
    )}
  </div>
);

const TimelinePanel: FC<{ trace: TraceData }> = ({ trace }) => (
  <div className="relative pl-4">
    {trace.timeline.map((item, i) => (
      <div key={i} className="flex items-start gap-6 mb-6 last:mb-0">
        <div className="w-16 text-sm text-muted-foreground pt-1 shrink-0">
          Step {item.step}
        </div>
        <div className="flex flex-col items-start gap-1">
          <span className="bg-blue-600 text-white text-xs font-bold px-3 py-1 rounded-full">
            {formatDuration(item.durationMs / 1000)}
          </span>
          <span className="text-sm text-muted-foreground">{item.name}</span>
        </div>
      </div>
    ))}
  </div>
);

const TokenOverviewPanel: FC<{ trace: TraceData }> = ({ trace }) => {
  const usage = trace.tokenUsage;
  const nodesWithUsage = trace.toolCalls.filter(
    (tc) => tc.usage && tc.usage.total_tokens > 0
  );

  return (
    <div className="space-y-5">
      {/* Totals */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="bg-card border border-border rounded-lg p-4">
          <div className="text-xs text-muted-foreground uppercase tracking-wide">
            Input Tokens
          </div>
          <div className="text-2xl font-bold text-blue-600 dark:text-blue-400 mt-1">
            {formatNumber(usage.input_tokens)}
          </div>
        </div>
        <div className="bg-card border border-border rounded-lg p-4">
          <div className="text-xs text-muted-foreground uppercase tracking-wide">
            Output Tokens
          </div>
          <div className="text-2xl font-bold text-emerald-600 dark:text-emerald-400 mt-1">
            {formatNumber(usage.output_tokens)}
          </div>
        </div>
        <div className="bg-card border border-border rounded-lg p-4">
          <div className="text-xs text-muted-foreground uppercase tracking-wide">
            Total Tokens
          </div>
          <div className="text-2xl font-bold text-purple-600 dark:text-purple-400 mt-1">
            {formatNumber(usage.total_tokens)}
          </div>
        </div>
        <div className="bg-card border border-border rounded-lg p-4">
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground uppercase tracking-wide">
            Est. Cost
            <span className="relative group inline-flex">
              <LuInfo className="w-3.5 h-3.5 cursor-help text-muted-foreground hover:text-foreground transition-colors" />
              <span className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 rounded-lg bg-popover border border-border text-popover-foreground text-xs font-normal normal-case tracking-normal shadow-lg px-3 py-2 opacity-0 group-hover:opacity-100 transition-opacity duration-150 z-50 leading-relaxed">
                Cost is estimated based on the model's published per-token pricing. Actual billing may differ.
                <span className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-border" />
              </span>
            </span>
          </div>
          <div className="text-2xl font-bold text-amber-600 dark:text-amber-400 mt-1">
            {formatCost(usage.cost)}
          </div>
          <div className="text-[10px] text-muted-foreground mt-0.5">estimated</div>
        </div>
      </div>

      {/* Per-node breakdown */}
      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-sm font-semibold">Usage by Node</h3>
        </div>
        {nodesWithUsage.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground text-center">
            No LLM usage recorded for this trace.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-xs text-muted-foreground uppercase">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Node</th>
                  <th className="px-4 py-2 text-right font-medium">Input</th>
                  <th className="px-4 py-2 text-right font-medium">Output</th>
                  <th className="px-4 py-2 text-right font-medium">Total</th>
                  <th className="px-4 py-2 text-right font-medium">
                    <span className="inline-flex items-center justify-end gap-1.5">
                      Est. Cost
                      <span className="relative group inline-flex">
                        <LuInfo className="w-3.5 h-3.5 cursor-help text-muted-foreground hover:text-foreground transition-colors" />
                        <span className="pointer-events-none absolute bottom-full right-0 mb-2 w-56 rounded-lg bg-popover border border-border text-popover-foreground text-xs font-normal normal-case tracking-normal shadow-lg px-3 py-2 opacity-0 group-hover:opacity-100 transition-opacity duration-150 z-50 leading-relaxed">
                          Cost is estimated based on the model's published per-token pricing. Actual billing may differ.
                        </span>
                      </span>
                    </span>
                  </th>
                  <th className="px-4 py-2 text-left font-medium">LLM Calls</th>
                </tr>
              </thead>
              <tbody>
                {nodesWithUsage.map((tc) => (
                  <tr key={tc.id} className="border-t border-border">
                    <td className="px-4 py-2 font-medium">{tc.name}</td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {formatNumber(tc.usage!.input_tokens)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {formatNumber(tc.usage!.output_tokens)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums font-semibold">
                      {formatNumber(tc.usage!.total_tokens)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {formatCost(tc.usage!.cost)}
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {tc.usage!.calls && tc.usage!.calls.length > 0
                        ? formatCallerNames(tc.usage!.calls)
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot className="bg-muted/40 font-semibold">
                <tr className="border-t border-border">
                  <td className="px-4 py-2">Total</td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatNumber(usage.input_tokens)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatNumber(usage.output_tokens)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatNumber(usage.total_tokens)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    <span title="Estimated — calculated by LangChain based on model pricing">
                      {formatCost(usage.cost)}
                    </span>
                  </td>
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

// ─── Main Page ────────────────────────────────────────────────────────────────

interface TraceLogsProps {
  // When provided, the component renders inside a Dialog and uses these
  // props instead of route params / location state. Closing the dialog
  // calls onClose. When omitted, the component renders as a full page
  // route (the original ``/trace/:messageId`` behaviour, kept for
  // direct-link backward compat).
  messageIdProp?: string;
  onClose?: () => void;
}

const TraceLogs: FC<TraceLogsProps> = ({ messageIdProp, onClose }) => {
  const location = useLocation();
  const navigate = useNavigate();
  const params = useParams<{ messageId: string }>();
  const messageId = messageIdProp || params.messageId;
  const isDialog = !!onClose;

  const stateMessage = isDialog ? null : location.state?.message;
  const stateUserQuery = isDialog ? null : location.state?.userQuery;

  const [apiData, setApiData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  // Always fetch the trace JSON — the backend writes it on every response,
  // so the API is the canonical source. Falls back to the navigation-state
  // message only if the API call fails (e.g. trace file got wiped by
  // container recreation). Page-mode messages restored from chat history
  // don't carry the full ``query_sources.agent_steps`` payload that the
  // trace view needs, so we can't trust ``stateMessage`` alone.
  useEffect(() => {
    if (!messageId) {
      setLoading(false);
      return;
    }
    const creds = sessionStorage.getItem("creds");
    // Skip the API call when there are no creds — sending ``Basic null``
    // makes FastAPI's HTTPBasic challenge with ``WWW-Authenticate: Basic``
    // which triggers the browser's native auth popup. Better to show
    // "no data" and let the user log back in via the normal flow.
    if (!creds) {
      setLoading(false);
      setApiData(null);
      return;
    }
    setLoading(true);
    fetch(`/ui/trace/${messageId}`, {
      headers: { Authorization: `Basic ${creds}` },
    })
      .then((res) => {
        if (!res.ok) throw new Error("Not found");
        return res.json();
      })
      .then((data) => setApiData(data))
      .catch(() => setApiData(null))
      .finally(() => setLoading(false));
  }, [messageId]);

  const message = apiData ? {
    content: apiData.natural_language_response,
    response_time: apiData.response_time,
    response_type: apiData.response_type,
    query_sources: apiData.query_sources,
  } : stateMessage;
  const userQuery = apiData?.user_query || stateUserQuery;

  const trace = useMemo(
    () => (message ? buildTraceFromMessage(message, userQuery) : null),
    [message, userQuery]
  );

  const handleBack = () => {
    if (onClose) {
      onClose();
      return;
    }
    if (window.history.length > 1) {
      navigate(-1);
    } else {
      navigate("/chat");
    }
  };

  const handleDownload = () => {
    if (!trace) return;
    const blob = new Blob([JSON.stringify(trace, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trace_${trace.sessionId}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const wrap = (inner: JSX.Element) => {
    if (isDialog) {
      return (
        <Dialog open onOpenChange={(o) => !o && onClose && onClose()}>
          <DialogContent
            className="w-fit min-w-[400px] max-w-[min(95vw,768px)] max-h-[90vh] overflow-y-auto bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D] p-0"
            onInteractOutside={(e) => e.preventDefault()}
            onOpenAutoFocus={(e) => e.preventDefault()}
          >
            {inner}
          </DialogContent>
        </Dialog>
      );
    }
    return <div className="min-h-screen bg-background">{inner}</div>;
  };

  if (loading) {
    return wrap(
      <div className="flex items-center justify-center py-16">
        <p className="text-muted-foreground">Loading trace data...</p>
      </div>
    );
  }

  if (!trace) {
    return wrap(
      <div className="flex items-center justify-center py-16">
        <p className="text-muted-foreground">Trace data not found.</p>
      </div>
    );
  }

  return wrap(
    <>
      {/* Header — only the title in dialog mode (Download moves to the
          footer alongside Close), so nothing constrains dialog width.
          Page mode keeps the original sticky flex bar with Back +
          Download. DialogTitle / DialogDescription wire up the
          accessibility labels Radix expects on a Dialog. */}
      {isDialog ? (
        <>
          <DialogTitle className="absolute left-6 top-4 text-xl font-semibold">Trace Logs</DialogTitle>
          <DialogDescription className="sr-only">
            Execution trace, citations, and token usage for the selected chat response.
          </DialogDescription>
        </>
      ) : (
        <div className="sticky top-0 z-10 bg-background border-b border-border">
          <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
            <div>
              <button
                onClick={handleBack}
                className="flex items-center gap-1 text-sm text-blue-600 dark:text-blue-400 hover:underline mb-1"
              >
                <LuArrowLeft className="w-4 h-4" />
                Close &amp; Back to Chat
              </button>
              <h1 className="text-xl font-semibold">Trace Logs</h1>
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={handleDownload}
                className="flex items-center gap-1.5 bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
              >
                <LuDownload className="w-4 h-4" />
                Download
              </button>
            </div>
          </div>
        </div>
      )}

      <div className={isDialog ? "px-6 pt-16 pb-6 space-y-6" : "max-w-5xl mx-auto px-6 py-6 space-y-6"}>
        {/* Original Query */}
        <div className="bg-card border border-border rounded-lg p-5">
          <h2 className="text-sm font-semibold mb-2">Original Query</h2>
          <div className="bg-muted rounded-md px-4 py-3 text-sm">
            {trace.originalQuery}
          </div>
        </div>

        {/* Conversation Context */}
        <div className="bg-card border border-border rounded-lg p-5">
          <h2 className="text-sm font-semibold mb-2">Conversation Context</h2>
          <div className="space-y-1">
            {trace.conversationContext.map((line, i) => (
              <p key={i} className="text-sm text-muted-foreground">
                {line}
              </p>
            ))}
          </div>
        </div>

        {/* Timing Overview */}
        <div className="bg-card border border-border rounded-lg p-5">
          <h2 className="text-sm font-semibold mb-4">Timing Overview</h2>
          <TimingRow
            items={[
              {
                value: formatDuration(trace.timing.totalDuration),
                label: "Total Duration",
                color: "text-blue-600 dark:text-blue-400",
              },
              {
                value: formatDuration(trace.timing.toolExecution),
                label: "Tool Execution",
                color: "text-emerald-600 dark:text-emerald-400",
              },
              {
                value: formatDuration(trace.timing.llmThinking),
                label: "LLM Thinking",
                color: "text-red-500 dark:text-red-400",
              },
            ]}
          />
          {/* Timeline bar */}
          <div className="relative">
            <div className="flex items-center gap-2 mb-1">
              <div className="w-3 h-3 rounded-full bg-emerald-500 shrink-0" />
              <div className="flex-1 h-3 rounded-full bg-gradient-to-r from-blue-500 via-purple-500 to-purple-600" />
            </div>
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>Start</span>
              <span>{trace.timing.startTime}</span>
              <span>{trace.timing.endTime}</span>
            </div>
          </div>
        </div>

        {/* Tabs */}
        <Tabs defaultValue="citations" className="w-full">
          <TabsList className="w-full justify-start bg-transparent border-b border-border rounded-none h-auto p-0 gap-0">
            <TabsTrigger
              value="citations"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none px-4 py-2.5"
            >
              Citations
              <span className="ml-1.5 bg-muted text-muted-foreground text-xs px-1.5 py-0.5 rounded-full">
                {trace.citations.length}
              </span>
            </TabsTrigger>
            <TabsTrigger
              value="logs"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none px-4 py-2.5"
            >
              <LuActivity className="w-4 h-4 mr-1.5" />
              Logs
            </TabsTrigger>
            <TabsTrigger
              value="toolcalls"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none px-4 py-2.5"
            >
              Tool Calls
              <span className="ml-1.5 bg-muted text-muted-foreground text-xs px-1.5 py-0.5 rounded-full">
                {trace.toolCalls.length}
              </span>
            </TabsTrigger>
            <TabsTrigger
              value="timeline"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none px-4 py-2.5"
            >
              Timeline
            </TabsTrigger>
            <TabsTrigger
              value="tokens"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none px-4 py-2.5"
            >
              <LuCoins className="w-4 h-4 mr-1.5" />
              Token Overview
              {trace.tokenUsage.total_tokens > 0 && (
                <span className="ml-1.5 bg-muted text-muted-foreground text-xs px-1.5 py-0.5 rounded-full">
                  {formatNumber(trace.tokenUsage.total_tokens)}
                </span>
              )}
            </TabsTrigger>
          </TabsList>

          <TabsContent value="citations" className="pt-4">
            <CitationsPanel trace={trace} />
          </TabsContent>
          <TabsContent value="logs" className="pt-4">
            <LogsPanel trace={trace} />
          </TabsContent>
          <TabsContent value="toolcalls" className="pt-4">
            <ToolCallsPanel trace={trace} />
          </TabsContent>
          <TabsContent value="timeline" className="pt-4">
            <TimelinePanel trace={trace} />
          </TabsContent>
          <TabsContent value="tokens" className="pt-4">
            <TokenOverviewPanel trace={trace} />
          </TabsContent>
        </Tabs>

        {/* Footer — Download (primary action style) + Close (outline,
            matches other dialogs). Replaces the top-right Download in
            dialog mode so the dialog can size to content. */}
        <div className="flex justify-end gap-2 pt-2">
          {isDialog && (
            <Button onClick={handleDownload} className="gradient text-white">
              <LuDownload className="w-4 h-4 mr-1.5" />
              Download
            </Button>
          )}
          <Button
            variant="outline"
            onClick={handleBack}
            className="dark:border-[#3D3D3D]"
          >
            Close
          </Button>
        </div>
      </div>
    </>
  );
};

export default TraceLogs;
