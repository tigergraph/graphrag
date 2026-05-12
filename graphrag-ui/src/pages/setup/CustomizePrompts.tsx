import React, { useState, useEffect } from "react";
import { FileText, Save, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import ConfigScopeToggle from "@/components/ConfigScopeToggle";
import { useRoles } from "@/hooks/useRoles";
import { useLocation } from "react-router-dom";

// Ordered to follow the lifecycle of a graph: setup → ingest → rebuild
// → query. The Customize Prompts page lists them in the same order so
// admins read them top-down in the order they fire.
//
// ``query_generation`` (map_question_to_schema) is intentionally not
// listed here — Query Guidance now covers its only end-user-facing
// customization need (domain hints + examples). The underlying prompt
// is still available on disk and editable via direct API for advanced
// use cases.
const ALL_PROMPT_TYPES = [
  { id: "schema_extraction", name: "Schema Extraction", description: "Rules the LLM follows when proposing a domain schema from sample documents (Initialize Graph dialog)." },
  { id: "entity_relationship", name: "Entity Relationships", description: "Extract entities and relationships from document chunks during ingest." },
  { id: "community_summarization", name: "Community Summarization", description: "Summarize each community after Louvain detection during rebuild." },
  { id: "query_guidance", name: "Query Guidance", description: "Free-form domain hints and example mappings — injected into question-to-schema, generate-function, generate-cypher, and generate-gsql prompts. Empty by default. Max 8000 characters." },
  { id: "chatbot_response", name: "Chatbot Responses", description: "How the chatbot composes the final answer to the user from retrieved context." },
];

const CustomizePrompts = () => {
  const location = useLocation();
  const { isSuperuser, isGlobalDesigner } = useRoles(location.pathname);
  const graphOnly = !isSuperuser && !isGlobalDesigner;
  const [isLoading, setIsLoading] = useState(true);
  const [expandedPrompt, setExpandedPrompt] = useState<string | null>(null);
  // Only the prompt types returned by the backend (filtered by access level)
  const [availablePromptIds, setAvailablePromptIds] = useState<string[]>([]);
  
  // Prompts loaded from backend (editable content only)
  const [prompts, setPrompts] = useState({
    chatbot_response: "",
    entity_relationship: "",
    community_summarization: "",
    query_generation: "",
    schema_extraction: "",
    query_guidance: "",
  });

  // Template variables that should not be edited (stored separately)
  const [promptTemplates, setPromptTemplates] = useState({
    chatbot_response: "",
    entity_relationship: "",
    community_summarization: "",
    query_generation: "",
    schema_extraction: "",
    query_guidance: "",
  });

  // Only render prompt types the backend returned for this user
  const promptTypes = ALL_PROMPT_TYPES.filter(p => availablePromptIds.includes(p.id));

  const [isSaving, setIsSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [saveMessageType, setSaveMessageType] = useState<"success" | "error" | "">("");
  const [configScope, setConfigScope] = useState<"global" | "graph">("global");
  const [selectedGraph, setSelectedGraph] = useState(sessionStorage.getItem("selectedGraph") || "");
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);

  const handleSavePrompt = async (promptId: string) => {
    setIsSaving(true);
    setSaveMessage("");
    setSaveMessageType("");

    try {
      const creds = sessionStorage.getItem("creds");
      const query = selectedGraph ? `?graphname=${encodeURIComponent(selectedGraph)}` : "";
      const response = await fetch(`/ui/prompts${query}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify({
          prompt_type: promptId,
          editable_content: prompts[promptId as keyof typeof prompts],
          template_variables: promptTemplates[promptId as keyof typeof promptTemplates],
          graphname: selectedGraph || undefined,
        }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Failed to save prompt");
      }

      const result = await response.json();
      setSaveMessage(`✅ ${result.message}`);
      setSaveMessageType("success");
      setExpandedPrompt(null); // Collapse after successful save
    } catch (error: any) {
      console.error("Error saving prompt:", error);
      setSaveMessage(`❌ Error: ${error.message}`);
      setSaveMessageType("error");
    } finally {
      setIsSaving(false);
    }
  };

  const handlePromptChange = (promptId: string, value: string) => {
    setPrompts(prev => ({ ...prev, [promptId]: value }));
  };

  const fetchPrompts = async (graphname?: string) => {
    setIsLoading(true);
    const effectiveGraph = graphname ?? selectedGraph;
    try {
      const creds = sessionStorage.getItem("creds");
      const query = effectiveGraph ? `?graphname=${encodeURIComponent(effectiveGraph)}` : "";
      const response = await fetch(`/ui/prompts${query}`, {
        headers: { Authorization: `Basic ${creds}` },
      });

      if (!response.ok) {
        throw new Error("Failed to fetch prompts");
      }

      const data = await response.json();

      // Track which prompts this user is allowed to see (backend filters by role)
      setAvailablePromptIds(Object.keys(data.prompts));

      // Update prompts with fetched data (editable content only)
      setPrompts({
        chatbot_response: data.prompts.chatbot_response?.editable_content !== undefined
          ? data.prompts.chatbot_response.editable_content
          : (typeof data.prompts.chatbot_response === 'string' ? data.prompts.chatbot_response : ""),
        entity_relationship: data.prompts.entity_relationship?.editable_content !== undefined
          ? data.prompts.entity_relationship.editable_content
          : (typeof data.prompts.entity_relationship === 'string' ? data.prompts.entity_relationship : ""),
        community_summarization: data.prompts.community_summarization?.editable_content !== undefined
          ? data.prompts.community_summarization.editable_content
          : (typeof data.prompts.community_summarization === 'string' ? data.prompts.community_summarization : ""),
        query_generation: data.prompts.query_generation?.editable_content !== undefined
          ? data.prompts.query_generation.editable_content
          : (typeof data.prompts.query_generation === 'string' ? data.prompts.query_generation : ""),
        schema_extraction: data.prompts.schema_extraction?.editable_content !== undefined
          ? data.prompts.schema_extraction.editable_content
          : (typeof data.prompts.schema_extraction === 'string' ? data.prompts.schema_extraction : ""),
        query_guidance: data.prompts.query_guidance?.editable_content !== undefined
          ? data.prompts.query_guidance.editable_content
          : (typeof data.prompts.query_guidance === 'string' ? data.prompts.query_guidance : ""),
      });

      // Store template variables separately
      setPromptTemplates({
        chatbot_response: data.prompts.chatbot_response?.template_variables || "",
        entity_relationship: data.prompts.entity_relationship?.template_variables || "",
        community_summarization: data.prompts.community_summarization?.template_variables || "",
        query_generation: data.prompts.query_generation?.template_variables || "",
        schema_extraction: data.prompts.schema_extraction?.template_variables || "",
        query_guidance: data.prompts.query_guidance?.template_variables || "",
      });
    } catch (error) {
      console.error("Error loading prompts:", error);
    } finally {
      setIsLoading(false);
    }
  };

  // Fetch prompts and graph list on mount
  useEffect(() => {
    const site = JSON.parse(sessionStorage.getItem("site") || "{}");
    const graphs = site.graphs || [];
    setAvailableGraphs(graphs);
    const storedGraph = sessionStorage.getItem("selectedGraph") || "";
    if (graphOnly) {
      // Graph admins must use graph-specific scope
      setConfigScope("graph");
      const graph = storedGraph || (graphs.length > 0 ? graphs[0] : "");
      if (graph) {
        setSelectedGraph(graph);
        sessionStorage.setItem("selectedGraph", graph);
        window.dispatchEvent(new Event("graphrag:selectedGraph"));
        fetchPrompts(graph);
      }
    } else if (storedGraph) {
      setConfigScope("graph");
      setSelectedGraph(storedGraph);
      fetchPrompts(storedGraph);
    } else {
      fetchPrompts("");
    }
  }, [graphOnly]);

  // Stay in sync when another component (Bot, Refresh dialog,
  // Ingest dialog) changes the shared selectedGraph. The prompts
  // are scoped per graph, so a change triggers a re-fetch too.
  useEffect(() => {
    const handler = () => {
      const next = sessionStorage.getItem("selectedGraph") || "";
      if (next === selectedGraph) return;
      setSelectedGraph(next);
      if (next) {
        setConfigScope("graph");
        fetchPrompts(next);
      } else if (!graphOnly) {
        setConfigScope("global");
        fetchPrompts("");
      }
    };
    window.addEventListener("graphrag:selectedGraph", handler);
    return () => window.removeEventListener("graphrag:selectedGraph", handler);
  }, [selectedGraph, graphOnly]);

  return (
    <div className="p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center">
              <FileText className="h-6 w-6 text-tigerOrange" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-black dark:text-white">
                Customize Prompts
              </h1>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                Customize the core prompts used by GraphRAG
              </p>
            </div>
          </div>
        </div>

        {/* Config Scope Toggle */}
        <ConfigScopeToggle
          configScope={configScope}
          selectedGraph={selectedGraph}
          availableGraphs={availableGraphs}
          graphOnly={graphOnly}
          onScopeChange={(scope) => {
            setConfigScope(scope);
            setSaveMessage("");
            setSaveMessageType("");
            if (scope === "global") {
              setSelectedGraph("");
              sessionStorage.removeItem("selectedGraph");
              window.dispatchEvent(new Event("graphrag:selectedGraph"));
              fetchPrompts("");
            } else if (selectedGraph) {
              fetchPrompts(selectedGraph);
            }
          }}
          onGraphChange={(value) => {
            setConfigScope("graph");
            setSelectedGraph(value);
            sessionStorage.setItem("selectedGraph", value);
            window.dispatchEvent(new Event("graphrag:selectedGraph"));
            setSaveMessage("");
            setSaveMessageType("");
            fetchPrompts(value);
          }}
          graphSelectedHint="Only customized prompts are stored per graph. Others fall back to global defaults."
        />

        <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
          <div className="space-y-6">
            {/* Save Message */}
            {saveMessage && (
              <div
                className={`p-4 rounded-lg text-sm ${
                  saveMessageType === "success"
                    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                    : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                }`}
              >
                {saveMessage}
              </div>
            )}

            {/* Prompt Templates */}
            <div className="mt-6 space-y-4">
              <h3 className="text-lg font-semibold text-black dark:text-white">
                Prompt Templates
              </h3>
              
              <div className="space-y-4">
                {promptTypes.map((prompt) => (
                  <div
                    key={prompt.id}
                    className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-4"
                  >
                    <div className="flex items-start justify-between mb-3">
                      <div>
                        <h4 className="font-medium text-black dark:text-white mb-1">
                          {prompt.name}
                        </h4>
                        <p className="text-sm text-gray-600 dark:text-gray-400">
                          {prompt.description}
                        </p>
                      </div>
                      <Button
                        onClick={() => setExpandedPrompt(expandedPrompt === prompt.id ? null : prompt.id)}
                        variant="outline"
                        size="sm"
                        className="dark:border-[#3D3D3D]"
                      >
                        {expandedPrompt === prompt.id ? "Collapse" : "Edit"}
                      </Button>
                    </div>
                    
                    {expandedPrompt === prompt.id && (
                      <div className="mt-4 space-y-3">
                        <textarea
                          value={prompts[prompt.id as keyof typeof prompts]}
                          onChange={(e) => handlePromptChange(prompt.id, e.target.value)}
                          rows={15}
                          className="w-full p-3 rounded border dark:border-[#3D3D3D] dark:bg-background text-sm font-mono"
                          placeholder="Enter your prompt template here..."
                        />
                        <div className="flex gap-2">
                          <Button
                            onClick={() => handleSavePrompt(prompt.id)}
                            disabled={isSaving}
                            className="gradient text-white"
                            size="sm"
                          >
                            {isSaving ? (
                              <>
                                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                Saving...
                              </>
                            ) : (
                              <>
                                <Save className="h-4 w-4 mr-2" />
                                Save Prompt
                              </>
                            )}
                          </Button>
                          <Button
                            onClick={() => setExpandedPrompt(null)}
                            variant="outline"
                            size="sm"
                            className="dark:border-[#3D3D3D]"
                          >
                            Cancel
                          </Button>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>

          </div>
        </div>
      </div>
    </div>
  );
};

export default CustomizePrompts;

