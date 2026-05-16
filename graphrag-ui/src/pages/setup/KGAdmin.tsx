import React, { useState, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { TagInput, TypeHint } from "@/components/ui/tag-input";
import { Database, Loader2, RefreshCw, Upload } from "lucide-react";
import { pauseIdleTimer, resumeIdleTimer, pingIdleTimer } from "@/hooks/useIdleTimeout";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
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
import { useConfirm } from "@/hooks/useConfirm";
import { useAlert } from "@/hooks/useAlert";
import { useNavigate } from "react-router-dom";
import IngestGraph from "./IngestGraph";

const KGAdmin = () => {
  const [confirm, confirmDialog, isConfirmDialogOpen] = useConfirm();
  const [showAlert, alertDialog] = useAlert();
  const navigate = useNavigate();
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);
  
  // Dialog states
  const [initializeDialogOpen, setInitializeDialogOpen] = useState(false);
  const [refreshDialogOpen, setRefreshDialogOpen] = useState(false);
  const [ingestDialogOpen, setIngestDialogOpen] = useState(false);
  // Reset states when dialogs close
  const handleInitializeDialogChange = (open: boolean) => {
    if (!open && isConfirmDialogOpen) {
      return;
    }
    // Closing the dialog (X, Esc, click-outside-prevented, or the
    // Cancel button) intentionally PRESERVES state — schema source,
    // typed graph name, picked sample files, the in-flight extract
    // spinner, and any returned draft GSQL all stay so the user can
    // reopen and pick up where they left off. State is only reset
    // when the user clicks the success "Done" button below
    // (handleInitializeReset).
    setInitializeDialogOpen(open);
  };

  const handleInitializeReset = () => {
    setGraphName("");
    setStatusMessage("");
    setStatusType("");
    setSchemaSource("none");
    setPasteGsql("");
    setDraftProposal(null);
    setSampleFiles([]);
    setExtractedFingerprint(null);
    setAttributesCollapsed(false);
    setCollapsedVertices(new Set());
    setCollapsedEdges(new Set());
    setVertexHints([]);
    setEdgeHints([]);
    setRenderedSchemaPrompt("");
    setIsInitComplete(false);
    setPrecheckPassed(false);
    setPrecheckMessage("");
    setCollectedVertexDescs({});
    setCollectedEdgeDescs({});
  };

  const handleRefreshDialogChange = (open: boolean) => {
    if (!open && isConfirmDialogOpen) {
      return;
    }
    setRefreshDialogOpen(open);
    if (!open) {
      setRefreshMessage("");
      setPollingActive(false);
    }
  };

  // Initialize state
  const [graphName, setGraphName] = useState("");
  const [isInitializing, setIsInitializing] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [statusType, setStatusType] = useState<"success" | "error" | "">("");

  // Graph-name combobox: dropdown of existing graphs that the user can
  // filter by typing. The input is the single source of truth; clicking
  // a row replaces the typed text.
  const [graphNameDropdownOpen, setGraphNameDropdownOpen] = useState(false);
  const graphNameComboRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!graphNameDropdownOpen) return;
    const handleOutside = (e: MouseEvent) => {
      if (
        graphNameComboRef.current &&
        !graphNameComboRef.current.contains(e.target as Node)
      ) {
        setGraphNameDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleOutside);
    return () => document.removeEventListener("mousedown", handleOutside);
  }, [graphNameDropdownOpen]);

  // Precheck state for the "none" schema-source path. The user clicks
  // Precheck before Create & Init to either confirm the graph is new
  // OR review/edit descriptions for pre-existing user-defined types.
  const [precheckPassed, setPrecheckPassed] = useState(false);
  const [precheckRunning, setPrecheckRunning] = useState(false);
  const [precheckMessage, setPrecheckMessage] = useState("");
  // Descriptions collected from the description-edit dialog. When non-empty,
  // the Create & Init submission carries use_existing_schema=true and these
  // maps so the backend stamps them onto EntityType / RelationshipType.
  const [collectedVertexDescs, setCollectedVertexDescs] = useState<Record<string, string>>({});
  const [collectedEdgeDescs, setCollectedEdgeDescs] = useState<Record<string, string>>({});
  // Description-edit dialog state
  const [descDialogOpen, setDescDialogOpen] = useState(false);
  const [descDialogVertices, setDescDialogVertices] = useState<string[]>([]);
  const [descDialogEdges, setDescDialogEdges] = useState<Array<{ name: string; from: string; to: string }>>([]);
  const [descDialogVertexDescs, setDescDialogVertexDescs] = useState<Record<string, string>>({});
  const [descDialogEdgeDescs, setDescDialogEdgeDescs] = useState<Record<string, string>>({});
  const [descDialogLoading, setDescDialogLoading] = useState(false);
  // True only after the full create-graph + initialize-graph round
  // succeeds. The "Done" button gates on this — extraction success
  // alone (statusType === "success" mid-flow) must NOT show Done,
  // because the user still needs to click Initialize.
  const [isInitComplete, setIsInitComplete] = useState(false);
  // Schema-source state (Phase 1). 'none' = legacy auto-create path;
  // 'gsql' = user pastes ADD VERTEX/EDGE statements (or `gsql ls`
  // output); 'samples' = user uploads a few representative documents,
  // the backend runs schema_extraction LLM, returns GSQL, and the
  // textarea is populated for review/edit before /initialize_graph.
  const [schemaSource, setSchemaSource] = useState<"none" | "gsql" | "samples">("none");
  // Two distinct buffers — Paste GSQL is the user's verbatim text for
  // the strict-syntax path; Generate-from-samples populates a
  // structured proposal (vertices / edges / attributes) the UI edits
  // in form mode.
  const [pasteGsql, setPasteGsql] = useState("");
  const [draftProposal, setDraftProposal] = useState<{
    vertices: Array<{
      name: string;
      description: string;
      attributes: Array<{ name: string; type: string }>;
    }>;
    edges: Array<{
      name: string;
      description: string;
      pairs: Array<[string, string]>;
      attributes: Array<{ name: string; type: string }>;
    }>;
    domain_label?: string;
  } | null>(null);
  const [sampleFiles, setSampleFiles] = useState<File[]>([]);
  const [maxSampleFiles, setMaxSampleFiles] = useState<number>(5);
  const [maxTotalMb, setMaxTotalMb] = useState<number>(50);
  const [isExtractingSchema, setIsExtractingSchema] = useState(false);
  // Optional structured guidance for the schema-extraction LLM.
  // Each chip is a ``{name, description}`` pair entered as
  // ``Name`` or ``Name: description`` in the TagInput. Backend
  // injects these as a "Suggested types" block in the resolved
  // prompt; on init success the rendered prompt is persisted as
  // the per-graph override so future re-extractions reuse it.
  const [vertexHints, setVertexHints] = useState<TypeHint[]>([]);
  const [edgeHints, setEdgeHints] = useState<TypeHint[]>([]);
  // Captures the rendered prompt returned by /extract_schema_from_jsonl
  // so the post-init save flow can write it as the per-graph override.
  const [renderedSchemaPrompt, setRenderedSchemaPrompt] = useState<string>("");
  // Lookup of names the user CAN'T pick for suggested types: GSQL
  // reserved words + GraphRAG structural type names. Keyed by
  // lowercased name → reason string the TagInput surfaces inline.
  // Same forbidden set feeds both Suggested Vertex Types and
  // Suggested Edge Types so e.g. ``Document`` is rejected as a
  // vertex name AND as an edge endpoint reference.
  const [forbiddenNames, setForbiddenNames] = useState<Record<string, string>>({});
  // Fingerprint of the file set used for the most recent successful
  // extraction. Used to disable the *Extract draft schema* button
  // when the same files are selected (no new work to do).
  const [extractedFingerprint, setExtractedFingerprint] = useState<string | null>(null);
  // True when the form-mode editor's per-card attribute lists are
  // hidden, for a cleaner overview of types.
  const [attributesCollapsed, setAttributesCollapsed] = useState<boolean>(false);
  // Per-card collapse state in the draft-schema review form. When a
  // type's index is in the set, only its name + description are shown
  // (everything else — attributes, edge endpoints — is hidden).
  // ``allCollapsed`` drives the toggle button label and lets us flip
  // every card at once.
  const [collapsedVertices, setCollapsedVertices] = useState<Set<number>>(new Set());
  const [collapsedEdges, setCollapsedEdges] = useState<Set<number>>(new Set());
  const allVerticesCollapsed =
    !!draftProposal &&
    draftProposal.vertices.length > 0 &&
    collapsedVertices.size === draftProposal.vertices.length;
  const allEdgesCollapsed =
    !!draftProposal &&
    draftProposal.edges.length > 0 &&
    collapsedEdges.size === draftProposal.edges.length;
  const toggleVertexCollapsed = (idx: number) =>
    setCollapsedVertices((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  const toggleEdgeCollapsed = (idx: number) =>
    setCollapsedEdges((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  const toggleAllVerticesCollapsed = () => {
    if (!draftProposal) return;
    if (allVerticesCollapsed) {
      setCollapsedVertices(new Set());
    } else {
      setCollapsedVertices(
        new Set(draftProposal.vertices.map((_, i) => i))
      );
    }
  };
  const toggleAllEdgesCollapsed = () => {
    if (!draftProposal) return;
    if (allEdgesCollapsed) {
      setCollapsedEdges(new Set());
    } else {
      setCollapsedEdges(new Set(draftProposal.edges.map((_, i) => i)));
    }
  };

  const fingerprintFiles = (files: File[]): string =>
    files
      .map((f) => `${f.name}:${f.size}:${f.lastModified}`)
      .sort()
      .join("|");

  // Composite fingerprint covering both the file set AND the hint
  // chips so changing either re-enables the Extract button.
  const sampleFingerprint =
    fingerprintFiles(sampleFiles) +
    "|hints:" +
    JSON.stringify({ v: vertexHints, e: edgeHints });

  const PRIMITIVE_TYPES = [
    "STRING",
    "INT",
    "UINT",
    "DOUBLE",
    "FLOAT",
    "BOOL",
    "DATETIME",
  ];

  // Render the form-mode draft proposal back into ADD VERTEX / ADD
  // DIRECTED EDGE GSQL for submission to /initialize_graph. Mirrors
  // schema_proposal.emit_preview_gsql on the backend so a round-trip
  // produces identical output.
  const draftProposalToGsql = (
    proposal: NonNullable<typeof draftProposal>
  ): string => {
    const lines: string[] = [];
    if (proposal.domain_label) {
      lines.push(`// Domain: ${proposal.domain_label}`);
      lines.push("");
    }
    for (const v of proposal.vertices) {
      if (!v.name.trim()) continue;
      if (v.description) lines.push(`// ${v.description}`);
      const attrs = v.attributes
        .filter((a) => a.name.trim())
        .map((a) => `${a.name} ${a.type}`)
        .join(", ");
      const attrPart = attrs ? `, ${attrs}` : "";
      lines.push(
        `ADD VERTEX ${v.name} (PRIMARY_ID id STRING${attrPart}) ` +
          `WITH PRIMARY_ID_AS_ATTRIBUTE="true";`
      );
      lines.push("");
    }
    for (const e of proposal.edges) {
      if (!e.name.trim() || e.pairs.length === 0) continue;
      if (e.description) lines.push(`// ${e.description}`);
      const pairs = e.pairs
        .filter(([f, t]) => f.trim() && t.trim())
        .map(([f, t]) => `FROM ${f}, TO ${t}`)
        .join(" | ");
      if (!pairs) continue;
      const attrs = e.attributes
        .filter((a) => a.name.trim())
        .map((a) => `${a.name} ${a.type}`)
        .join(", ");
      const attrPart = attrs ? `, ${attrs}` : "";
      lines.push(
        `ADD DIRECTED EDGE ${e.name} (${pairs}${attrPart}) ` +
          `WITH REVERSE_EDGE="reverse_${e.name}";`
      );
      lines.push("");
    }
    return lines.join("\n").trimEnd() + "\n";
  };

  // Refresh state
  // Seed from the shared ``selectedGraph`` so the dropdown
  // matches whatever was last picked elsewhere (Bot, IngestGraph,
  // etc.). Reacts to ``graphrag:selectedGraph`` events below.
  const [refreshGraphName, setRefreshGraphName] = useState(
    sessionStorage.getItem("selectedGraph") || ""
  );
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshMessage, setRefreshMessage] = useState("");
  const [isRebuildRunning, setIsRebuildRunning] = useState(false);
  const isRebuildRunningRef = useRef(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(false);
  const [pollingActive, setPollingActive] = useState(false);

  // Load available graphs. First seed from sessionStorage so the
  // dropdown shows something immediately, then refresh from
  // /ui/list_graphs so a graph created/initialized after login (or
  // during a session where the init request failed client-side but
  // succeeded server-side) is still visible without re-login.
  useEffect(() => {
    const store = JSON.parse(sessionStorage.getItem("site") || "{}");
    if (store.graphs && Array.isArray(store.graphs)) {
      setAvailableGraphs(store.graphs);
      if (store.graphs.length > 0 && !refreshGraphName) {
        setRefreshGraphName(store.graphs[0]);
      }
    }
    const creds = sessionStorage.getItem("creds");
    if (!creds) return;
    fetch("/ui/list_graphs", {
      headers: { Authorization: `Basic ${creds}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data || !Array.isArray(data.graphs)) return;
        const graphs: string[] = data.graphs;
        setAvailableGraphs(graphs);
        const cached = JSON.parse(sessionStorage.getItem("site") || "{}");
        cached.graphs = graphs;
        sessionStorage.setItem("site", JSON.stringify(cached));
        if (graphs.length > 0 && !refreshGraphName) {
          setRefreshGraphName(graphs[0]);
        }
      })
      .catch(() => {
        /* keep cached value; not fatal */
      });
  }, []);

  // Keep the Refresh-dialog graph picker in sync with the shared
  // ``selectedGraph`` so changing the graph elsewhere (Bot, the
  // Ingest dialog) immediately reflects here.
  useEffect(() => {
    const handler = () => {
      const next = sessionStorage.getItem("selectedGraph") || "";
      if (next && next !== refreshGraphName) setRefreshGraphName(next);
    };
    window.addEventListener("graphrag:selectedGraph", handler);
    return () => window.removeEventListener("graphrag:selectedGraph", handler);
  }, [refreshGraphName]);

  // Pull schema-init caps from /ui/config when the Initialize dialog opens.
  // Read-only here; the values are edited on the GraphRAG Config page.
  useEffect(() => {
    if (!initializeDialogOpen) return;
    // If there's pending sample-flow state (extraction in flight or a
    // returned draft), force the "Generate from sample documents"
    // radio to be selected so the user immediately sees the spinner /
    // form on reopen, instead of landing on the previously-selected
    // option.
    if (isExtractingSchema || draftProposal) {
      setSchemaSource("samples");
    }
    const creds = sessionStorage.getItem("creds");
    if (!creds) return;
    fetch(`/ui/config`, { headers: { Authorization: `Basic ${creds}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const cfg = data?.graphrag_config || {};
        if (typeof cfg.schema_max_sample_files === "number")
          setMaxSampleFiles(cfg.schema_max_sample_files);
        if (typeof cfg.schema_max_total_mb === "number")
          setMaxTotalMb(cfg.schema_max_total_mb);
      })
      .catch(() => {
        /* fall back to defaults */
      });
    // Pull the list of names the user can't pick for suggested types.
    // Empty / failed response leaves ``forbiddenNames`` as ``{}`` so
    // the TagInput falls back to format-only validation — the
    // downstream parser would still drop reserved/structural names,
    // just without the inline message.
    fetch(`/ui/schema_reserved_names`, {
      headers: { Authorization: `Basic ${creds}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        const map: Record<string, string> = {};
        for (const w of data.gsql_keywords || []) {
          map[String(w).toLowerCase()] = "GSQL reserved word";
        }
        for (const t of data.structural_vertex_types || []) {
          map[String(t).toLowerCase()] = "reserved structural vertex type";
        }
        for (const t of data.structural_edge_types || []) {
          map[String(t).toLowerCase()] = "reserved structural edge type";
        }
        setForbiddenNames(map);
      })
      .catch(() => {
        /* keep current value; not fatal */
      });
  }, [initializeDialogOpen]);

  // Any change to the graph name or schema source invalidates a prior
  // precheck — the next Create & Init must re-run the eligibility flow.
  useEffect(() => {
    setPrecheckPassed(false);
    setPrecheckMessage("");
    setCollectedVertexDescs({});
    setCollectedEdgeDescs({});
  }, [graphName, schemaSource]);

  const handleSampleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const list = Array.from(e.target.files || []);
    if (list.length > maxSampleFiles) {
      setStatusMessage(`Too many files: pick at most ${maxSampleFiles}.`);
      setStatusType("error");
      e.target.value = "";
      return;
    }
    const totalBytes = list.reduce((sum, f) => sum + f.size, 0);
    if (totalBytes > maxTotalMb * 1024 * 1024) {
      setStatusMessage(`Total size exceeds ${maxTotalMb} MB cap.`);
      setStatusType("error");
      e.target.value = "";
      return;
    }
    const oversize = list.find((f) => f.size > 10 * 1024 * 1024);
    if (oversize) {
      setStatusMessage(`File ${oversize.name} exceeds the 10 MB per-file cap.`);
      setStatusType("error");
      e.target.value = "";
      return;
    }
    setSampleFiles(list);
    setStatusMessage("");
    setStatusType("");
  };

  const handleExtractFromSamples = async () => {
    if (!graphName.trim()) {
      setStatusMessage("Enter a graph name before extracting a draft schema.");
      setStatusType("error");
      return;
    }
    if (sampleFiles.length === 0) {
      setStatusMessage("Pick at least one sample document first.");
      setStatusType("error");
      return;
    }
    setIsExtractingSchema(true);
    setStatusMessage(
      `Step 1/2: Converting ${sampleFiles.length} uploaded file${sampleFiles.length === 1 ? "" : "s"} to text…`
    );
    setStatusType("");
    // The LLM call can take minutes; pause the idle timer so the
    // user isn't logged out mid-extraction.
    pauseIdleTimer();
    try {
      const creds = sessionStorage.getItem("creds");
      if (!creds) throw new Error("Not authenticated. Please login first.");

      // Step 1/2: upload + convert. Returns the saved filenames so we
      // know exactly which JSONLs to feed to the LLM in step 2.
      const form = new FormData();
      sampleFiles.forEach((f) => form.append("files", f));
      const convertResp = await fetch(
        `/ui/${graphName}/convert_sample_files`,
        {
          method: "POST",
          headers: { Authorization: `Basic ${creds}` },
          body: form,
        }
      );
      const convertData = await convertResp.json();
      if (!convertResp.ok) {
        throw new Error(
          convertData.detail || `Conversion failed: ${convertResp.statusText}`
        );
      }

      // Step 2/2: LLM call. The status flip now reflects the real
      // backend phase change, not a timer.
      setStatusMessage("Step 2/2: Extracting schema with LLM…");
      const resp = await fetch(
        `/ui/${graphName}/extract_schema_from_jsonl`,
        {
          method: "POST",
          headers: {
            Authorization: `Basic ${creds}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            request_id: convertData.request_id || "",
            filenames: convertData.saved_files || [],
            vertex_hints: vertexHints,
            edge_hints: edgeHints,
          }),
        }
      );
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || `Extraction failed: ${resp.statusText}`);
      }
      // Stash the rendered prompt so the post-init save can write it
      // as the per-graph schema_extraction.txt override.
      if (typeof data.rendered_prompt === "string") {
        setRenderedSchemaPrompt(data.rendered_prompt);
      }
      const proposal = data.proposal;
      if (
        !proposal ||
        ((proposal.vertices?.length ?? 0) === 0 &&
          (proposal.edges?.length ?? 0) === 0)
      ) {
        throw new Error("LLM returned no schema. Try different sample files.");
      }
      // Normalize so every record has the optional fields the form
      // editor expects (defensive — backend always sets them today).
      setDraftProposal({
        domain_label: proposal.domain_label,
        vertices: (proposal.vertices || []).map((v: any) => ({
          name: v.name || "",
          description: v.description || "",
          attributes: (v.attributes || []).map((a: any) => ({
            name: a.name || "",
            type: a.type || "STRING",
          })),
        })),
        edges: (proposal.edges || []).map((e: any) => ({
          name: e.name || "",
          description: e.description || "",
          pairs: (e.pairs || []).map((p: any) => [
            p?.[0] || "",
            p?.[1] || "",
          ]) as Array<[string, string]>,
          attributes: (e.attributes || []).map((a: any) => ({
            name: a.name || "",
            type: a.type || "STRING",
          })),
        })),
      });
      // Capture the composite fingerprint (files + hint chips) so the
      // Extract button stays disabled until something changes.
      setExtractedFingerprint(sampleFingerprint);
      setStatusMessage(
        `Draft schema ready (${data.summary?.vertex_count ?? "?"} vertex types, ` +
          `${data.summary?.edge_count ?? "?"} edge types). Review/edit below, then click Initialize.`
      );
      setStatusType("success");
    } catch (error: any) {
      console.error("Schema extraction error:", error);
      setStatusMessage(`❌ ${error.message}`);
      setStatusType("error");
    } finally {
      resumeIdleTimer();
      setIsExtractingSchema(false);
    }
  };

  // Precheck for the "none" schema-source path.
  //   * Empty graph  → precheckPassed = true, Create & Init becomes clickable.
  //   * Structural   → alert "manual cleanup required", precheckPassed stays false.
  //   * User types   → LLM seeds descriptions, description-edit dialog opens.
  const handlePrecheck = async () => {
    if (!graphName.trim()) {
      setPrecheckMessage("Please enter a graph name first.");
      return;
    }
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(graphName)) {
      setPrecheckMessage("Invalid graph name — must start with a letter or underscore.");
      return;
    }
    setPrecheckRunning(true);
    setPrecheckMessage("");
    try {
      const creds = sessionStorage.getItem("creds");
      const eligResp = await fetch(`/ui/${graphName}/check_init_eligibility`, {
        headers: { Authorization: `Basic ${creds}` },
      });
      const elig = await eligResp.json();
      if (!eligResp.ok) {
        setPrecheckMessage(`Precheck failed: ${elig?.detail || eligResp.statusText}`);
        return;
      }
      if (elig.state === "structural_present") {
        await showAlert(
          "Existing GraphRAG schema detected, manual cleanup required."
        );
        setPrecheckPassed(false);
        setPrecheckMessage("Existing GraphRAG schema present — cannot initialize.");
        return;
      }
      if (elig.state === "empty") {
        setPrecheckPassed(true);
        setPrecheckMessage("Graph is empty or new — ready to initialize.");
        return;
      }
      // state === "user_types_present" — seed descriptions and open the
      // edit dialog. The Create & Init button stays disabled until the
      // user accepts.
      const vts: string[] = elig.user_vertex_types || [];
      const ets: string[] = elig.user_edge_types || [];
      const pairsMap: Record<string, string[][]> = elig.user_edge_pairs || {};
      const edges = ets.map((name) => {
        const pair = (pairsMap[name] || [])[0] || ["", ""];
        return { name, from: pair[0] || "", to: pair[1] || "" };
      });
      setDescDialogVertices(vts);
      setDescDialogEdges(edges);
      setDescDialogVertexDescs(Object.fromEntries(vts.map((v) => [v, ""])));
      setDescDialogEdgeDescs(Object.fromEntries(edges.map((e) => [e.name, ""])));
      setDescDialogOpen(true);
      setDescDialogLoading(true);
      // LLM call to seed each description; best-effort. The dialog stays
      // editable regardless.
      try {
        const sugResp = await fetch(
          `/ui/${graphName}/suggest_type_descriptions`,
          {
            method: "POST",
            headers: {
              Authorization: `Basic ${creds}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              vertex_types: vts,
              edge_types: edges,
            }),
          }
        );
        if (sugResp.ok) {
          const sug = await sugResp.json();
          setDescDialogVertexDescs((prev) => ({ ...prev, ...(sug.vertex_descriptions || {}) }));
          setDescDialogEdgeDescs((prev) => ({ ...prev, ...(sug.edge_descriptions || {}) }));
        }
      } catch {
        // Silent — leave descriptions blank for the user to fill in.
      } finally {
        setDescDialogLoading(false);
      }
    } catch (err: any) {
      setPrecheckMessage(`Precheck failed: ${err.message}`);
    } finally {
      setPrecheckRunning(false);
    }
  };

  const handleAcceptDescriptions = () => {
    setCollectedVertexDescs({ ...descDialogVertexDescs });
    setCollectedEdgeDescs({ ...descDialogEdgeDescs });
    setPrecheckPassed(true);
    setPrecheckMessage(
      `Adopting ${descDialogVertices.length} vertex and ${descDialogEdges.length} edge type${descDialogEdges.length === 1 ? "" : "s"} from existing schema.`
    );
    setDescDialogOpen(false);
  };

  // Initialize Graph
  const handleInitializeGraph = async () => {
    if (!graphName.trim()) {
      setStatusMessage("Please enter a graph name");
      setStatusType("error");
      return;
    }

    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(graphName)) {
      setStatusMessage("Invalid graph name. Must start with a letter or underscore, followed by letters, digits, or underscores.");
      setStatusType("error");
      return;
    }

    setIsInitializing(true);
    setStatusMessage("Creating graph and initializing GraphRAG schema...");
    setStatusType("");
    // Schema-change job + retriever installs can take minutes; pause
    // the idle timer so the user isn't logged out mid-init.
    pauseIdleTimer();

    try {
      const creds = sessionStorage.getItem("creds");
      if (!creds) {
        throw new Error("Not authenticated. Please login first.");
      }

      setStatusMessage("Step 1/2: Creating graph...");
      const createResponse = await fetch(`/ui/${graphName}/create_graph`, {
        method: "POST",
        headers: { Authorization: `Basic ${creds}` },
      });

      const createData = await createResponse.json();

      if (!createResponse.ok) {
        const detail = createData.detail;
        const msg = typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((d: any) => d.msg || JSON.stringify(d)).join("; ")
            : createData.message || `Failed to create graph: ${createResponse.statusText}`;
        throw new Error(msg);
      }

      if (createData.status !== "success") {
        if (createData.message && createData.message.includes("already exists")) {
          const shouldInitialize = await confirm(
            `Graph "${graphName}" already exists. Do you want to initialize it with GraphRAG schema?`
          );
          if (!shouldInitialize) {
            setStatusMessage("Operation cancelled by user.");
            setStatusType("error");
            setIsInitializing(false);
            return;
          }
        } else {
          throw new Error(
            createData.message || `Failed to create graph: ${createData.details}`
          );
        }
      }

      setStatusMessage("Step 2/2: Submitting GraphRAG schema initialization...");
      const initBody: Record<string, any> = {};
      const adoptingExisting =
        schemaSource === "none" &&
        (Object.keys(collectedVertexDescs).length > 0 ||
          Object.keys(collectedEdgeDescs).length > 0);
      if (schemaSource === "gsql" && pasteGsql.trim()) {
        initBody.schema_gsql = pasteGsql;
      } else if (schemaSource === "samples" && draftProposal) {
        const gsql = draftProposalToGsql(draftProposal).trim();
        if (gsql) initBody.schema_gsql = gsql;
      } else if (adoptingExisting) {
        initBody.use_existing_schema = true;
        initBody.vertex_descriptions = collectedVertexDescs;
        initBody.edge_descriptions = collectedEdgeDescs;
      }

      const initResponse = await fetch(`/ui/${graphName}/initialize_graph`, {
        method: "POST",
        headers: {
          Authorization: `Basic ${creds}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(initBody),
      });
      const initData = await initResponse.json();

      // Server-side preflight may still reject (e.g. graph state
      // changed between Precheck and Create & Init, or the caller
      // skipped Precheck on samples/gsql with pre-existing types).
      if (initResponse.status === 409 && initData?.detail?.reason) {
        const message: string = initData.detail.message;
        await showAlert(message);
        setStatusMessage(message);
        setStatusType("error");
        setIsInitializing(false);
        return;
      }

      if (!initResponse.ok) {
        const detail = initData?.detail;
        const msg = typeof detail === "string"
          ? detail
          : detail?.message
            ? detail.message
            : `Failed to submit init: ${initResponse.statusText}`;
        throw new Error(msg);
      }

      if (initData.status !== "submitted") {
        throw new Error(
          initData.message || `Init submission failed: ${JSON.stringify(initData)}`
        );
      }

      // Poll for completion. The bg task updates per-graph state on
      // the server; we read it every few seconds and surface progress.
      setStatusMessage("Step 2/2: Initializing GraphRAG schema (this can take several minutes)...");
      const pollIntervalMs = 5000;
      const maxWaitMs = 30 * 60 * 1000; // 30 minutes hard cap
      const start = Date.now();
      let finalState: any = null;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        if (Date.now() - start > maxWaitMs) {
          throw new Error(
            "Init still running after 30 minutes; check server logs."
          );
        }
        await new Promise((r) => setTimeout(r, pollIntervalMs));
        let statusResp: Response;
        try {
          statusResp = await fetch(
            `/ui/${graphName}/initialize_status`,
            { headers: { Authorization: `Basic ${creds}` } }
          );
        } catch {
          // Transient network blip — retry on the next tick rather
          // than aborting; the bg task is still working server-side.
          continue;
        }
        if (!statusResp.ok) continue;
        // Successful status poll on a user-initiated long flow — keep
        // the UI idle timer alive so the user isn't logged out while
        // watching the init progress.
        pingIdleTimer();
        const statusData = await statusResp.json();
        if (statusData.message) {
          setStatusMessage(`Step 2/2: ${statusData.message}`);
        }
        if (statusData.state === "completed") {
          finalState = statusData;
          break;
        }
        if (statusData.state === "error") {
          throw new Error(
            statusData.error || statusData.message || "Init failed"
          );
        }
      }

      const result = finalState?.result || {};
      const domain = result.domain_schema_status;
      let domainNote = "";
      if (domain && domain.status === "applied") {
        const stmts = domain.statements?.length ?? 0;
        domainNote = ` Domain schema applied (${stmts} statement${stmts === 1 ? "" : "s"}).`;
      } else if (domain && domain.status === "no-op") {
        domainNote = " Domain schema already up-to-date.";
      }
      setStatusMessage(
        `✅ Graph "${graphName}" created and initialized successfully!${domainNote} You can now close this dialog.`
      );
      setStatusType("success");
      setIsInitComplete(true);

      // If the user supplied any structured hints, persist the
      // rendered prompt (default + suggested-types block) as the
      // per-graph schema_extraction.txt override so future
      // re-extractions on this graph reuse the same hints. Failure
      // here is non-fatal — the init itself already succeeded.
      const hintCount = vertexHints.length + edgeHints.length;
      if (hintCount > 0 && renderedSchemaPrompt) {
        try {
          await fetch("/ui/prompts", {
            method: "POST",
            headers: {
              Authorization: `Basic ${creds}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              graphname: graphName,
              prompt_type: "schema_extraction",
              editable_content: renderedSchemaPrompt,
            }),
          });
        } catch (e) {
          console.warn("Saving per-graph schema prompt failed (non-fatal):", e);
        }
      }

      const newGraph = graphName;
      setAvailableGraphs(prev => {
        if (!prev.includes(newGraph)) {
          const updated = [...prev, newGraph];
          const store = JSON.parse(sessionStorage.getItem("site") || "{}");
          store.graphs = updated;
          sessionStorage.setItem("site", JSON.stringify(store));
          return updated;
        }
        return prev;
      });

      // Make the just-initialized graph the default selection across
      // the app (chat picker, ingest dialog, customize prompts, etc.).
      // Bot.tsx listens to ``graphrag:selectedGraph`` events to refresh
      // its dropdown; other pages re-read sessionStorage on mount.
      sessionStorage.setItem("selectedGraph", newGraph);
      window.dispatchEvent(new Event("graphrag:selectedGraph"));

      setRefreshGraphName(graphName);
      setGraphName("");
    } catch (error: any) {
      console.error("Error creating graph:", error);
      setStatusMessage(`❌ Error: ${error.message}`);
      setStatusType("error");
    } finally {
      resumeIdleTimer();
      setIsInitializing(false);
    }
  };

  // Check rebuild status
  const checkRebuildStatus = async (
    graphName: string,
    showLoadingMessage: boolean = false
  ) => {
    if (!graphName) return;

    setIsCheckingStatus(true);
    if (showLoadingMessage) {
      setRefreshMessage("Checking rebuild status...");
    }

    try {
      const creds = sessionStorage.getItem("creds");
      const statusResponse = await fetch(`/ui/${graphName}/rebuild_status`, {
        method: "GET",
        headers: { Authorization: `Basic ${creds}` },
      });

      if (statusResponse.ok) {
        const statusData = await statusResponse.json();
        const wasRunning = isRebuildRunningRef.current;
        const isCurrentlyRunning = statusData.is_running || false;

        setIsRebuildRunning(isCurrentlyRunning);
        isRebuildRunningRef.current = isCurrentlyRunning;

        if (isCurrentlyRunning) {
          // Long-running flow with active user interest — keep the
          // UI idle timer alive on each successful poll.
          pingIdleTimer();
          setPollingActive(true);
          const startTime = statusData.started_at
            ? new Date(statusData.started_at * 1000).toLocaleString()
            : "unknown time";
          const stage = statusData.stage ? ` — ${statusData.stage}` : "";
          setRefreshMessage(
            `⚠️ A rebuild is already in progress for "${graphName}" (started at ${startTime})${stage}. Please wait for it to complete.`
          );
        } else if (wasRunning && statusData.status === "completed") {
          setRefreshMessage(`✅ Rebuild completed successfully for "${graphName}".`);
          setPollingActive(false);
        } else if (statusData.status === "failed") {
          setRefreshMessage(`❌ Previous rebuild failed: ${statusData.error || "Unknown error"}`);
          setPollingActive(false);
        } else if (statusData.status === "error") {
          setRefreshMessage(`❌ Failed to check rebuild status: ${statusData.error || "Unknown error"}`);
          setPollingActive(false);
        } else if (statusData.status === "unknown") {
          setRefreshMessage(`⚠️ ECC service returned unknown status. It may be unavailable.`);
          setPollingActive(false);
        } else {
          setRefreshMessage("");
        }
      } else {
        setRefreshMessage(`❌ Failed to check rebuild status (HTTP ${statusResponse.status}).`);
      }
    } catch (error: any) {
      console.error("Error checking rebuild status:", error);
      if (showLoadingMessage) {
        setRefreshMessage(`❌ Unable to reach ECC service: ${error.message || "Connection failed"}`);
      }
    } finally {
      setIsCheckingStatus(false);
    }
  };

  // Rebuild Graph
  const handleRefreshGraph = async () => {
    if (!refreshGraphName) {
      setRefreshMessage("Please select a graph");
      return;
    }

    if (isRebuildRunning) {
      setRefreshMessage(
        `⚠️ A rebuild is already in progress. Please wait for it to complete.`
      );
      return;
    }

    setIsRefreshing(true);

    const shouldRefresh = await confirm(
      `Are you sure you want to rebuild the knowledge graph "${refreshGraphName}"? This will rerun entity extraction and community detection.`
    );
    if (!shouldRefresh) {
      setRefreshMessage("Operation cancelled by user.");
      setIsRefreshing(false);
      return;
    }

    setRefreshMessage("Verifying rebuild status...");

    try {
      const creds = sessionStorage.getItem("creds");

      // Final status check to prevent race conditions
      const statusCheckResponse = await fetch(`/ui/${refreshGraphName}/rebuild_status`, {
        method: "GET",
        headers: { Authorization: `Basic ${creds}` },
      });

      if (statusCheckResponse.ok) {
        const statusData = await statusCheckResponse.json();
        if (statusData.is_running) {
          setRefreshMessage(`⚠️ A rebuild is already in progress for "${refreshGraphName}". Please wait for it to complete.`);
          setIsRebuildRunning(true);
          isRebuildRunningRef.current = true;
          setIsRefreshing(false);
          return;
        }
      }

      setRefreshMessage("Submitting rebuild request...");

      const response = await fetch(`/ui/${refreshGraphName}/rebuild_graph`, {
        method: "POST",
        headers: { Authorization: `Basic ${creds}` },
      });

      if (!response.ok) {
        const errorData = await response.json();
        if (response.status === 409) {
          setRefreshMessage(`⚠️ ${errorData.detail || errorData.message}`);
          setIsRefreshing(false);
          return;
        }
        throw new Error(
          errorData.detail || `Failed to rebuild graph: ${response.statusText}`
        );
      }

      const data = await response.json();
      console.log("Refresh response:", data);

      setRefreshMessage(
        `✅ Rebuild submitted successfully! The knowledge graph "${refreshGraphName}" is being rebuilt.`
      );
      setIsRebuildRunning(true);
      isRebuildRunningRef.current = true;
      setPollingActive(true);
    } catch (error: any) {
      console.error("Error refreshing graph:", error);
      setRefreshMessage(`❌ Error: ${error.message}`);
    } finally {
      setIsRefreshing(false);
    }
  };

  // Initial status check when dialog opens
  useEffect(() => {
    if (refreshDialogOpen && refreshGraphName) {
      checkRebuildStatus(refreshGraphName, true);
    }
  }, [refreshDialogOpen, refreshGraphName]);

  // Poll status only while a rebuild is actively running
  useEffect(() => {
    if (!pollingActive || !refreshDialogOpen || !refreshGraphName) return;

    pauseIdleTimer();
    const intervalId = setInterval(() => {
      checkRebuildStatus(refreshGraphName, false);
    }, 5000);

    return () => {
      clearInterval(intervalId);
      resumeIdleTimer();
    };
  }, [pollingActive, refreshDialogOpen, refreshGraphName]);

  return (
    <div className="p-8">
      <div className="max-w-7xl mx-auto">
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-black dark:text-white mb-2">
            Knowledge Graph Setup
          </h1>
          <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
            Configure and manage your knowledge graphs
          </p>
        </div>

        {/* Card Grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {/* Initialize Card */}
          <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
            <div className="mb-4">
              <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
                <Database className="h-6 w-6 text-tigerOrange" />
              </div>
              <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">
                Initialize Knowledge Graph
              </h2>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
                Create the knowledge graph schema and queries for future document ingestion.
              </p>
            </div>
            <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
              <Button
                onClick={() => setInitializeDialogOpen(true)}
                className="gradient w-full text-white"
              >
                <Database className="h-4 w-4 mr-2" />
                Initialize Graph
              </Button>
            </div>
          </div>

          {/* Ingest Card */}
          <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
            <div className="mb-4">
              <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
                <Upload className="h-6 w-6 text-tigerOrange" />
              </div>
              <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">
                Ingest to Knowledge Graph
              </h2>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
                Upload and ingest documents into your knowledge graph for future content processing.
              </p>
            </div>
            <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
              <Button
                onClick={() => setIngestDialogOpen(true)}
                className="gradient w-full text-white"
              >
                <Upload className="h-4 w-4 mr-2" />
                Ingest Document
              </Button>
            </div>
          </div>

          {/* Rebuild Card */}
          <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
            <div className="mb-4">
              <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
                <RefreshCw className="h-6 w-6 text-tigerOrange" />
              </div>
              <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">
                Rebuild Knowledge Graph
              </h2>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
                Process documents and rerun entity extraction + community detection.
              </p>
            </div>
            <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
              <Button
                onClick={() => setRefreshDialogOpen(true)}
                className="gradient w-full text-white"
              >
                <RefreshCw className="h-4 w-4 mr-2" />
                Rebuild Graph
              </Button>
            </div>
          </div>

        </div>

        {/* Initialize Dialog */}
        <Dialog open={initializeDialogOpen} onOpenChange={handleInitializeDialogChange}>
          <DialogContent
            className="sm:max-w-[760px] max-h-[90vh] overflow-y-auto bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]"
            onInteractOutside={(e) => e.preventDefault()}
          >
            <DialogHeader>
              <DialogTitle className="text-black dark:text-white">Initialize Knowledge Graph</DialogTitle>
              <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
                Enter the name of your knowledge graph. The system will create it if necessary and initialize it with the GraphRAG schema.
              </DialogDescription>
            </DialogHeader>

            <div className="py-4">
              <div className="mb-4">
                <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                  Knowledge Graph Name
                </label>
                <div ref={graphNameComboRef} className="relative">
                  {/* Wrapper carries the visual styling (matching the
                      SelectTrigger used by other graph selectors); the
                      inner <input> is borderless/transparent so its
                      native text rendering can't clip the underscore
                      glyph against the bottom border. */}
                  <div
                    className={
                      "flex h-11 w-full items-center rounded-md border border-input " +
                      "bg-background dark:border-[#3D3D3D] dark:bg-shadeA " +
                      "px-3 text-sm focus-within:ring-2 focus-within:ring-ring " +
                      "focus-within:ring-offset-2 ring-offset-background"
                    }
                  >
                    <input
                      type="text"
                      placeholder="Type a new name or pick an existing graph"
                      value={graphName}
                      onChange={(e) => {
                        setGraphName(e.target.value);
                        if (!graphNameDropdownOpen) setGraphNameDropdownOpen(true);
                      }}
                      onFocus={() => setGraphNameDropdownOpen(true)}
                      disabled={isInitializing || isExtractingSchema}
                      className="flex-1 bg-transparent outline-none border-0 p-0 text-sm text-black dark:text-white placeholder:text-muted-foreground disabled:opacity-50"
                      // appearance:none disables Chrome's native input
                      // rendering (which on macOS clips descenders like
                      // '_' even when the wrapper has plenty of room).
                      // lineHeight + a slightly taller wrapper finish
                      // the job of making the underscore glyph fully
                      // visible in long names.
                      style={{
                        WebkitAppearance: "none",
                        appearance: "none",
                        lineHeight: "1.5",
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !isInitializing && !isExtractingSchema) {
                          handleInitializeGraph();
                        } else if (e.key === "Escape") {
                          setGraphNameDropdownOpen(false);
                        }
                      }}
                    />
                    <button
                      type="button"
                      onClick={() => setGraphNameDropdownOpen((o) => !o)}
                      disabled={
                        isInitializing ||
                        isExtractingSchema ||
                        availableGraphs.length === 0
                      }
                      aria-label="Toggle existing graphs"
                      className="ml-2 p-0.5 text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200 disabled:opacity-50"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                      </svg>
                    </button>
                  </div>
                  {graphNameDropdownOpen && (() => {
                    const q = graphName.trim().toLowerCase();
                    const filtered = q
                      ? availableGraphs.filter((g) => g.toLowerCase().includes(q))
                      : availableGraphs;
                    if (filtered.length === 0) return null;
                    return (
                      <div className="absolute top-full left-0 right-0 mt-1 max-h-60 overflow-y-auto rounded-md border border-gray-300 dark:border-[#3D3D3D] bg-white dark:bg-shadeA shadow-md z-50">
                        {filtered.map((g) => (
                          <button
                            key={g}
                            type="button"
                            onClick={() => {
                              setGraphName(g);
                              setGraphNameDropdownOpen(false);
                            }}
                            className="w-full text-left px-3 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-800 text-black dark:text-white"
                          >
                            {g}
                          </button>
                        ))}
                      </div>
                    );
                  })()}
                </div>
              </div>

              <div className="mb-4">
                <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                  Domain schema (optional)
                </label>
                <div className="flex flex-col gap-1 mb-2 text-sm text-gray-700 dark:text-[#D9D9D9]">
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="radio"
                      name="schemaSource"
                      checked={schemaSource === "none"}
                      onChange={() => setSchemaSource("none")}
                      // Only disable when init or extraction is in
                      // flight AND this is NOT the currently-selected
                      // option — that way the active radio keeps its
                      // full "selected" styling so the user can clearly
                      // see which source is running.
                      disabled={
                        (isInitializing || isExtractingSchema) &&
                        schemaSource !== "none"
                      }
                    />
                    <span>None — only create the GraphRAG structural schema</span>
                  </label>
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="radio"
                      name="schemaSource"
                      checked={schemaSource === "samples"}
                      onChange={() => setSchemaSource("samples")}
                      disabled={
                        (isInitializing || isExtractingSchema) &&
                        schemaSource !== "samples"
                      }
                    />
                    <span>Generate from sample documents</span>
                  </label>
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="radio"
                      name="schemaSource"
                      checked={schemaSource === "gsql"}
                      onChange={() => setSchemaSource("gsql")}
                      disabled={
                        (isInitializing || isExtractingSchema) &&
                        schemaSource !== "gsql"
                      }
                    />
                    <span>Paste GSQL schema</span>
                  </label>
                </div>

                {schemaSource === "none" && (
                  <div className="space-y-2 mt-3">
                    {precheckMessage ? (
                      <p
                        className={`text-xs ${
                          precheckPassed
                            ? "text-green-600 dark:text-green-400"
                            : "text-amber-600 dark:text-amber-400"
                        }`}
                      >
                        {precheckMessage}
                      </p>
                    ) : (
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        Click <strong>Check existing schema</strong> to verify the graph before initializing.
                      </p>
                    )}
                    <Button
                      onClick={handlePrecheck}
                      disabled={
                        isInitializing ||
                        isExtractingSchema ||
                        precheckRunning ||
                        !graphName.trim()
                      }
                      className="gradient text-white"
                    >
                      {precheckRunning ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Checking…
                        </>
                      ) : (
                        <>Check existing schema</>
                      )}
                    </Button>
                  </div>
                )}

                {schemaSource === "samples" && (
                  <div className="space-y-3">
                    <input
                      type="file"
                      multiple
                      accept=".pdf,.docx,.html,.htm,.md,.txt,.json,.xml,.csv"
                      onChange={handleSampleFileSelect}
                      disabled={isInitializing || isExtractingSchema}
                      className="block w-full text-xs text-gray-700 dark:text-[#D9D9D9]"
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      Up to {maxSampleFiles} files, ≤10 MB each, ≤{maxTotalMb} MB total.
                      Selected: {sampleFiles.length}
                      {sampleFiles.length > 0 &&
                        ` (${(sampleFiles.reduce((s, f) => s + f.size, 0) / (1024 * 1024)).toFixed(1)} MB)`}
                    </p>
                    <div className="space-y-2">
                      <p className="text-xs text-gray-600 dark:text-gray-300">
                        Suggested types (optional). Vertex format:{" "}
                        <code className="px-1 py-0.5 rounded bg-gray-100 dark:bg-shadeA font-semibold">Name</code>{" "}
                        or{" "}
                        <code className="px-1 py-0.5 rounded bg-gray-100 dark:bg-shadeA font-semibold">Name: description</code>.
                        Edge format adds an optional endpoint pair:{" "}
                        <code className="px-1 py-0.5 rounded bg-gray-100 dark:bg-shadeA font-semibold">Name (From -&gt; To)</code>{" "}
                        or{" "}
                        <code className="px-1 py-0.5 rounded bg-gray-100 dark:bg-shadeA font-semibold">Name (From -&gt; To): description</code>.
                        Press Enter or comma to add each entry.
                      </p>
                      <div>
                        <label className="block text-xs font-medium text-gray-700 dark:text-[#D9D9D9] mb-1">
                          Suggested Vertex Types
                        </label>
                        <TagInput
                          values={vertexHints}
                          onChange={setVertexHints}
                          disabled={isInitializing || isExtractingSchema}
                          placeholder="e.g., Company: publicly listed corporation, Filing, Person"
                          ariaLabel="Suggested vertex types"
                          forbiddenNames={forbiddenNames}
                        />
                      </div>
                      <div>
                        <label className="block text-xs font-medium text-gray-700 dark:text-[#D9D9D9] mb-1">
                          Suggested Edge Types
                        </label>
                        <TagInput
                          values={edgeHints}
                          onChange={setEdgeHints}
                          disabled={isInitializing || isExtractingSchema}
                          placeholder="e.g., PUBLISHES (Company -> Filing), OWNS, EMPLOYS: a Company employs a Person"
                          ariaLabel="Suggested edge types"
                          acceptsEndpoints
                          forbiddenNames={forbiddenNames}
                        />
                      </div>
                    </div>
                    <Button
                      onClick={handleExtractFromSamples}
                      disabled={
                        isInitializing ||
                        isExtractingSchema ||
                        sampleFiles.length === 0 ||
                        !graphName.trim() ||
                        // Already extracted these exact files — re-running
                        // would just hit the LLM again with the same input.
                        // Picking a different file set clears the
                        // fingerprint and re-enables the button.
                        (extractedFingerprint !== null &&
                          extractedFingerprint === sampleFingerprint)
                      }
                      className="gradient text-white"
                    >
                      {isExtractingSchema ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Extracting…
                        </>
                      ) : (
                        <>Extract draft schema</>
                      )}
                    </Button>

                    {draftProposal && (
                      <div className="border border-gray-200 dark:border-[#3D3D3D] rounded p-3 space-y-4 max-h-[420px] overflow-y-auto">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-xs text-gray-500 dark:text-gray-400 flex-1 mr-2">
                            Review and edit the draft below. Each vertex auto-gets a primary
                            key <code>id</code> (STRING) — you don't need to add it. Click
                            Initialize when ready.
                          </p>
                          <button
                            type="button"
                            onClick={() => setAttributesCollapsed((c) => !c)}
                            disabled={isInitializing || isExtractingSchema}
                            className="text-xs text-blue-600 hover:underline disabled:opacity-50 whitespace-nowrap"
                          >
                            {attributesCollapsed ? "Expand attributes" : "Collapse attributes"}
                          </button>
                        </div>

                        {/* Vertex types */}
                        <div>
                          <div className="flex items-center justify-between mb-2 gap-2">
                            <h4 className="text-sm font-semibold text-black dark:text-white">
                              Vertex types ({draftProposal.vertices.length})
                            </h4>
                            <div className="flex items-center gap-2">
                              <button
                                type="button"
                                onClick={toggleAllVerticesCollapsed}
                                disabled={
                                  isInitializing ||
                                  isExtractingSchema ||
                                  draftProposal.vertices.length === 0
                                }
                                className="text-xs text-blue-600 hover:underline disabled:opacity-50 whitespace-nowrap"
                              >
                                {allVerticesCollapsed
                                  ? "Expand all vertex types"
                                  : "Collapse all vertex types"}
                              </button>
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={isInitializing || isExtractingSchema}
                                onClick={() =>
                                  setDraftProposal((p) =>
                                    p
                                      ? {
                                          ...p,
                                          vertices: [
                                            ...p.vertices,
                                            { name: "", description: "", attributes: [] },
                                          ],
                                        }
                                      : p
                                  )
                                }
                                className="text-xs h-7 dark:border-[#3D3D3D]"
                              >
                                + Add vertex
                              </Button>
                            </div>
                          </div>
                          <div className="space-y-2">
                            {draftProposal.vertices.map((v, vIdx) => (
                              <div
                                key={vIdx}
                                className="border border-gray-200 dark:border-[#3D3D3D] rounded p-2 space-y-2"
                              >
                                <div className="flex gap-2 items-center">
                                  <button
                                    type="button"
                                    aria-label={
                                      collapsedVertices.has(vIdx)
                                        ? "Expand vertex type"
                                        : "Collapse vertex type"
                                    }
                                    onClick={() => toggleVertexCollapsed(vIdx)}
                                    className="text-xs text-gray-500 hover:text-gray-800 dark:hover:text-white w-5 h-7 flex items-center justify-center"
                                  >
                                    {collapsedVertices.has(vIdx) ? "▶" : "▼"}
                                  </button>
                                  <Input
                                    placeholder="VertexName"
                                    value={v.name}
                                    onChange={(e) =>
                                      setDraftProposal((p) =>
                                        p
                                          ? {
                                              ...p,
                                              vertices: p.vertices.map((vv, i) =>
                                                i === vIdx ? { ...vv, name: e.target.value } : vv
                                              ),
                                            }
                                          : p
                                      )
                                    }
                                    disabled={isInitializing || isExtractingSchema}
                                    className="flex-1 h-8 text-sm dark:border-[#3D3D3D] dark:bg-shadeA"
                                  />
                                  {collapsedVertices.has(vIdx) && (
                                    <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[40%]">
                                      {v.attributes.length} attr{v.attributes.length === 1 ? "" : "s"}
                                    </span>
                                  )}
                                  <button
                                    type="button"
                                    disabled={isInitializing || isExtractingSchema}
                                    onClick={() =>
                                      setDraftProposal((p) =>
                                        p
                                          ? {
                                              ...p,
                                              vertices: p.vertices.filter((_, i) => i !== vIdx),
                                            }
                                          : p
                                      )
                                    }
                                    className="text-xs text-red-600 hover:underline disabled:opacity-50"
                                  >
                                    Remove
                                  </button>
                                </div>
                                {!collapsedVertices.has(vIdx) && (<>
                                <Input
                                  placeholder="Description (1 sentence)"
                                  value={v.description}
                                  onChange={(e) =>
                                    setDraftProposal((p) =>
                                      p
                                        ? {
                                            ...p,
                                            vertices: p.vertices.map((vv, i) =>
                                              i === vIdx
                                                ? { ...vv, description: e.target.value }
                                                : vv
                                            ),
                                          }
                                        : p
                                    )
                                  }
                                  disabled={isInitializing || isExtractingSchema}
                                  className="h-8 text-sm dark:border-[#3D3D3D] dark:bg-shadeA"
                                />
                                <div className="text-xs text-gray-500 dark:text-gray-400">
                                  Attributes ({v.attributes.length}); primary key <code>id</code> auto-added
                                  {attributesCollapsed && (
                                    <span className="ml-2 text-gray-400">— collapsed</span>
                                  )}
                                </div>
                                {!attributesCollapsed && v.attributes.map((a, aIdx) => (
                                  <div key={aIdx} className="flex gap-2 items-center">
                                    <Input
                                      placeholder="attr_name"
                                      value={a.name}
                                      onChange={(e) =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                vertices: p.vertices.map((vv, i) =>
                                                  i === vIdx
                                                    ? {
                                                        ...vv,
                                                        attributes: vv.attributes.map(
                                                          (aa, j) =>
                                                            j === aIdx
                                                              ? {
                                                                  ...aa,
                                                                  // Auto-replace whitespace
                                                                  // with underscores so the
                                                                  // displayed name always
                                                                  // matches the GSQL
                                                                  // identifier that will be
                                                                  // emitted (whitespace is
                                                                  // not a valid char in
                                                                  // GSQL idents).
                                                                  name: e.target.value.replace(
                                                                    /\s+/g,
                                                                    "_"
                                                                  ),
                                                                }
                                                              : aa
                                                        ),
                                                      }
                                                    : vv
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      disabled={isInitializing || isExtractingSchema}
                                      className="flex-1 h-7 text-xs font-mono dark:border-[#3D3D3D] dark:bg-shadeA"
                                    />
                                    <select
                                      value={a.type}
                                      onChange={(e) =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                vertices: p.vertices.map((vv, i) =>
                                                  i === vIdx
                                                    ? {
                                                        ...vv,
                                                        attributes: vv.attributes.map(
                                                          (aa, j) =>
                                                            j === aIdx
                                                              ? { ...aa, type: e.target.value }
                                                              : aa
                                                        ),
                                                      }
                                                    : vv
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      disabled={isInitializing || isExtractingSchema}
                                      className="h-7 text-xs border rounded px-1 dark:border-[#3D3D3D] dark:bg-shadeA"
                                    >
                                      {PRIMITIVE_TYPES.map((t) => (
                                        <option key={t} value={t}>
                                          {t}
                                        </option>
                                      ))}
                                    </select>
                                    <button
                                      type="button"
                                      disabled={isInitializing || isExtractingSchema}
                                      onClick={() =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                vertices: p.vertices.map((vv, i) =>
                                                  i === vIdx
                                                    ? {
                                                        ...vv,
                                                        attributes: vv.attributes.filter(
                                                          (_, j) => j !== aIdx
                                                        ),
                                                      }
                                                    : vv
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      className="text-xs text-red-600 hover:underline disabled:opacity-50"
                                    >
                                      ✕
                                    </button>
                                  </div>
                                ))}
                                {!attributesCollapsed && (
                                  <button
                                    type="button"
                                    disabled={isInitializing || isExtractingSchema}
                                    onClick={() =>
                                      setDraftProposal((p) =>
                                        p
                                          ? {
                                              ...p,
                                              vertices: p.vertices.map((vv, i) =>
                                                i === vIdx
                                                  ? {
                                                      ...vv,
                                                      attributes: [
                                                        ...vv.attributes,
                                                        { name: "", type: "STRING" },
                                                      ],
                                                    }
                                                  : vv
                                              ),
                                            }
                                          : p
                                      )
                                    }
                                    className="text-xs text-blue-600 hover:underline disabled:opacity-50"
                                  >
                                    + Add attribute
                                  </button>
                                )}
                                </>)}
                              </div>
                            ))}
                          </div>
                        </div>

                        {/* Edge types */}
                        <div>
                          <div className="flex items-center justify-between mb-2 gap-2">
                            <h4 className="text-sm font-semibold text-black dark:text-white">
                              Edge types ({draftProposal.edges.length})
                            </h4>
                            <div className="flex items-center gap-2">
                              <button
                                type="button"
                                onClick={toggleAllEdgesCollapsed}
                                disabled={
                                  isInitializing ||
                                  isExtractingSchema ||
                                  draftProposal.edges.length === 0
                                }
                                className="text-xs text-blue-600 hover:underline disabled:opacity-50 whitespace-nowrap"
                              >
                                {allEdgesCollapsed
                                  ? "Expand all edge types"
                                  : "Collapse all edge types"}
                              </button>
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={isInitializing || isExtractingSchema}
                                onClick={() =>
                                  setDraftProposal((p) =>
                                    p
                                      ? {
                                          ...p,
                                          edges: [
                                            ...p.edges,
                                            {
                                              name: "",
                                              description: "",
                                              pairs: [["", ""]],
                                              attributes: [],
                                            },
                                          ],
                                        }
                                      : p
                                  )
                                }
                                className="text-xs h-7 dark:border-[#3D3D3D]"
                              >
                                + Add edge
                              </Button>
                            </div>
                          </div>
                          <div className="space-y-2">
                            {draftProposal.edges.map((e, eIdx) => (
                              <div
                                key={eIdx}
                                className="border border-gray-200 dark:border-[#3D3D3D] rounded p-2 space-y-2"
                              >
                                <div className="flex gap-2 items-center">
                                  <button
                                    type="button"
                                    aria-label={
                                      collapsedEdges.has(eIdx)
                                        ? "Expand edge type"
                                        : "Collapse edge type"
                                    }
                                    onClick={() => toggleEdgeCollapsed(eIdx)}
                                    className="text-xs text-gray-500 hover:text-gray-800 dark:hover:text-white w-5 h-7 flex items-center justify-center"
                                  >
                                    {collapsedEdges.has(eIdx) ? "▶" : "▼"}
                                  </button>
                                  <Input
                                    placeholder="EDGE_NAME"
                                    value={e.name}
                                    onChange={(ev) =>
                                      setDraftProposal((p) =>
                                        p
                                          ? {
                                              ...p,
                                              edges: p.edges.map((ee, i) =>
                                                i === eIdx
                                                  ? { ...ee, name: ev.target.value }
                                                  : ee
                                              ),
                                            }
                                          : p
                                      )
                                    }
                                    disabled={isInitializing || isExtractingSchema}
                                    className="flex-1 h-8 text-sm dark:border-[#3D3D3D] dark:bg-shadeA"
                                  />
                                  {collapsedEdges.has(eIdx) && (
                                    <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[40%]">
                                      {e.pairs.length} pair{e.pairs.length === 1 ? "" : "s"}, {e.attributes.length} attr
                                      {e.attributes.length === 1 ? "" : "s"}
                                    </span>
                                  )}
                                  <button
                                    type="button"
                                    disabled={isInitializing || isExtractingSchema}
                                    onClick={() =>
                                      setDraftProposal((p) =>
                                        p
                                          ? {
                                              ...p,
                                              edges: p.edges.filter((_, i) => i !== eIdx),
                                            }
                                          : p
                                      )
                                    }
                                    className="text-xs text-red-600 hover:underline disabled:opacity-50"
                                  >
                                    Remove
                                  </button>
                                </div>
                                {!collapsedEdges.has(eIdx) && (<>
                                <Input
                                  placeholder="Description (1 sentence)"
                                  value={e.description}
                                  onChange={(ev) =>
                                    setDraftProposal((p) =>
                                      p
                                        ? {
                                            ...p,
                                            edges: p.edges.map((ee, i) =>
                                              i === eIdx
                                                ? { ...ee, description: ev.target.value }
                                                : ee
                                            ),
                                          }
                                        : p
                                    )
                                  }
                                  disabled={isInitializing || isExtractingSchema}
                                  className="h-8 text-sm dark:border-[#3D3D3D] dark:bg-shadeA"
                                />
                                <div className="text-xs text-gray-500 dark:text-gray-400">
                                  Endpoints (FROM → TO):
                                </div>
                                {e.pairs.map((pair, pIdx) => (
                                  <div key={pIdx} className="flex gap-2 items-center">
                                    <Input
                                      placeholder="FromVertex"
                                      value={pair[0]}
                                      onChange={(ev) =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                edges: p.edges.map((ee, i) =>
                                                  i === eIdx
                                                    ? {
                                                        ...ee,
                                                        pairs: ee.pairs.map((pr, j) =>
                                                          j === pIdx
                                                            ? [ev.target.value, pr[1]]
                                                            : pr
                                                        ) as Array<[string, string]>,
                                                      }
                                                    : ee
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      disabled={isInitializing || isExtractingSchema}
                                      className="flex-1 h-7 text-xs dark:border-[#3D3D3D] dark:bg-shadeA"
                                    />
                                    <span className="text-xs text-gray-500">→</span>
                                    <Input
                                      placeholder="ToVertex"
                                      value={pair[1]}
                                      onChange={(ev) =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                edges: p.edges.map((ee, i) =>
                                                  i === eIdx
                                                    ? {
                                                        ...ee,
                                                        pairs: ee.pairs.map((pr, j) =>
                                                          j === pIdx
                                                            ? [pr[0], ev.target.value]
                                                            : pr
                                                        ) as Array<[string, string]>,
                                                      }
                                                    : ee
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      disabled={isInitializing || isExtractingSchema}
                                      className="flex-1 h-7 text-xs dark:border-[#3D3D3D] dark:bg-shadeA"
                                    />
                                    <button
                                      type="button"
                                      disabled={isInitializing || isExtractingSchema}
                                      onClick={() =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                edges: p.edges.map((ee, i) =>
                                                  i === eIdx
                                                    ? {
                                                        ...ee,
                                                        pairs: ee.pairs.filter(
                                                          (_, j) => j !== pIdx
                                                        ),
                                                      }
                                                    : ee
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      className="text-xs text-red-600 hover:underline disabled:opacity-50"
                                    >
                                      ✕
                                    </button>
                                  </div>
                                ))}
                                <button
                                  type="button"
                                  disabled={isInitializing || isExtractingSchema}
                                  onClick={() =>
                                    setDraftProposal((p) =>
                                      p
                                        ? {
                                            ...p,
                                            edges: p.edges.map((ee, i) =>
                                              i === eIdx
                                                ? {
                                                    ...ee,
                                                    pairs: [...ee.pairs, ["", ""]] as Array<
                                                      [string, string]
                                                    >,
                                                  }
                                                : ee
                                            ),
                                          }
                                        : p
                                    )
                                  }
                                  className="text-xs text-blue-600 hover:underline disabled:opacity-50"
                                >
                                  + Add pair
                                </button>
                                <div className="text-xs text-gray-500 dark:text-gray-400 pt-1">
                                  Attributes ({e.attributes.length}, optional)
                                  {attributesCollapsed && (
                                    <span className="ml-2 text-gray-400">— collapsed</span>
                                  )}
                                </div>
                                {!attributesCollapsed && e.attributes.map((a, aIdx) => (
                                  <div key={aIdx} className="flex gap-2 items-center">
                                    <Input
                                      placeholder="attr_name"
                                      value={a.name}
                                      onChange={(ev) =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                edges: p.edges.map((ee, i) =>
                                                  i === eIdx
                                                    ? {
                                                        ...ee,
                                                        attributes: ee.attributes.map(
                                                          (aa, j) =>
                                                            j === aIdx
                                                              ? {
                                                                  ...aa,
                                                                  // Auto-replace whitespace
                                                                  // with underscores —
                                                                  // GSQL idents can't have
                                                                  // spaces, and rendering
                                                                  // them as `_` makes the
                                                                  // visual unambiguous.
                                                                  name: ev.target.value.replace(
                                                                    /\s+/g,
                                                                    "_"
                                                                  ),
                                                                }
                                                              : aa
                                                        ),
                                                      }
                                                    : ee
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      disabled={isInitializing || isExtractingSchema}
                                      className="flex-1 h-7 text-xs font-mono dark:border-[#3D3D3D] dark:bg-shadeA"
                                    />
                                    <select
                                      value={a.type}
                                      onChange={(ev) =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                edges: p.edges.map((ee, i) =>
                                                  i === eIdx
                                                    ? {
                                                        ...ee,
                                                        attributes: ee.attributes.map(
                                                          (aa, j) =>
                                                            j === aIdx
                                                              ? { ...aa, type: ev.target.value }
                                                              : aa
                                                        ),
                                                      }
                                                    : ee
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      disabled={isInitializing || isExtractingSchema}
                                      className="h-7 text-xs border rounded px-1 dark:border-[#3D3D3D] dark:bg-shadeA"
                                    >
                                      {PRIMITIVE_TYPES.map((t) => (
                                        <option key={t} value={t}>
                                          {t}
                                        </option>
                                      ))}
                                    </select>
                                    <button
                                      type="button"
                                      disabled={isInitializing || isExtractingSchema}
                                      onClick={() =>
                                        setDraftProposal((p) =>
                                          p
                                            ? {
                                                ...p,
                                                edges: p.edges.map((ee, i) =>
                                                  i === eIdx
                                                    ? {
                                                        ...ee,
                                                        attributes: ee.attributes.filter(
                                                          (_, j) => j !== aIdx
                                                        ),
                                                      }
                                                    : ee
                                                ),
                                              }
                                            : p
                                        )
                                      }
                                      className="text-xs text-red-600 hover:underline disabled:opacity-50"
                                    >
                                      ✕
                                    </button>
                                  </div>
                                ))}
                                {!attributesCollapsed && (
                                  <button
                                    type="button"
                                    disabled={isInitializing || isExtractingSchema}
                                    onClick={() =>
                                      setDraftProposal((p) =>
                                        p
                                          ? {
                                              ...p,
                                              edges: p.edges.map((ee, i) =>
                                                i === eIdx
                                                  ? {
                                                      ...ee,
                                                      attributes: [
                                                        ...ee.attributes,
                                                        { name: "", type: "STRING" },
                                                      ],
                                                    }
                                                  : ee
                                              ),
                                            }
                                          : p
                                      )
                                    }
                                    className="text-xs text-blue-600 hover:underline disabled:opacity-50"
                                  >
                                    + Add attribute
                                  </button>
                                )}
                                </>)}
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {schemaSource === "gsql" && (
                  <div className="space-y-2">
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      Paste TigerGraph GSQL <code>ADD VERTEX</code> /
                      <code> ADD [UN]DIRECTED EDGE</code> statements (or output of
                      <code> gsql ls</code>). If you don't include a
                      <code> PRIMARY_ID</code>, the system auto-adds
                      <code> PRIMARY_ID id STRING</code>. Lines that don't match
                      VERTEX / EDGE patterns are silently ignored.
                    </p>
                    <textarea
                      placeholder={
                        "// A corporate entity.\n" +
                        "ADD VERTEX Company(PRIMARY_ID id STRING, name STRING, founded_year INT);\n\n" +
                        "// A formal report summarizing performance.\n" +
                        "ADD VERTEX Report(PRIMARY_ID id STRING, title STRING);\n\n" +
                        "// A company publishes a report.\n" +
                        "ADD DIRECTED EDGE PUBLISHES(FROM Company, TO Report, effective_date STRING);"
                      }
                      value={pasteGsql}
                      onChange={(e) => setPasteGsql(e.target.value)}
                      disabled={isInitializing || isExtractingSchema}
                      rows={20}
                      className="w-full text-xs font-mono p-3 border rounded dark:border-[#3D3D3D] dark:bg-shadeA leading-snug"
                      spellCheck={false}
                      style={{ tabSize: 2 }}
                    />
                  </div>
                )}
              </div>

              {statusMessage && (
                <div
                  className={`p-3 rounded-lg text-sm ${
                    statusType === "success"
                      ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                      : statusType === "error"
                      ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                      : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                  }`}
                >
                  {statusMessage}
                </div>
              )}
            </div>

            <DialogFooter>
              {isInitComplete ? (
                <Button
                  className="gradient text-white w-full"
                  onClick={() => {
                    handleInitializeReset();
                    setInitializeDialogOpen(false);
                  }}
                >
                  Done
                </Button>
              ) : (
                <>
                  <Button
                    variant="outline"
                    onClick={() => handleInitializeDialogChange(false)}
                    disabled={isInitializing}
                    className="dark:border-[#3D3D3D]"
                  >
                    Close
                  </Button>
                  <Button
                    onClick={handleInitializeGraph}
                    disabled={
                      isInitializing ||
                      isExtractingSchema ||
                      !graphName.trim() ||
                      // "None" requires a successful precheck before
                      // Create & Init becomes clickable. Precheck
                      // verifies the graph is new or, if it has
                      // existing types, collects descriptions for them.
                      (schemaSource === "none" && !precheckPassed) ||
                      // "Generate from sample documents" is only ready
                      // to submit once the LLM has returned a draft
                      // proposal with at least one vertex.
                      (schemaSource === "samples" &&
                        (!draftProposal ||
                          draftProposal.vertices.length === 0)) ||
                      // "Paste GSQL schema" needs non-empty content;
                      // an empty paste is effectively the "None" path
                      // and should be picked explicitly there.
                      (schemaSource === "gsql" && !pasteGsql.trim())
                    }
                    className="gradient text-white"
                  >
                    {isInitializing ? (
                      <>
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        Creating...
                      </>
                    ) : (
                      <>
                        <Database className="h-4 w-4 mr-2" />
                        Create & Initialize
                      </>
                    )}
                  </Button>
                </>
              )}
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Description-edit dialog for the adopt-existing path. Names
            are read-only; the user reviews/edits LLM-seeded descriptions
            and clicks Accept to unlock Create & Initialize. */}
        <Dialog
          open={descDialogOpen}
          onOpenChange={(open) => {
            if (!open) setDescDialogOpen(false);
          }}
        >
          <DialogContent
            className="sm:max-w-[760px] max-h-[90vh] overflow-y-auto bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]"
            onInteractOutside={(e) => e.preventDefault()}
          >
            <DialogHeader>
              <DialogTitle className="text-black dark:text-white">
                Use existing schema as domain types
              </DialogTitle>
              <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
                Review and edit descriptions for each existing type. They will be saved with the graph and used by query and extraction tools.
              </DialogDescription>
            </DialogHeader>

            <div className="py-4 space-y-4">
              {descDialogLoading && (
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  <Loader2 className="h-3 w-3 mr-2 animate-spin inline" />
                  Generating description suggestions...
                </p>
              )}

              {descDialogVertices.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold mb-2 text-black dark:text-white">
                    Vertex types
                  </h3>
                  <div className="space-y-2">
                    {descDialogVertices.map((v) => (
                      <div key={v} className="grid grid-cols-[10rem_1fr] gap-3 items-start">
                        <span className="text-sm font-mono text-black dark:text-white pt-2 break-all">
                          {v}
                        </span>
                        <Input
                          value={descDialogVertexDescs[v] || ""}
                          onChange={(e) =>
                            setDescDialogVertexDescs((prev) => ({
                              ...prev,
                              [v]: e.target.value,
                            }))
                          }
                          placeholder="One-sentence description"
                          className="dark:border-[#3D3D3D] dark:bg-shadeA"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {descDialogEdges.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold mb-2 text-black dark:text-white">
                    Edge types
                  </h3>
                  <div className="space-y-2">
                    {descDialogEdges.map((e) => (
                      <div key={e.name} className="grid grid-cols-[10rem_1fr] gap-3 items-start">
                        <div className="text-sm pt-2">
                          <div className="font-mono text-black dark:text-white break-all">
                            {e.name}
                          </div>
                          {e.from && e.to && (
                            <div className="text-xs text-gray-500 dark:text-gray-400">
                              {e.from} → {e.to}
                            </div>
                          )}
                        </div>
                        <Input
                          value={descDialogEdgeDescs[e.name] || ""}
                          onChange={(ev) =>
                            setDescDialogEdgeDescs((prev) => ({
                              ...prev,
                              [e.name]: ev.target.value,
                            }))
                          }
                          placeholder="One-sentence description"
                          className="dark:border-[#3D3D3D] dark:bg-shadeA"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setDescDialogOpen(false)}
                className="dark:border-[#3D3D3D]"
              >
                Cancel
              </Button>
              <Button
                onClick={handleAcceptDescriptions}
                className="gradient text-white"
              >
                Accept
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Ingest Dialog */}
        <Dialog
          open={ingestDialogOpen}
          onOpenChange={(open) => {
            if (!open && isConfirmDialogOpen) {
              return;
            }
            setIngestDialogOpen(open);
          }}
        >
          <DialogContent
            className="sm:max-w-[700px] bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D] max-h-[80vh] overflow-y-auto"
            onInteractOutside={(e) => e.preventDefault()}
          >
            <DialogHeader>
              <DialogTitle className="text-black dark:text-white">Document Ingestion for Knowledge Graph</DialogTitle>
              <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
                Upload files locally, download from cloud storage, or configure Amazon Bedrock Data Automation for document ingestion
              </DialogDescription>
            </DialogHeader>
            <IngestGraph isModal={true} />
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setIngestDialogOpen(false)}
                className="dark:border-[#3D3D3D]"
              >
                Close
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Rebuild Dialog */}
        <Dialog open={refreshDialogOpen} onOpenChange={handleRefreshDialogChange}>
          <DialogContent
            className="sm:max-w-[500px] bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]"
            onInteractOutside={(e) => e.preventDefault()}
          >
            <DialogHeader>
              <DialogTitle className="text-black dark:text-white">Rebuild Knowledge Graph</DialogTitle>
              <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
                Process documents and rerun entity extraction + community detection for your knowledge graph.
              </DialogDescription>
            </DialogHeader>

            <div className="py-4 space-y-4">
              <div>
                <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                  Select Graph to Rebuild
                </label>
                <Select
                  value={refreshGraphName}
                  onValueChange={(v) => {
                    setRefreshGraphName(v);
                    sessionStorage.setItem("selectedGraph", v);
                    window.dispatchEvent(new Event("graphrag:selectedGraph"));
                  }}
                  disabled={isRefreshing || isRebuildRunning || isCheckingStatus}
                >
                  <SelectTrigger
                    className="dark:border-[#3D3D3D] dark:bg-shadeA"
                    disabled={isRefreshing || isRebuildRunning || isCheckingStatus}
                  >
                    <SelectValue placeholder="Select a graph" />
                  </SelectTrigger>
                  <SelectContent>
                    {availableGraphs.length > 0 ? (
                      availableGraphs.map((graph) => (
                        <SelectItem key={graph} value={graph}>
                          {graph}
                        </SelectItem>
                      ))
                    ) : (
                      <SelectItem value="no-graphs" disabled>
                        No graphs available
                      </SelectItem>
                    )}
                  </SelectContent>
                </Select>
              </div>

              <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4">
                <p className="text-sm text-yellow-800 dark:text-yellow-200 font-medium">
                  ⚠️ Warning
                </p>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  This operation will process new documents and rerun community detection that will interrupt related queries.
                  Please confirm to proceed.
                </p>
              </div>

              {refreshMessage && (
                <div className={`p-3 rounded-lg text-sm ${
                  refreshMessage.includes("✅")
                    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                    : refreshMessage.includes("❌")
                    ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                    : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                }`}>
                  {refreshMessage}
                </div>
              )}
            </div>

            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => handleRefreshDialogChange(false)}
                disabled={isRefreshing}
                className="dark:border-[#3D3D3D]"
              >
                Close
              </Button>
              <Button
                onClick={handleRefreshGraph}
                disabled={isRefreshing || !refreshGraphName || isRebuildRunning || isCheckingStatus}
                className="gradient text-white"
              >
                {isRefreshing ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Submitting...
                  </>
                ) : isRebuildRunning ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Rebuild In Progress...
                  </>
                ) : isCheckingStatus ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Checking Status...
                  </>
                ) : (
                  <>
                    <RefreshCw className="h-4 w-4 mr-2" />
                    Confirm & Rebuild
                  </>
                )}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
      {confirmDialog}
      {alertDialog}
    </div>
  );
};

export default KGAdmin;
