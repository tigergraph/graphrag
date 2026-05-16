import React, { useState, useEffect, useRef } from "react";
import { Settings, Save, Loader2 } from "lucide-react";
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

const GraphRAGConfig = () => {
  const [selectedGraph, setSelectedGraph] = useState(sessionStorage.getItem("selectedGraph") || "");
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);
  const [reuseEmbedding, setReuseEmbedding] = useState(true);
  const [eccUrl, setEccUrl] = useState("http://graphrag-ecc:8001");
  const [chatHistoryUrl, setChatHistoryUrl] = useState("http://chat-history:8002");

  // Default chunker (used when no chunker specified in document)
  const [defaultChunker, setDefaultChunker] = useState("semantic");

  // Retrieval settings
  const [topK, setTopK] = useState("5");
  const [numHops, setNumHops] = useState("2");
  const [numSeenMin, setNumSeenMin] = useState("2");
  const [communityLevel, setCommunityLevel] = useState("2");
  const [docOnly, setDocOnly] = useState(false);
  const [enableRouterFallback, setEnableRouterFallback] = useState(true);

  // Collapsible section toggles (Configuration Scope and General Settings
  // are always shown). Advanced Ingestion stays collapsed by default —
  // matches the prior behavior.
  const [showChunker, setShowChunker] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showSchema, setShowSchema] = useState(false);
  const [showEndpoints, setShowEndpoints] = useState(false);
  const [loadBatchSize, setLoadBatchSize] = useState("500");
  const [upsertDelay, setUpsertDelay] = useState("0");
  const [maxConcurrency, setMaxConcurrency] = useState("10");

  // Schema-aware initialization (Phase 1 sample-doc path)
  const [schemaMaxSampleFiles, setSchemaMaxSampleFiles] = useState("5");
  const [schemaMaxTotalMb, setSchemaMaxTotalMb] = useState("50");
  // Dynamic schema behavior at extraction and retrieval time
  const [strictMode, setStrictMode] = useState(false);
  // Tri-state: "auto" leaves the key unset (server picks based on whether
  // a domain schema exists); "true"/"false" force the behavior.
  const [retrievalIncludeEntity, setRetrievalIncludeEntity] = useState<"auto" | "true" | "false">("auto");

  // Chunker-specific settings
  const [chunkSize, setChunkSize] = useState("");
  const [overlapSize, setOverlapSize] = useState("");
  const [semanticMethod, setSemanticMethod] = useState("");
  const [semanticThreshold, setSemanticThreshold] = useState("");
  const [regexPattern, setRegexPattern] = useState("");

  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState<"success" | "error" | "">("");

  // Scope: "global" edits global config, "graph" edits per-graph overrides
  const [configScope, setConfigScope] = useState<"global" | "graph">("global");
  const [graphOverrides, setGraphOverrides] = useState<Record<string, any>>({});

  // Track configs as loaded from API so we only save what's needed
  const loadedGlobalConfig = useRef<Record<string, any>>({});
  const loadedGraphOverrides = useRef<Record<string, any>>({});

  useEffect(() => {
    const site = JSON.parse(sessionStorage.getItem("site") || "{}");
    setAvailableGraphs(site.graphs || []);
    fetchConfig();
  }, []);


  const applyGraphragConfig = (graphragConfig: any) => {
    if (!graphragConfig) return;
    setReuseEmbedding(graphragConfig.reuse_embedding ?? true);
    setEccUrl(graphragConfig.ecc || "http://graphrag-ecc:8001");
    setChatHistoryUrl(graphragConfig.chat_history_api || "http://chat-history:8002");
    setDefaultChunker(graphragConfig.chunker || "semantic");
    setTopK(String(graphragConfig.top_k ?? 5));
    setNumHops(String(graphragConfig.num_hops ?? 2));
    setNumSeenMin(String(graphragConfig.num_seen_min ?? 2));
    setCommunityLevel(String(graphragConfig.community_level ?? 2));
    setDocOnly(graphragConfig.doc_only ?? false);
    setEnableRouterFallback(graphragConfig.enable_router_fallback ?? true);
    setLoadBatchSize(String(graphragConfig.load_batch_size ?? 500));
    setUpsertDelay(String(graphragConfig.upsert_delay ?? 0));
    setMaxConcurrency(String(graphragConfig.default_concurrency ?? 10));
    setSchemaMaxSampleFiles(String(graphragConfig.schema_max_sample_files ?? 5));
    setSchemaMaxTotalMb(String(graphragConfig.schema_max_total_mb ?? 50));
    setStrictMode(graphragConfig.strict_mode ?? false);
    const rie = graphragConfig.retrieval_include_entity;
    setRetrievalIncludeEntity(rie === undefined || rie === null ? "auto" : rie ? "true" : "false");

    const chunkerConfig = graphragConfig.chunker_config || {};
    setChunkSize(String(chunkerConfig.chunk_size ?? ""));
    setOverlapSize(chunkerConfig.overlap_size != null ? String(chunkerConfig.overlap_size) : "");
    setSemanticMethod(chunkerConfig.method || "");
    setSemanticThreshold(chunkerConfig.threshold != null ? String(chunkerConfig.threshold) : "");
    setRegexPattern(chunkerConfig.pattern != null ? chunkerConfig.pattern : "");
  };

  const fetchConfig = async (scope?: "global" | "graph", graphname?: string) => {
    setIsLoading(true);
    const effectiveScope = scope ?? configScope;
    const effectiveGraph = graphname ?? selectedGraph;
    const creds = sessionStorage.getItem("creds");
    const params = new URLSearchParams();
    if (effectiveGraph) params.set("graphname", effectiveGraph);
    if (effectiveScope === "graph") params.set("scope", "graph");
    const queryString = params.toString() ? `?${params.toString()}` : "";
    const url = `/ui/config${queryString}`;

    // Transient backend failures (cold start, brief upstream timeouts via
    // nginx, momentary 502/503/504) are common right after a service
    // restart and produced the intermittent "Failed to fetch configuration"
    // on this page. Retry a few times with backoff before surfacing an
    // error to the user. Auth failures (401/403) and 4xx are not retried.
    const shouldRetry = (status: number | null, err: unknown) => {
      if (err && status === null) return true; // network error
      if (status !== null && status >= 500) return true;
      return false;
    };

    const maxAttempts = 3;
    let lastErr: any = null;
    let lastStatus: number | null = null;

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        const response = await fetch(url, {
          headers: { Authorization: `Basic ${creds}` },
        });
        lastStatus = response.status;
        if (!response.ok) {
          if (attempt < maxAttempts && shouldRetry(response.status, null)) {
            await new Promise((r) => setTimeout(r, 500 * attempt));
            continue;
          }
          throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        const deepCopy = (obj: any) => JSON.parse(JSON.stringify(obj || {}));
        loadedGlobalConfig.current = deepCopy(data.graphrag_config);

        if (effectiveScope === "graph" && data.graphrag_overrides) {
          loadedGraphOverrides.current = deepCopy(data.graphrag_overrides);
          setGraphOverrides(data.graphrag_overrides);
          // Show per-graph values: merge global + overrides for display
          const merged = { ...data.graphrag_config, ...data.graphrag_overrides };
          applyGraphragConfig(merged);
        } else {
          loadedGraphOverrides.current = {};
          setGraphOverrides({});
          applyGraphragConfig(data.graphrag_config);
        }
        // Clear any prior transient error banner on success.
        setMessage("");
        setMessageType("");
        setIsLoading(false);
        return;
      } catch (error: any) {
        lastErr = error;
        if (attempt < maxAttempts && shouldRetry(lastStatus, error)) {
          await new Promise((r) => setTimeout(r, 500 * attempt));
          continue;
        }
        break;
      }
    }

    console.error("Error fetching config:", lastErr, "status=", lastStatus);
    setMessage(
      `Failed to load configuration${lastStatus ? ` (HTTP ${lastStatus})` : ""}. Please retry.`
    );
    setMessageType("error");
    setIsLoading(false);
  };

  const handleSave = async () => {
    setIsSaving(true);
    setMessage("");
    setMessageType("");

    try {
      const creds = sessionStorage.getItem("creds");
      
      // Build current UI state — only include non-empty fields
      const currentChunkerConfig: any = {};
      if (chunkSize !== "") currentChunkerConfig.chunk_size = parseInt(chunkSize);
      if (overlapSize !== "") currentChunkerConfig.overlap_size = parseInt(overlapSize);
      if (semanticMethod !== "") currentChunkerConfig.method = semanticMethod;
      if (semanticThreshold !== "") currentChunkerConfig.threshold = parseFloat(semanticThreshold);
      if (regexPattern !== "") currentChunkerConfig.pattern = regexPattern;

      const currentConfig: any = {
        reuse_embedding: reuseEmbedding,
        ecc: eccUrl,
        chat_history_api: chatHistoryUrl,
        chunker: defaultChunker,
        chunker_config: currentChunkerConfig,
        top_k: parseInt(topK),
        num_hops: parseInt(numHops),
        num_seen_min: parseInt(numSeenMin),
        community_level: parseInt(communityLevel),
        doc_only: docOnly,
        enable_router_fallback: enableRouterFallback,
        load_batch_size: parseInt(loadBatchSize),
        upsert_delay: parseInt(upsertDelay),
        default_concurrency: parseInt(maxConcurrency),
        schema_max_sample_files: parseInt(schemaMaxSampleFiles),
        schema_max_total_mb: parseInt(schemaMaxTotalMb),
        strict_mode: strictMode,
      };
      // retrieval_include_entity: only include the key when the user has
      // picked an explicit value. "auto" should leave it unset so the
      // server-side fallback (False with domain schema, True otherwise)
      // applies.
      if (retrievalIncludeEntity !== "auto") {
        currentConfig.retrieval_include_entity = retrievalIncludeEntity === "true";
      }

      // Display defaults — used to avoid saving values the user never changed
      const displayDefaults: Record<string, any> = {
        reuse_embedding: true,
        ecc: "http://graphrag-ecc:8001",
        chat_history_api: "http://chat-history:8002",
        chunker: "semantic",
        top_k: 5,
        num_hops: 2,
        num_seen_min: 2,
        community_level: 2,
        doc_only: false,
        enable_router_fallback: true,
        load_batch_size: 500,
        upsert_delay: 0,
        default_concurrency: 10,
        schema_max_sample_files: 5,
        schema_max_total_mb: 50,
        strict_mode: false,
      };

      // Determine which config to diff against based on scope
      const globalCfg = loadedGlobalConfig.current;
      const globalChunker = globalCfg.chunker_config || {};
      const graphragConfigData: any = {};

      // Helper: should a key be saved?
      // - If it was in the reference config (loaded/overrides), always save
      // - If it differs from the reference config AND differs from display default, save
      const shouldSave = (key: string, current: any, reference: Record<string, any>, wasInRef: boolean) => {
        if (wasInRef) return true;
        const diffFromRef = JSON.stringify(current) !== JSON.stringify(reference[key]);
        const matchesDefault = JSON.stringify(current) === JSON.stringify(displayDefaults[key]);
        return diffFromRef && !matchesDefault;
      };

      if (configScope === "graph") {
        // Graph scope: save values that differ from effective global (loaded or display default)
        // or were already overridden per-graph
        const overrides = loadedGraphOverrides.current;
        const overridesChunker = overrides.chunker_config || {};

        for (const key of Object.keys(currentConfig)) {
          if (key === "chunker_config") continue;
          const effectiveGlobal = key in globalCfg ? globalCfg[key] : displayDefaults[key];
          const diffFromGlobal = JSON.stringify(currentConfig[key]) !== JSON.stringify(effectiveGlobal);
          const wasOverridden = key in overrides;
          if (wasOverridden || diffFromGlobal) {
            graphragConfigData[key] = currentConfig[key];
          }
        }

        const chunkerConfig: any = {};
        for (const key of Object.keys(currentChunkerConfig)) {
          const effectiveGlobal = key in globalChunker ? globalChunker[key] : undefined;
          const diffFromGlobal = JSON.stringify(currentChunkerConfig[key]) !== JSON.stringify(effectiveGlobal);
          const wasOverridden = key in overridesChunker;
          if (wasOverridden || diffFromGlobal) {
            chunkerConfig[key] = currentChunkerConfig[key];
          }
        }
        if (Object.keys(chunkerConfig).length > 0 || "chunker_config" in overrides) {
          graphragConfigData.chunker_config = chunkerConfig;
        }
      } else {
        // Global scope: save loaded keys + user changes (skip display defaults for unloaded keys)
        for (const key of Object.keys(currentConfig)) {
          if (key === "chunker_config") continue;
          if (shouldSave(key, currentConfig[key], globalCfg, key in globalCfg)) {
            graphragConfigData[key] = currentConfig[key];
          }
        }

        const chunkerConfig: any = {};
        for (const key of Object.keys(currentChunkerConfig)) {
          const wasLoaded = key in globalChunker;
          const changed = JSON.stringify(currentChunkerConfig[key]) !== JSON.stringify(globalChunker[key]);
          if (wasLoaded || changed) {
            chunkerConfig[key] = currentChunkerConfig[key];
          }
        }
        if (Object.keys(chunkerConfig).length > 0 || "chunker_config" in globalCfg) {
          graphragConfigData.chunker_config = chunkerConfig;
        }
      }

      if (configScope === "graph") {
        graphragConfigData.scope = "graph";
        graphragConfigData.graphname = selectedGraph;
      }

      const response = await fetch("/ui/config/graphrag", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(graphragConfigData),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Failed to save GraphRAG configuration");
      }

      const result = await response.json();
      setMessage(result.message || "GraphRAG configuration saved successfully!");
      setMessageType("success");
      
      // Auto-hide success message after 3 seconds
      setTimeout(() => {
        setMessage("");
        setMessageType("");
      }, 3000);
    } catch (error: any) {
      console.error("Error saving GraphRAG config:", error);
      setMessage(`❌ Error: ${error.message}`);
      setMessageType("error");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center">
              <Settings className="h-6 w-6 text-tigerOrange" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-black dark:text-white">
                GraphRAG Configuration
              </h1>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                Configure GraphRAG-specific parameters and processing settings
              </p>
            </div>
          </div>
        </div>

        {/* Config Scope Toggle */}
        <ConfigScopeToggle
          configScope={configScope}
          selectedGraph={selectedGraph}
          availableGraphs={availableGraphs}
          onScopeChange={(scope) => {
            setConfigScope(scope);
            if (scope === "global") {
              fetchConfig("global");
            } else if (selectedGraph) {
              fetchConfig("graph", selectedGraph);
            }
          }}
          onGraphChange={(value) => {
            setConfigScope("graph");
            setSelectedGraph(value);
            sessionStorage.setItem("selectedGraph", value);
            window.dispatchEvent(new Event("graphrag:selectedGraph"));
            fetchConfig("graph", value);
          }}
          graphSelectedHint={
            Object.keys(graphOverrides).length > 0
              ? `Overridden keys: ${Object.keys(graphOverrides).join(", ")}. Other settings are inherited from global.`
              : "No per-graph overrides set. All settings are inherited from global defaults."
          }
        />

        {/* Success/Error Message */}
        {message && (
          <div
            className={`p-4 rounded-lg mb-6 ${
              messageType === "success"
                ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 border border-green-200 dark:border-green-800"
                : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 border border-red-200 dark:border-red-800"
            }`}
          >
            {message}
          </div>
        )}

        <fieldset>
        <div className="space-y-6">
          {/* General Settings */}
          <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
            <h2 className="text-lg font-semibold mb-4 text-black dark:text-white">
              General Settings
            </h2>
            <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-6">
              Configure general GraphRAG parameters.
            </p>

            <div className="space-y-4">
              <div>
                <div className="flex items-center space-x-2">
                  <input
                    type="checkbox"
                    id="reuseEmbedding"
                    className="rounded border-gray-300 dark:border-[#3D3D3D]"
                    checked={reuseEmbedding}
                    onChange={(e) => setReuseEmbedding(e.target.checked)}
                  />
                  <label htmlFor="reuseEmbedding" className="text-sm font-medium text-black dark:text-white">
                    Reuse Embedding
                  </label>
                </div>
                <p className="text-xs text-gray-600 dark:text-[#D9D9D9] mt-1 ml-6">
                  Skip fetching new embedding if embedding is already attached to it
                </p>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Top K
                  </label>
                  <Input
                    type="number"
                    min="1"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="5"
                    value={topK}
                    onChange={(e) => setTopK(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Number of top similar results to retrieve during search
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Number of Hops
                  </label>
                  <Input
                    type="number"
                    min="1"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="2"
                    value={numHops}
                    onChange={(e) => setNumHops(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Number of graph hops to traverse when expanding retrieved results
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Min Seen Count
                  </label>
                  <Input
                    type="number"
                    min="1"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="2"
                    value={numSeenMin}
                    onChange={(e) => setNumSeenMin(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Minimum times a node must appear across retrievals to be included in results
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Community Level
                  </label>
                  <Input
                    type="number"
                    min="1"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="2"
                    value={communityLevel}
                    onChange={(e) => setCommunityLevel(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Community hierarchy level used for community search
                  </p>
                </div>
              </div>

              <div>
                <div className="flex items-center space-x-2">
                  <input
                    type="checkbox"
                    id="docOnly"
                    className="rounded border-gray-300 dark:border-[#3D3D3D]"
                    checked={docOnly}
                    onChange={(e) => setDocOnly(e.target.checked)}
                  />
                  <label htmlFor="docOnly" className="text-sm font-medium text-black dark:text-white">
                    Document Only Search
                  </label>
                </div>
                <p className="text-xs text-gray-600 dark:text-[#D9D9D9] mt-1 ml-6">
                  Retrieve original documents instead of document chunks in results
                </p>
              </div>

              <div>
                <div className="flex items-center space-x-2">
                  <input
                    type="checkbox"
                    id="enableRouterFallback"
                    className="rounded border-gray-300 dark:border-[#3D3D3D]"
                    checked={enableRouterFallback}
                    onChange={(e) => setEnableRouterFallback(e.target.checked)}
                  />
                  <label htmlFor="enableRouterFallback" className="text-sm font-medium text-black dark:text-white">
                    Fallback to Vector Search on Structured-Data Failure
                  </label>
                </div>
                <p className="text-xs text-gray-600 dark:text-[#D9D9D9] mt-1 ml-6">
                  Fall back to vector search when structured-data retrieval fails.
                </p>
              </div>
            </div>
          </div>

          {/* Chunker Settings */}
          <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
            <button
              type="button"
              onClick={() => setShowChunker(!showChunker)}
              className="w-full flex items-center justify-between"
            >
              <h2 className="text-lg font-semibold text-black dark:text-white">
                Chunker Settings
              </h2>
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {showChunker ? "▲ Collapse" : "▼ Expand"}
              </span>
            </button>
            {!showChunker && (
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mt-2">
                Configure document chunking for ingestion.
              </p>
            )}

            {showChunker && (
            <div className="space-y-4 mt-4">
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-2">
                Configure document chunking for ingestion.
              </p>
              <div>
                <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                  Default Chunker
                </label>
                <Select value={defaultChunker} onValueChange={setDefaultChunker}>
                  <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                    <SelectValue placeholder="Select default chunker" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="semantic">Semantic Chunker</SelectItem>
                    <SelectItem value="character">Character Chunker</SelectItem>
                    <SelectItem value="regex">Regex Chunker</SelectItem>
                    <SelectItem value="markdown">Markdown Chunker</SelectItem>
                    <SelectItem value="html">HTML Chunker</SelectItem>
                    <SelectItem value="recursive">Recursive Chunker</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Used when no chunker is specified for a document
                </p>
              </div>

              {/* Character/Markdown/Recursive chunker settings */}
              <div className="border border-gray-200 dark:border-[#3D3D3D] rounded-lg p-4">
                <h3 className="text-sm font-medium mb-3 text-black dark:text-white">
                  Character / Markdown / Recursive Chunker
                </h3>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Chunk Size
                    </label>
                    <Input
                      type="number"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="2048"
                      value={chunkSize}
                      onChange={(e) => setChunkSize(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Maximum size of each chunk
                    </p>
                  </div>
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Overlap Size
                    </label>
                    <Input
                      type="number"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="1/8 of chunk size"
                      value={overlapSize}
                      onChange={(e) => setOverlapSize(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Overlap between consecutive chunks.
                    </p>
                  </div>
                </div>
              </div>

              {/* Semantic chunker settings */}
              <div className="border border-gray-200 dark:border-[#3D3D3D] rounded-lg p-4">
                <h3 className="text-sm font-medium mb-3 text-black dark:text-white">
                  Semantic Chunker
                </h3>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Semantic Method
                    </label>
                    <Select value={semanticMethod || "percentile"} onValueChange={(v) => setSemanticMethod(v)}>
                      <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                        <SelectValue placeholder="Percentile (default)" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="percentile">Percentile</SelectItem>
                        <SelectItem value="standard_deviation">Standard Deviation</SelectItem>
                        <SelectItem value="interquartile">Interquartile</SelectItem>
                        <SelectItem value="gradient">Gradient</SelectItem>
                      </SelectContent>
                    </Select>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Breakpoint detection method
                    </p>
                  </div>
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Semantic Threshold
                    </label>
                    <Input
                      type="number"
                      step="0.01"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="0.95"
                      value={semanticThreshold}
                      onChange={(e) => setSemanticThreshold(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Threshold for detecting breakpoints
                    </p>
                  </div>
                </div>
              </div>

              {/* Regex chunker settings */}
              <div className="border border-gray-200 dark:border-[#3D3D3D] rounded-lg p-4">
                <h3 className="text-sm font-medium mb-3 text-black dark:text-white">
                  Regex Chunker
                </h3>
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Regex Pattern
                  </label>
                  <Input
                    type="text"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="\r?\n"
                    value={regexPattern}
                    onChange={(e) => setRegexPattern(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Regular expression pattern to split on
                  </p>
                </div>
              </div>
            </div>
            )}
          </div>

          {message && (
            <div
              className={`p-4 rounded-lg ${
                messageType === "success"
                  ? "bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-200"
                  : "bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-200"
              }`}
            >
              {message}
            </div>
          )}

          {/* Advanced Ingestion Settings */}
          <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
            <button
              type="button"
              onClick={() => setShowAdvanced(!showAdvanced)}
              className="w-full flex items-center justify-between"
            >
              <h2 className="text-lg font-semibold text-black dark:text-white">
                Advanced Ingestion Settings
              </h2>
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {showAdvanced ? "▲ Collapse" : "▼ Expand"}
              </span>
            </button>
            {!showAdvanced && (
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mt-2">
                Performance tuning for document ingestion and batch processing.
              </p>
            )}

            {showAdvanced && (
              <div className="space-y-4 mt-4">
                <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-2">
                  Performance tuning for document ingestion and batch processing.
                </p>

                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Batch Size
                    </label>
                    <Input
                      type="number"
                      min="1"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="500"
                      value={loadBatchSize}
                      onChange={(e) => setLoadBatchSize(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Number of vertices written per batch.
                    </p>
                  </div>

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Upsert Delay
                    </label>
                    <Input
                      type="number"
                      min="0"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="0"
                      value={upsertDelay}
                      onChange={(e) => setUpsertDelay(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Seconds between batches
                    </p>
                  </div>

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Default Concurrency
                    </label>
                    <Input
                      type="number"
                      min="1"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="10"
                      value={maxConcurrency}
                      onChange={(e) => setMaxConcurrency(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Max concurrent workers for graph queries, LLM, and embedding calls
                    </p>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Schema extraction (sample-doc proposal path) + dynamic schema
              behavior (how domain types are enforced and retrieved). */}
          <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
            <button
              type="button"
              onClick={() => setShowSchema(!showSchema)}
              className="w-full flex items-center justify-between"
            >
              <h2 className="text-lg font-semibold text-black dark:text-white">
                Schema Extraction
              </h2>
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {showSchema ? "▲ Collapse" : "▼ Expand"}
              </span>
            </button>
            {!showSchema && (
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mt-2">
                Sample-doc schema extraction limits and runtime behavior of
                the domain schema.
              </p>
            )}

            {showSchema && (
            <div className="mt-4">
            <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-6">
              Controls for the schema-extraction sample-doc workflow and the
              runtime behavior of the dynamic (domain) schema during entity
              extraction and retrieval.
            </p>

            {/* Schema extraction sub-section */}
            <div className="border border-gray-200 dark:border-[#3D3D3D] rounded-lg p-4 mb-4">
              <h3 className="text-sm font-medium mb-3 text-black dark:text-white">
                Schema Extraction (Sample Documents)
              </h3>
              <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
                Limits for the <em>Generate from sample documents</em> path on
                the <em>Initialize Knowledge Graph</em> dialog.
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Max Sample Files
                  </label>
                  <Input
                    type="number"
                    min="1"
                    max="20"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="5"
                    value={schemaMaxSampleFiles}
                    onChange={(e) => setSchemaMaxSampleFiles(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Maximum number of sample documents per schema-extraction run
                  </p>
                </div>
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Max Total Size (MB)
                  </label>
                  <Input
                    type="number"
                    min="1"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="50"
                    value={schemaMaxTotalMb}
                    onChange={(e) => setSchemaMaxTotalMb(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Combined upload cap across all sample files
                  </p>
                </div>
              </div>
            </div>

            {/* Dynamic schema runtime sub-section */}
            <div className="border border-gray-200 dark:border-[#3D3D3D] rounded-lg p-4">
              <h3 className="text-sm font-medium mb-3 text-black dark:text-white">
                Dynamic Schema Runtime
              </h3>
              <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
                How the domain schema is enforced during entity extraction and
                used by retrievers at query time.
              </p>

              <div className="space-y-4">
                <div>
                  <div className="flex items-center space-x-2">
                    <input
                      type="checkbox"
                      id="strictMode"
                      className="rounded border-gray-300 dark:border-[#3D3D3D]"
                      checked={strictMode}
                      onChange={(e) => setStrictMode(e.target.checked)}
                    />
                    <label htmlFor="strictMode" className="text-sm font-medium text-black dark:text-white">
                      Strict Mode
                    </label>
                  </div>
                  <p className="text-xs text-gray-600 dark:text-[#D9D9D9] mt-1 ml-6">
                    Drop extracted entities and relationships that don't match
                    the domain schema.
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Include Generic Entity in Retrieval
                  </label>
                  <Select
                    value={retrievalIncludeEntity}
                    onValueChange={(v) =>
                      setRetrievalIncludeEntity(v as "auto" | "true" | "false")
                    }
                  >
                    <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">Auto (default)</SelectItem>
                      <SelectItem value="true">Yes – always include Entity</SelectItem>
                      <SelectItem value="false">No – domain types only</SelectItem>
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Include generic entities in retrieval alongside domain types.
                  </p>
                </div>
              </div>
            </div>
            </div>
            )}
          </div>

          {/* Service Endpoints (global only) */}
          {configScope !== "graph" && (
            <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
              <button
                type="button"
                onClick={() => setShowEndpoints(!showEndpoints)}
                className="w-full flex items-center justify-between"
              >
                <h2 className="text-lg font-semibold text-black dark:text-white">
                  Service Endpoints
                </h2>
                <span className="text-sm text-gray-500 dark:text-gray-400">
                  {showEndpoints ? "▲ Collapse" : "▼ Expand"}
                </span>
              </button>
              {!showEndpoints && (
                <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mt-2">
                  Internal service URLs (global only).
                </p>
              )}

              {showEndpoints && (
              <div className="space-y-4 mt-4">
                <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-2">
                  Configure internal service URLs. These are global settings and cannot be overridden per graph.
                </p>
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    ECC Service URL
                  </label>
                  <Input
                    type="text"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="http://graphrag-ecc:8001"
                    value={eccUrl}
                    onChange={(e) => setEccUrl(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Entity-Context-Community service endpoint
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Chat History API URL
                  </label>
                  <Input
                    type="text"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="http://chat-history:8002"
                    value={chatHistoryUrl}
                    onChange={(e) => setChatHistoryUrl(e.target.value)}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Chat history service endpoint
                  </p>
                </div>
              </div>
              )}
            </div>
          )}

          <Button onClick={handleSave} disabled={isSaving} className="gradient text-white w-full">
            {isSaving ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Saving...
              </>
            ) : (
              <>
                <Save className="h-4 w-4 mr-2" />
                Save GraphRAG Configuration
              </>
            )}
          </Button>
        </div>
        </fieldset>
      </div>
    </div>
  );
};

export default GraphRAGConfig;

