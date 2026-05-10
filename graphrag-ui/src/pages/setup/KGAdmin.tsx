import React, { useState, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Database, Loader2, RefreshCw, Upload } from "lucide-react";
import { pauseIdleTimer, resumeIdleTimer } from "@/hooks/useIdleTimeout";
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
import { useNavigate } from "react-router-dom";
import IngestGraph from "./IngestGraph";

const KGAdmin = () => {
  const [confirm, confirmDialog, isConfirmDialogOpen] = useConfirm();
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
    setIsInitComplete(false);
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
  // Fingerprint of the file set used for the most recent successful
  // extraction. Used to disable the *Extract draft schema* button
  // when the same files are selected (no new work to do).
  const [extractedFingerprint, setExtractedFingerprint] = useState<string | null>(null);
  // True when the form-mode editor's per-card attribute lists are
  // hidden, for a cleaner overview of types.
  const [attributesCollapsed, setAttributesCollapsed] = useState<boolean>(false);

  const fingerprintFiles = (files: File[]): string =>
    files
      .map((f) => `${f.name}:${f.size}:${f.lastModified}`)
      .sort()
      .join("|");

  const sampleFingerprint = fingerprintFiles(sampleFiles);

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
  const [refreshGraphName, setRefreshGraphName] = useState("");
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
  }, [initializeDialogOpen]);

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
          body: JSON.stringify({ filenames: convertData.saved_files || [] }),
        }
      );
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || `Extraction failed: ${resp.statusText}`);
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
      setExtractedFingerprint(fingerprintFiles(sampleFiles));
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
      const initBody: { schema_gsql?: string } = {};
      if (schemaSource === "gsql" && pasteGsql.trim()) {
        initBody.schema_gsql = pasteGsql;
      } else if (schemaSource === "samples" && draftProposal) {
        const gsql = draftProposalToGsql(draftProposal).trim();
        if (gsql) initBody.schema_gsql = gsql;
      }
      // Submit the init job. The backend kicks off a BackgroundTask
      // and returns 202 immediately so the browser doesn't drop the
      // request mid-flight on long inits (TG schema-change + retriever
      // installs can take 10+ minutes).
      const initResponse = await fetch(`/ui/${graphName}/initialize_graph`, {
        method: "POST",
        headers: {
          Authorization: `Basic ${creds}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(initBody),
      });

      const initData = await initResponse.json();

      if (!initResponse.ok) {
        throw new Error(
          initData.detail || `Failed to submit init: ${initResponse.statusText}`
        );
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
          setPollingActive(true);
          const startTime = statusData.started_at
            ? new Date(statusData.started_at * 1000).toLocaleString()
            : "unknown time";
          setRefreshMessage(
            `⚠️ A rebuild is already in progress for "${graphName}" (started at ${startTime}). Please wait for it to complete.`
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

  // Refresh Graph
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
      `Are you sure you want to refresh the knowledge graph "${refreshGraphName}"? This will rebuild the graph content.`
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
          errorData.detail || `Failed to refresh graph: ${response.statusText}`
        );
      }

      const data = await response.json();
      console.log("Refresh response:", data);

      setRefreshMessage(
        `✅ Refresh submitted successfully! The knowledge graph "${refreshGraphName}" is being rebuilt.`
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
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
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

          {/* Refresh Card */}
          <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
            <div className="mb-4">
              <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
                <RefreshCw className="h-6 w-6 text-tigerOrange" />
              </div>
              <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">
                Refresh Knowledge Graph
              </h2>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
                Process new documents in your knowledge graph to refresh its content.
              </p>
            </div>
            <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
              <Button
                onClick={() => setRefreshDialogOpen(true)}
                className="gradient w-full text-white"
              >
                <RefreshCw className="h-4 w-4 mr-2" />
                Refresh Graph
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
                <Input
                  placeholder="e.g., MyKnowledgeGraph"
                  value={graphName}
                  onChange={(e) => setGraphName(e.target.value)}
                  disabled={isInitializing || isExtractingSchema}
                  className="dark:border-[#3D3D3D] dark:bg-shadeA"
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !isInitializing && !isExtractingSchema) {
                      handleInitializeGraph();
                    }
                  }}
                />
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
                        <div className="flex items-center justify-between">
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
                          <div className="flex items-center justify-between mb-2">
                            <h4 className="text-sm font-semibold text-black dark:text-white">
                              Vertex types ({draftProposal.vertices.length})
                            </h4>
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
                          <div className="space-y-2">
                            {draftProposal.vertices.map((v, vIdx) => (
                              <div
                                key={vIdx}
                                className="border border-gray-200 dark:border-[#3D3D3D] rounded p-2 space-y-2"
                              >
                                <div className="flex gap-2 items-center">
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
                              </div>
                            ))}
                          </div>
                        </div>

                        {/* Edge types */}
                        <div>
                          <div className="flex items-center justify-between mb-2">
                            <h4 className="text-sm font-semibold text-black dark:text-white">
                              Edge types ({draftProposal.edges.length})
                            </h4>
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
                          <div className="space-y-2">
                            {draftProposal.edges.map((e, eIdx) => (
                              <div
                                key={eIdx}
                                className="border border-gray-200 dark:border-[#3D3D3D] rounded p-2 space-y-2"
                              >
                                <div className="flex gap-2 items-center">
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
                    Cancel
                  </Button>
                  <Button
                    onClick={handleInitializeGraph}
                    disabled={
                      isInitializing ||
                      isExtractingSchema ||
                      !graphName.trim() ||
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

        {/* Refresh Dialog */}
        <Dialog open={refreshDialogOpen} onOpenChange={handleRefreshDialogChange}>
          <DialogContent
            className="sm:max-w-[500px] bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]"
            onInteractOutside={(e) => e.preventDefault()}
          >
            <DialogHeader>
              <DialogTitle className="text-black dark:text-white">Refresh Knowledge Graph</DialogTitle>
              <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
                Rebuild the graph content and rerun community detection for your knowledge graph
              </DialogDescription>
            </DialogHeader>

            <div className="py-4 space-y-4">
              <div>
                <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                  Select Graph to Refresh
                </label>
                <Select
                  value={refreshGraphName}
                  onValueChange={setRefreshGraphName}
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
                    Confirm & Refresh
                  </>
                )}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
      {confirmDialog}
    </div>
  );
};

export default KGAdmin;
