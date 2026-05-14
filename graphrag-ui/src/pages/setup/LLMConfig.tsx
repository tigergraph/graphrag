import React, { useState, useEffect } from "react";
import { Server, CheckCircle2, Save, Loader2 } from "lucide-react";
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

// Type definitions for provider fields
interface FieldConfig {
  key: string;
  label: string;
  type: string;
  required?: boolean;
  placeholder?: string;
}

interface ProviderConfig {
  authFields: FieldConfig[];
  configFields: FieldConfig[];
  note?: string;
}

// Provider-specific field configuration
const PROVIDER_FIELDS: Record<string, ProviderConfig> = {
  openai: {
    authFields: [
      { key: "OPENAI_API_KEY", label: "API Key", type: "password", required: true }
    ],
    configFields: []
  },
  azure: {
    authFields: [
      { key: "AZURE_OPENAI_API_KEY", label: "Azure OpenAI API Key", type: "password", required: true },
      { key: "AZURE_OPENAI_ENDPOINT", label: "Azure Endpoint", type: "text", required: true, placeholder: "https://your-resource.openai.azure.com/" },
      { key: "OPENAI_API_VERSION", label: "API Version", type: "text", required: true, placeholder: "2024-02-15-preview" }
    ],
    configFields: [
      { key: "azure_deployment", label: "Azure Deployment Name", type: "text", required: true, placeholder: "gpt-4" }
    ]
  },
  genai: {
    authFields: [
      { key: "GOOGLE_API_KEY", label: "Google API Key", type: "password", required: true }
    ],
    configFields: []
  },
  vertexai: {
    authFields: [],
    configFields: [
      { key: "project", label: "GCP Project ID (Optional)", type: "text", placeholder: "my-project-id" },
      { key: "location", label: "Location (Optional)", type: "text", placeholder: "us-central1" }
    ],
    note: "VertexAI uses service account credentials from GOOGLE_APPLICATION_CREDENTIALS environment variable"
  },
  bedrock: {
    authFields: [
      { key: "AWS_ACCESS_KEY_ID", label: "AWS Access Key ID", type: "password", required: true },
      { key: "AWS_SECRET_ACCESS_KEY", label: "AWS Secret Access Key", type: "password", required: true }
    ],
    configFields: [
      { key: "region_name", label: "AWS Region", type: "text", required: false, placeholder: "us-east-1" }
    ]
  },
  groq: {
    authFields: [
      { key: "GROQ_API_KEY", label: "Groq API Key", type: "password", required: true }
    ],
    configFields: []
  },
  ollama: {
    authFields: [],
    configFields: [
      { key: "base_url", label: "Ollama Base URL", type: "text", required: true, placeholder: "http://localhost:11434" }
    ]
  },
  sagemaker: {
    authFields: [
      { key: "AWS_ACCESS_KEY_ID", label: "AWS Access Key ID", type: "password", required: true },
      { key: "AWS_SECRET_ACCESS_KEY", label: "AWS Secret Access Key", type: "password", required: true }
    ],
    configFields: [
      { key: "region_name", label: "AWS Region", type: "text", required: true, placeholder: "us-east-1" },
      { key: "endpoint_name", label: "SageMaker Endpoint Name", type: "text", required: true }
    ]
  },
  huggingface: {
    authFields: [
      { key: "HUGGINGFACEHUB_API_TOKEN", label: "HuggingFace API Token", type: "password", required: true }
    ],
    configFields: [
      { key: "endpoint_url", label: "Endpoint URL (Optional)", type: "text", placeholder: "https://api-inference.huggingface.co/models/..." }
    ]
  },
  watsonx: {
    authFields: [
      { key: "WATSONX_API_KEY", label: "IBM WatsonX API Key", type: "password", required: true },
      { key: "WATSONX_URL", label: "WatsonX URL", type: "text", required: true, placeholder: "https://us-south.ml.cloud.ibm.com" },
      { key: "WATSONX_PROJECT_ID", label: "Project ID", type: "text", required: true }
    ],
    configFields: []
  }
};

// Single provider list shared across all service Select dropdowns
const LLM_PROVIDERS = [
  { value: "openai", label: "OpenAI" },
  { value: "azure", label: "Azure OpenAI" },
  { value: "genai", label: "Google GenAI (Gemini)" },
  { value: "vertexai", label: "Google Vertex AI" },
  { value: "bedrock", label: "AWS Bedrock" },
  { value: "groq", label: "Groq" },
  { value: "ollama", label: "Ollama" },
  { value: "sagemaker", label: "AWS SageMaker" },
  { value: "huggingface", label: "HuggingFace" },
  { value: "watsonx", label: "IBM WatsonX" },
] as const;

const LLMConfig = () => {
  const [selectedGraph, setSelectedGraph] = useState(sessionStorage.getItem("selectedGraph") || "");
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);
  const [useMultipleProviders, setUseMultipleProviders] = useState(false);
  const [llmConfigAccess, setLlmConfigAccess] = useState<"full" | "chatbot_only">("full");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState<"success" | "error" | "">("");
  const [testResults, setTestResults] = useState<any>(null);
  const [connectionTested, setConnectionTested] = useState(false);

  const [useCustomMultimodal, setUseCustomMultimodal] = useState(false);

  // Canonical per-service state — both single and multi-provider UIs read/write these
  const [completionProvider, setCompletionProvider] = useState("openai");
  const [completionConfig, setCompletionConfig] = useState<Record<string, string>>({});
  const [completionDefaultModel, setCompletionDefaultModel] = useState("");
  
  const [embeddingProvider, setEmbeddingProvider] = useState("openai");
  const [embeddingConfig, setEmbeddingConfig] = useState<Record<string, string>>({});
  const [embeddingModel, setEmbeddingModel] = useState("");
  
  const [multimodalProvider, setMultimodalProvider] = useState("openai");
  const [multimodalConfig, setMultimodalConfig] = useState<Record<string, string>>({});
  const [multimodalModelName, setMultimodalModelName] = useState("");
  const isChatbotOnlyMode = llmConfigAccess === "chatbot_only";

  // Per-graph chatbot config state (chatbot_only mode)
  const [useCustomChatbot, setUseCustomChatbot] = useState(false);
  const [chatbotProvider, setChatbotProvider] = useState("openai");
  const [chatbotProviderConfig, setChatbotProviderConfig] = useState<Record<string, string>>({});
  const [chatbotModelName, setChatbotModelName] = useState("");
  const [chatbotTemperature, setChatbotTemperature] = useState("0");
  const [globalChatInfo, setGlobalChatInfo] = useState({ llm_service: "", llm_model: "" });

  // Superadmin scope: "global" edits global config, "graph" edits per-graph overrides
  const [configScope, setConfigScope] = useState<"global" | "graph">("global");
  const [graphOverrides, setGraphOverrides] = useState<Record<string, any>>({});


  // Load available graphs and config on mount
  useEffect(() => {
    const site = JSON.parse(sessionStorage.getItem("site") || "{}");
    setAvailableGraphs(site.graphs || []);
    fetchConfig();
  }, []);

  const fetchConfig = async (scope?: "global" | "graph", graphname?: string) => {
    setIsLoading(true);
    const effectiveScope = scope ?? configScope;
    const effectiveGraph = graphname ?? selectedGraph;
    try {
      const creds = sessionStorage.getItem("creds");
      const params = new URLSearchParams();
      if (effectiveGraph) params.set("graphname", effectiveGraph);
      if (effectiveScope === "graph") params.set("scope", "graph");
      const queryString = params.toString() ? `?${params.toString()}` : "";
      const response = await fetch(`/ui/config${queryString}`, {
        headers: { Authorization: `Basic ${creds}` },
      });

      if (!response.ok) {
        throw new Error("Failed to fetch configuration");
      }

      const data = await response.json();
      const llmConfig = data.llm_config;
      setLlmConfigAccess(data.llm_config_access === "chatbot_only" ? "chatbot_only" : "full");

      // Store graph overrides when in per-graph scope
      if (data.graph_overrides) {
        setGraphOverrides(data.graph_overrides);
      } else {
        setGraphOverrides({});
      }

      // Detect providers (needed by chat/multimodal fallback below)
      const completionProv = llmConfig.completion_service?.llm_service?.toLowerCase();
      const embeddingProv = llmConfig.embedding_service?.embedding_model_service?.toLowerCase();
      const multimodalProv = llmConfig.multimodal_service?.llm_service?.toLowerCase();
      const chatProv = llmConfig.chat_service?.llm_service?.toLowerCase();
      const defaultProv = completionProv || "openai";

      // All config field keys that any provider might use
      const allConfigKeys = ["base_url", "azure_deployment", "region_name", "project", "location", "endpoint_name", "endpoint_url"];

      // Build the base config: top-level auth + completion_service fields.
      // Every service inherits missing keys from this base.
      const baseConfig: Record<string, string> = {};
      // Layer 1: top-level auth
      if (llmConfig.authentication_configuration) {
        for (const [key, value] of Object.entries(llmConfig.authentication_configuration)) {
          if (typeof value === "string") baseConfig[key] = value;
        }
      }
      // Layer 2: completion_service config fields + auth
      if (llmConfig.completion_service) {
        for (const key of allConfigKeys) {
          if (llmConfig.completion_service[key]) baseConfig[key] = llmConfig.completion_service[key];
        }
        if (llmConfig.completion_service.authentication_configuration) {
          for (const [key, value] of Object.entries(llmConfig.completion_service.authentication_configuration)) {
            if (typeof value === "string") baseConfig[key] = value;
          }
        }
      }

      // Helper: load a service config, inheriting all missing keys from baseConfig
      const loadServiceConfigResolved = (svc: any) => {
        // Start with base config as defaults
        const cfg: Record<string, string> = { ...baseConfig };
        // Override with service-specific config fields
        if (svc) {
          for (const key of allConfigKeys) {
            if (svc[key]) cfg[key] = svc[key];
          }
          // Override with service-specific auth
          if (svc.authentication_configuration) {
            for (const [key, value] of Object.entries(svc.authentication_configuration)) {
              if (typeof value === "string") cfg[key] = value;
            }
          }
        }
        return cfg;
      };

      // Parse per-graph chatbot config (chatbot_only mode)
      if (data.global_chat_info) {
        setGlobalChatInfo(data.global_chat_info);
      }
      if (data.chatbot_config) {
        setUseCustomChatbot(true);
        setChatbotProvider(data.chatbot_config.llm_service?.toLowerCase() || defaultProv);
        setChatbotModelName(data.chatbot_config.llm_model || "");
        setChatbotTemperature(String(data.chatbot_config.model_kwargs?.temperature ?? "0"));
        // Resolve chatbot config: base config + chatbot overrides
        setChatbotProviderConfig(loadServiceConfigResolved(data.chatbot_config));
      } else {
        setUseCustomChatbot(false);
      }

      const currentDefaultModel = llmConfig.completion_service?.llm_model || "";
      setCompletionDefaultModel(currentDefaultModel);

      const allSameProvider =
        completionProv === embeddingProv &&
        (!multimodalProv || completionProv === multimodalProv) &&
        (!chatProv || completionProv === chatProv);

      setUseMultipleProviders(!allSameProvider);

      // Load chat_service config for full mode (superadmin)
      // Chat inherits from base (completion) when not explicitly set
      if (llmConfig.chat_service) {
        setUseCustomChatbot(true);
        setChatbotProvider(chatProv || defaultProv);
        setChatbotModelName(llmConfig.chat_service.llm_model || "");
        setChatbotTemperature(String(llmConfig.chat_service.model_kwargs?.temperature ?? "0"));
        setChatbotProviderConfig(loadServiceConfigResolved(llmConfig.chat_service));
      } else {
        setUseCustomChatbot(false);
        setChatbotProvider(defaultProv);
        setChatbotModelName("");
        setChatbotTemperature("0");
        setChatbotProviderConfig({ ...baseConfig });
      }

      // Canonical per-service state — both single and multi-provider UIs read these
      setCompletionProvider(completionProv || "openai");
      setCompletionDefaultModel(llmConfig.completion_service?.llm_model || "");
      setCompletionConfig(loadServiceConfigResolved(llmConfig.completion_service));

      setEmbeddingProvider(embeddingProv || completionProv || "openai");
      setEmbeddingModel(llmConfig.embedding_service?.model_name || "");
      setEmbeddingConfig(loadServiceConfigResolved(llmConfig.embedding_service));

      setMultimodalProvider(multimodalProv || completionProv || "openai");
      const mmModel = llmConfig.multimodal_service?.llm_model || "";
      setMultimodalModelName(mmModel);
      setMultimodalConfig(loadServiceConfigResolved(llmConfig.multimodal_service));
      setUseCustomMultimodal(!!mmModel || !!multimodalProv);
    } catch (error: any) {
      console.error("Error fetching config:", error);
      setMessage(`Failed to load configuration: ${error.message}`);
      setMessageType("error");
    } finally {
      setIsLoading(false);
    }
  };

  const clearTestResults = () => {
    setConnectionTested(false);
    setTestResults(null);
    setMessage("");
    setMessageType("");
  };

  // Update config when provider changes - CLEAR ALL FIELDS
  const handleProviderChange = (newProvider: string, target: 'completion' | 'embedding' | 'multimodal') => {
    if (target === 'completion') {
      setCompletionProvider(newProvider);
      setCompletionConfig({});
      setCompletionDefaultModel("");
      // In single-provider mode, all services share the same provider
      if (!useMultipleProviders) {
        setEmbeddingProvider(newProvider);
        setEmbeddingConfig({});
        setEmbeddingModel("");
        setMultimodalProvider(newProvider);
        setMultimodalConfig({});
        setMultimodalModelName("");
      }
    } else if (target === 'embedding') {
      setEmbeddingProvider(newProvider);
      setEmbeddingConfig({});
      // Clear model name when switching provider
      setEmbeddingModel("");
    } else if (target === 'multimodal') {
      setMultimodalProvider(newProvider);
      setMultimodalConfig({});
      // Clear model name when switching provider
      setMultimodalModelName("");
    }
    clearTestResults();
  };

  const buildAuthConfig = (provider: string, config: Record<string, string>) => {
    const authConfig: Record<string, string> = {};
    const providerFields = PROVIDER_FIELDS[provider as keyof typeof PROVIDER_FIELDS];
    if (!providerFields) return authConfig;
    
    providerFields.authFields.forEach(field => {
      if (config[field.key]) {
        authConfig[field.key] = config[field.key];
      }
    });
    
    return authConfig;
  };

  const buildServiceConfig = (provider: string, config: Record<string, string>) => {
    const serviceConfig: Record<string, any> = {};
    const providerFields = PROVIDER_FIELDS[provider as keyof typeof PROVIDER_FIELDS];
    if (!providerFields) return serviceConfig;
    
    providerFields.configFields.forEach(field => {
      if (config[field.key]) {
        serviceConfig[field.key] = config[field.key];
      }
    });
    
    return serviceConfig;
  };

  /**
   * Build the candidate LLM config payload.
   * Used by both test-connection and save — same structure, single source of truth.
   * Inherited services (multimodal, chatbot) are set to null when not customized.
   */
  const buildLLMConfigPayload = (): any => {
    let llmConfigData: any;

    if (useMultipleProviders) {
      const completionServiceConfig: any = {
        llm_service: completionProvider,
        llm_model: completionDefaultModel,
        authentication_configuration: buildAuthConfig(completionProvider, completionConfig),
        model_kwargs: { temperature: 0 },
        prompt_path: `./common/prompts/${getPromptPath(completionProvider)}/`,
        ...buildServiceConfig(completionProvider, completionConfig)
      };

      llmConfigData = {
        graphname: selectedGraph || undefined,
        completion_service: completionServiceConfig,
        embedding_service: {
          embedding_model_service: embeddingProvider,
          model_name: embeddingModel,
          authentication_configuration: buildAuthConfig(embeddingProvider, embeddingConfig),
          ...buildServiceConfig(embeddingProvider, embeddingConfig)
        },
      };

      if (useCustomMultimodal && multimodalModelName) {
        llmConfigData.multimodal_service = {
          llm_service: multimodalProvider,
          llm_model: multimodalModelName,
          authentication_configuration: buildAuthConfig(multimodalProvider, multimodalConfig),
          model_kwargs: { temperature: 0 },
          ...buildServiceConfig(multimodalProvider, multimodalConfig)
        };
      } else {
        llmConfigData.multimodal_service = null;
      }

      if (useCustomChatbot) {
        llmConfigData.chat_service = {
          llm_service: chatbotProvider,
          llm_model: chatbotModelName,
          authentication_configuration: buildAuthConfig(chatbotProvider, chatbotProviderConfig),
          model_kwargs: { temperature: parseFloat(chatbotTemperature) || 0 },
          ...buildServiceConfig(chatbotProvider, chatbotProviderConfig),
        };
      } else {
        llmConfigData.chat_service = null;
      }
    } else {
      const completionServiceConfig: any = {
        llm_service: completionProvider,
        llm_model: completionDefaultModel,
        model_kwargs: { temperature: 0 },
        prompt_path: `./common/prompts/${getPromptPath(completionProvider)}/`,
        ...buildServiceConfig(completionProvider, completionConfig)
      };

      llmConfigData = {
        graphname: selectedGraph || undefined,
        authentication_configuration: buildAuthConfig(completionProvider, completionConfig),
        completion_service: completionServiceConfig,
        embedding_service: {
          embedding_model_service: completionProvider,
          model_name: embeddingModel,
        },
      };

      if (useCustomMultimodal && multimodalModelName.trim()) {
        llmConfigData.multimodal_service = {
          llm_model: multimodalModelName,
        };
      } else {
        llmConfigData.multimodal_service = null;
      }

      if (useCustomChatbot) {
        const chatTemp = parseFloat(chatbotTemperature) || 0;
        llmConfigData.chat_service = {
          ...(chatbotModelName.trim() ? { llm_model: chatbotModelName } : {}),
          model_kwargs: { temperature: chatTemp },
        };
      } else {
        llmConfigData.chat_service = null;
      }
    }

    if (configScope === "graph") {
      llmConfigData.scope = "graph";
    }

    return llmConfigData;
  };

  const handleSave = async () => {
    setIsSaving(true);
    setMessage("");
    setMessageType("");

    try {
      const creds = sessionStorage.getItem("creds");
      let llmConfigData: any;

      // Graph admin saving chatbot config
      if (isChatbotOnlyMode) {
        if (useCustomChatbot) {
          const chatService: any = {
            llm_service: chatbotProvider,
            llm_model: chatbotModelName,
            authentication_configuration: buildAuthConfig(chatbotProvider, chatbotProviderConfig),
            model_kwargs: { temperature: parseFloat(chatbotTemperature) || 0 },
            ...buildServiceConfig(chatbotProvider, chatbotProviderConfig),
          };
          llmConfigData = { graphname: selectedGraph || undefined, chat_service: chatService };
        } else {
          // Revert to inherit: send null chat_service
          llmConfigData = { graphname: selectedGraph || undefined, chat_service: null };
        }

        const response = await fetch("/ui/config/llm", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Basic ${creds}`,
          },
          body: JSON.stringify(llmConfigData),
        });

        if (!response.ok) {
          const errorData = await response.json();
          throw new Error(errorData.detail || "Failed to save configuration");
        }

        setMessage("Configuration saved successfully!");
        setMessageType("success");
        setTestResults(null);
        setConnectionTested(false);
        setIsSaving(false);
        return;
      }

      llmConfigData = buildLLMConfigPayload();

      const response = await fetch("/ui/config/llm", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(llmConfigData),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Failed to save configuration");
      }

      const scopeLabel = configScope === "graph" ? `graph "${selectedGraph}"` : "global";
      setMessage(`Configuration saved successfully (${scopeLabel})!`);
      setMessageType("success");
      setTestResults(null);
      setConnectionTested(false);

      // Refetch to sync all state with the saved config
      fetchConfig(configScope === "graph" ? "graph" : "global", selectedGraph || undefined);
    } catch (error: any) {
      console.error("Error saving config:", error);
      setMessage(`❌ Error: ${error.message}`);
      setMessageType("error");
    } finally {
      setIsSaving(false);
    }
  };

  const handleTestConnection = async () => {
    setIsTesting(true);
    setTestResults(null);
    setMessage("");
    setMessageType("");

    try {
      // Frontend validation
      const validateProvider = (provider: string, config: Record<string, string>, serviceName: string) => {
        const providerFields = PROVIDER_FIELDS[provider as keyof typeof PROVIDER_FIELDS];
        if (!providerFields) return null;
        
        for (const field of providerFields.authFields) {
          if (field.required && (!config[field.key] || !config[field.key].trim())) {
            return `${field.label} is required for ${serviceName}`;
          }
        }
        for (const field of providerFields.configFields) {
          if (field.required && (!config[field.key] || !config[field.key].trim())) {
            return `${field.label} is required for ${serviceName}`;
          }
        }
        return null;
      };

      const failValidation = (msg: string) => {
        setMessage(`❌ ${msg}`);
        setMessageType("error");
        setIsTesting(false);
      };

      if (useMultipleProviders) {
        const completionError = validateProvider(completionProvider, completionConfig, "Completion Service");
        if (completionError) { failValidation(completionError); return; }
        if (!completionDefaultModel.trim()) { failValidation("Model Name is required for Completion Service"); return; }

        const embeddingError = validateProvider(embeddingProvider, embeddingConfig, "Embedding Service");
        if (embeddingError) { failValidation(embeddingError); return; }
        if (!embeddingModel.trim()) { failValidation("Model Name is required for Embedding Service"); return; }

        if (useCustomMultimodal) {
          const multimodalError = validateProvider(multimodalProvider, multimodalConfig, "Multimodal Service");
          if (multimodalError) { failValidation(multimodalError); return; }
          if (!multimodalModelName.trim()) { failValidation("Model Name is required for Multimodal Service"); return; }
        }

        if (useCustomChatbot) {
          const chatbotError = validateProvider(chatbotProvider, chatbotProviderConfig, "Chatbot Service");
          if (chatbotError) { failValidation(chatbotError); return; }
          if (!chatbotModelName.trim()) { failValidation("Model Name is required for Chatbot Service"); return; }
        }
      } else {
        const singleError = validateProvider(completionProvider, completionConfig, completionProvider);
        if (singleError) { failValidation(singleError); return; }
        if (!completionDefaultModel.trim()) { failValidation("Completion Model is required"); return; }
        if (!embeddingModel.trim()) { failValidation("Embedding Model is required"); return; }
        if (useCustomMultimodal && !multimodalModelName.trim()) { failValidation("Multimodal Model is required when not inheriting from completion"); return; }
        if (useCustomChatbot && !chatbotModelName.trim()) { failValidation("Chatbot Model is required when not inheriting from completion"); return; }
      }
      
      const creds = sessionStorage.getItem("creds");
      const llmConfigData = buildLLMConfigPayload();

      const response = await fetch("/ui/config/llm/test", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(llmConfigData),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Connection test failed");
      }

      const result = await response.json();
      setTestResults(result.results);
      
      if (result.status === "success") {
        setMessage("✅ All connection tests passed successfully!");
        setMessageType("success");
        setConnectionTested(true);
      } else {
        setMessage("⚠️ Some connection tests failed. See details below.");
        setMessageType("error");
        setConnectionTested(false);
      }
      
    } catch (error: any) {
      console.error("Error testing connection:", error);
      setMessage(`❌ Error: ${error.message}`);
      setMessageType("error");
    } finally {
      setIsTesting(false);
    }
  };

  const getPromptPath = (provider: string) => {
    const providerMap: Record<string, string> = {
      openai: "openai_gpt4",
      azure: "openai_gpt4",
      genai: "google_gemini",
      vertexai: "gcp_vertexai_palm",
      bedrock: "aws_bedrock_claude3haiku",
    };
    return providerMap[provider] || "openai_gpt4";
  };

  // Get placeholder text based on provider
  const getModelPlaceholder = (provider: string, modelType: 'llm' | 'embedding' | 'multimodal') => {
    const placeholders: Record<string, Record<string, string>> = {
      openai: {
        llm: "e.g., gpt-4o-mini, gpt-4o, gpt-4-turbo",
        embedding: "e.g., text-embedding-3-small, text-embedding-3-large",
        multimodal: "e.g., gpt-4o, gpt-4-turbo"
      },
      azure: {
        llm: "e.g., gpt-4, gpt-35-turbo (your deployment name)",
        embedding: "e.g., text-embedding-ada-002 (your deployment name)",
        multimodal: "e.g., gpt-4-vision (your deployment name)"
      },
      genai: {
        llm: "e.g., gemini-1.5-flash, gemini-1.5-pro",
        embedding: "e.g., models/text-embedding-004",
        multimodal: "e.g., gemini-1.5-flash, gemini-1.5-pro"
      },
      vertexai: {
        llm: "e.g., gemini-1.5-flash, text-bison",
        embedding: "e.g., text-embedding-004, textembedding-gecko",
        multimodal: "e.g., gemini-1.5-flash, gemini-pro-vision"
      },
      bedrock: {
        llm: "e.g., anthropic.claude-3-haiku-20240307-v1:0",
        embedding: "e.g., amazon.titan-embed-text-v1",
        multimodal: "e.g., anthropic.claude-3-sonnet-20240229-v1:0"
      },
      groq: {
        llm: "e.g., llama-3.1-70b-versatile, mixtral-8x7b-32768",
        embedding: "Not supported",
        multimodal: "Not supported"
      },
      ollama: {
        llm: "e.g., llama3.2, llama3.1, mistral",
        embedding: "e.g., nomic-embed-text, mxbai-embed-large",
        multimodal: "e.g., llama3.2-vision, llava"
      },
      sagemaker: {
        llm: "Your SageMaker endpoint name",
        embedding: "Not supported",
        multimodal: "Not supported"
      },
      huggingface: {
        llm: "e.g., meta-llama/Meta-Llama-3-8B-Instruct",
        embedding: "Not supported",
        multimodal: "Not supported"
      },
      watsonx: {
        llm: "e.g., ibm/granite-13b-chat-v2",
        embedding: "Not supported",
        multimodal: "Not supported"
      }
    };
    
    return placeholders[provider]?.[modelType] || "Enter model name";
  };

  // Render provider-specific fields
  const renderProviderFields = (provider: string, config: Record<string, string>, setConfig: (config: Record<string, string>) => void) => {
    const providerFields = PROVIDER_FIELDS[provider as keyof typeof PROVIDER_FIELDS];
    if (!providerFields) return null;

    const handleFieldChange = (key: string, value: string) => {
      setConfig({ ...config, [key]: value });
      clearTestResults();
    };

    return (
      <>
        {providerFields.note && (
          <div className="p-3 bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300 text-sm rounded-lg">
            ℹ️ {providerFields.note}
          </div>
        )}
        
        {providerFields.authFields.map(field => (
          <div key={field.key}>
            <label className="block text-sm font-medium mb-2 text-black dark:text-white">
              {field.label} {field.required && <span className="text-red-500">*</span>}
            </label>
            <Input
              type={field.type}
              className="dark:border-[#3D3D3D] dark:bg-background"
              placeholder={field.placeholder || ""}
              value={config[field.key] || ""}
              onChange={(e) => handleFieldChange(field.key, e.target.value)}
            />
          </div>
        ))}
        
        {providerFields.configFields.map(field => (
          <div key={field.key}>
            <label className="block text-sm font-medium mb-2 text-black dark:text-white">
              {field.label} {field.required && <span className="text-red-500">*</span>}
            </label>
            <Input
              type={field.type}
              className="dark:border-[#3D3D3D] dark:bg-background"
              placeholder={field.placeholder || ""}
              value={config[field.key] || ""}
              onChange={(e) => handleFieldChange(field.key, e.target.value)}
            />
          </div>
        ))}
      </>
    );
  };

  if (isLoading) {
    return (
      <div className="p-8 flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-tigerOrange" />
      </div>
    );
  }

  if (isChatbotOnlyMode) {
    return (
      <div className="p-8">
        <div className="max-w-5xl mx-auto">
          <div className="mb-8">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center">
                <Server className="h-6 w-6 text-tigerOrange" />
              </div>
              <div>
                <h1 className="text-2xl font-bold text-black dark:text-white">
                  Chatbot LLM Configuration
                </h1>
                <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                  Configure the LLM provider and model for the chatbot.
                </p>
              </div>
            </div>
          </div>

          <ConfigScopeToggle
            configScope="graph"
            selectedGraph={selectedGraph}
            availableGraphs={availableGraphs}
            graphOnly={true}
            className="mb-6"
            onScopeChange={() => {}}
            onGraphChange={(value) => {
              setSelectedGraph(value);
              sessionStorage.setItem("selectedGraph", value);
              window.dispatchEvent(new Event("graphrag:selectedGraph"));
              clearTestResults();
              fetchConfig(undefined, value);
            }}
          />

          <div className="space-y-6">
            {/* Mode toggle */}
            <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
              <div className="space-y-4">
                <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                  Configuration Mode
                </label>
                <div className="flex gap-4">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="chatbotConfigMode"
                      checked={!useCustomChatbot}
                      onChange={() => { setUseCustomChatbot(false); clearTestResults(); }}
                      className="h-4 w-4"
                    />
                    <span className="text-sm text-black dark:text-white">Inherit from global config</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="chatbotConfigMode"
                      checked={useCustomChatbot}
                      onChange={() => { setUseCustomChatbot(true); clearTestResults(); }}
                      className="h-4 w-4"
                    />
                    <span className="text-sm text-black dark:text-white">Use custom LLM provider</span>
                  </label>
                </div>
                {!useCustomChatbot && globalChatInfo.llm_service && (
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    Currently inherited: {globalChatInfo.llm_service} / {globalChatInfo.llm_model}
                  </p>
                )}
              </div>
            </div>

            {/* Custom provider config */}
            {useCustomChatbot && (
              <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
                <h3 className="text-lg font-semibold mb-4 text-black dark:text-white">Custom Chatbot Provider</h3>
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      LLM Provider <span className="text-red-500">*</span>
                    </label>
                    <Select
                      value={chatbotProvider}
                      onValueChange={(value) => {
                        setChatbotProvider(value);
                        setChatbotProviderConfig({});
                        setChatbotModelName("");
                        clearTestResults();
                      }}
                    >
                      <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {LLM_PROVIDERS.map((p) => (
                          <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  {renderProviderFields(chatbotProvider, chatbotProviderConfig, setChatbotProviderConfig)}

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Model Name <span className="text-red-500">*</span>
                    </label>
                    <Input
                      type="text"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder={getModelPlaceholder(chatbotProvider, 'llm')}
                      value={chatbotModelName}
                      onChange={(e) => { setChatbotModelName(e.target.value); clearTestResults(); }}
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Temperature
                    </label>
                    <Input
                      type="number"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="0"
                      min="0"
                      max="2"
                      step="0.1"
                      value={chatbotTemperature}
                      onChange={(e) => { setChatbotTemperature(e.target.value); clearTestResults(); }}
                    />
                  </div>
                </div>
              </div>
            )}

            {/* Test results */}
            {testResults && (
              <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
                <h3 className="text-lg font-semibold mb-4 text-black dark:text-white">Test Results</h3>
                {Object.entries(testResults).map(([key, result]: [string, any]) => (
                  <div key={key} className={`p-3 rounded-lg text-sm mb-2 ${
                    result.status === "success"
                      ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                      : result.status === "error"
                      ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                      : "bg-gray-50 dark:bg-gray-900/20 text-gray-500"
                  }`}>
                    {result.message || `${key}: ${result.status}`}
                  </div>
                ))}
              </div>
            )}

            {message && (
              <div
                className={`p-4 rounded-lg text-sm mb-4 ${
                  messageType === "success"
                    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                    : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                }`}
              >
                {message}
              </div>
            )}

            <div className="flex gap-3">
              {useCustomChatbot && (
                <Button
                  onClick={handleTestConnection}
                  disabled={isSaving || isTesting}
                  variant="outline"
                  className="flex-1"
                >
                  {isTesting ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      Testing...
                    </>
                  ) : (
                    <>
                      <CheckCircle2 className="h-4 w-4 mr-2" />
                      Test Connection
                    </>
                  )}
                </Button>
              )}
              <Button
                onClick={handleSave}
                disabled={isSaving || isTesting || (useCustomChatbot && !connectionTested)}
                className="gradient text-white flex-1"
              >
                {isSaving ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Saving...
                  </>
                ) : (
                  <>
                    <Save className="h-4 w-4 mr-2" />
                    Save Configuration
                  </>
                )}
              </Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center">
              <Server className="h-6 w-6 text-tigerOrange" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-black dark:text-white">
                LLM Configuration
              </h1>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                Configure your Large Language Model provider settings
              </p>
            </div>
          </div>
        </div>

        <fieldset>
        <div className="space-y-6">
          {/* Config Scope Toggle (superadmin) */}
          <ConfigScopeToggle
            configScope={configScope}
            selectedGraph={selectedGraph}
            availableGraphs={availableGraphs}
            className=""
            onScopeChange={(scope) => {
              setConfigScope(scope);
              clearTestResults();
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
              clearTestResults();
              fetchConfig("graph", value);
            }}
            graphSelectedHint={
              Object.keys(graphOverrides).length > 0
                ? `Overridden services: ${Object.keys(graphOverrides).join(", ")}. Other settings are inherited from global.`
                : "No per-graph overrides set. All settings are inherited from global defaults."
            }
          />

          {/* Multi-Provider Toggle */}
          <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
            <div className="flex items-center space-x-3">
              <input
                type="checkbox"
                id="multiProvider"
                checked={useMultipleProviders}
                onChange={(e) => {
                  setUseMultipleProviders(e.target.checked);
                  if (!e.target.checked) {
                    // Switching to single-provider: unify providers/configs to completion
                    setEmbeddingProvider(completionProvider);
                    setEmbeddingConfig({ ...completionConfig });
                    setMultimodalProvider(completionProvider);
                    setMultimodalConfig({ ...completionConfig });
                  }
                  clearTestResults();
                }}
                className="h-4 w-4 rounded border-gray-300 dark:border-[#3D3D3D]"
              />
              <label htmlFor="multiProvider" className="text-sm font-medium text-black dark:text-white">
                Use different providers for different services
              </label>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-2 ml-7">
              Enable this to configure separate providers for chat completion, embeddings, and multimodal services
            </p>
          </div>

          {/* Single Provider Configuration */}
          {!useMultipleProviders && (
            <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
              <div className="space-y-6">
                <div>
                  <h2 className="text-lg font-semibold mb-4 text-black dark:text-white">
                    Provider Settings
                  </h2>
                  <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-6">
                    Configure your LLM provider settings for all services.
                  </p>

                  <div className="space-y-4">
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Provider
                      </label>
                      <Select value={completionProvider} onValueChange={(value) => handleProviderChange(value, 'completion')}>
                        <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                          <SelectValue placeholder="Select provider" />
                        </SelectTrigger>
                        <SelectContent>
                          {LLM_PROVIDERS.map((p) => (
                            <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        This provider will be used for all services (completion, embedding, multimodal)
                      </p>
                    </div>

                    {renderProviderFields(completionProvider, completionConfig, setCompletionConfig)}

                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Completion Model <span className="text-red-500">*</span>
                      </label>
                      <Input
                        type="text"
                        className="dark:border-[#3D3D3D] dark:bg-background"
                        placeholder={getModelPlaceholder(completionProvider, 'llm')}
                        value={completionDefaultModel}
                        onChange={(e) => {
                          setCompletionDefaultModel(e.target.value);
                          clearTestResults();
                        }}
                      />
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        Used by ECC for entity extraction and community summarization during document ingestion
                      </p>
                    </div>

                    <hr className="border-gray-200 dark:border-[#3D3D3D]" />

                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Chatbot Model
                      </label>
                      <div className="flex items-center space-x-2 mb-2">
                        <input
                          type="checkbox"
                          id="inheritChatbotModel"
                          className="rounded border-gray-300 dark:border-[#3D3D3D]"
                          checked={!useCustomChatbot}
                          onChange={(e) => {
                            setUseCustomChatbot(!e.target.checked);
                            clearTestResults();
                          }}
                        />
                        <label htmlFor="inheritChatbotModel" className="text-sm text-black dark:text-white">
                          Use same model as completion service
                        </label>
                      </div>
                      {useCustomChatbot && (
                        <Input
                          type="text"
                          className="dark:border-[#3D3D3D] dark:bg-background"
                          placeholder={getModelPlaceholder(completionProvider, 'llm')}
                          value={chatbotModelName}
                          onChange={(e) => {
                            setChatbotModelName(e.target.value);
                            clearTestResults();
                          }}
                        />
                      )}
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        Used by the chatbot for answering user questions
                      </p>
                    </div>

                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Chatbot Temperature
                      </label>
                      <Input
                        type="number"
                        className="dark:border-[#3D3D3D] dark:bg-background"
                        placeholder="0"
                        min="0"
                        max="2"
                        step="0.1"
                        value={chatbotTemperature}
                        onChange={(e) => { setChatbotTemperature(e.target.value); clearTestResults(); }}
                      />
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        Controls randomness of chatbot responses (0 = deterministic, higher = more creative)
                      </p>
                    </div>

                    <hr className="border-gray-200 dark:border-[#3D3D3D]" />

                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Multimodal Model
                      </label>
                      <div className="flex items-center space-x-2 mb-2">
                        <input
                          type="checkbox"
                          id="inheritMultimodalModel"
                          className="rounded border-gray-300 dark:border-[#3D3D3D]"
                          checked={!useCustomMultimodal}
                          onChange={(e) => {
                            setUseCustomMultimodal(!e.target.checked);
                            clearTestResults();
                          }}
                        />
                        <label htmlFor="inheritMultimodalModel" className="text-sm text-black dark:text-white">
                          Use same model as completion service
                        </label>
                      </div>
                      {!useCustomMultimodal && (
                        <p className="text-xs text-amber-600 dark:text-amber-400 mb-2">
                          Ensure your completion model supports vision input. Use "Test Connection" to verify.
                        </p>
                      )}
                      {useCustomMultimodal && (
                        <Input
                          type="text"
                          className="dark:border-[#3D3D3D] dark:bg-background"
                          placeholder={getModelPlaceholder(completionProvider, 'multimodal')}
                          value={multimodalModelName}
                          onChange={(e) => {
                            setMultimodalModelName(e.target.value);
                            clearTestResults();
                          }}
                        />
                      )}
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        Used for processing images and multimodal content
                      </p>
                    </div>

                    <hr className="border-gray-200 dark:border-[#3D3D3D]" />

                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Embedding Model <span className="text-red-500">*</span>
                      </label>
                      <Input
                        type="text"
                        className="dark:border-[#3D3D3D] dark:bg-background"
                        placeholder={getModelPlaceholder(completionProvider, 'embedding')}
                        value={embeddingModel}
                        onChange={(e) => {
                          setEmbeddingModel(e.target.value);
                          clearTestResults();
                        }}
                      />
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        Used for generating vector embeddings of document chunks
                      </p>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Multiple Provider Configuration */}
          {useMultipleProviders && (
            <>
              {/* Completion Provider */}
              <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
                <h2 className="text-lg font-semibold mb-4 text-black dark:text-white">
                  Completion Service
                </h2>
                <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-6">
                  LLM provider for entity extraction and community summarization during document ingestion.
                </p>

                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Provider
                    </label>
                    <Select value={completionProvider} onValueChange={(value) => handleProviderChange(value, 'completion')}>
                      <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                        <SelectValue placeholder="Select provider" />
                      </SelectTrigger>
                      <SelectContent>
                        {LLM_PROVIDERS.map((p) => (
                          <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  {renderProviderFields(completionProvider, completionConfig, setCompletionConfig)}

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Model Name <span className="text-red-500">*</span>
                    </label>
                    <Input
                      type="text"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder={getModelPlaceholder(completionProvider, 'llm')}
                      value={completionDefaultModel}
                      onChange={(e) => {
                        setCompletionDefaultModel(e.target.value);
                        clearTestResults();
                      }}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Model for entity extraction and community summarization.
                    </p>
                  </div>
                </div>
              </div>

              {/* Chatbot Service */}
              <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
                <h2 className="text-lg font-semibold mb-4 text-black dark:text-white">
                  Chatbot Service
                </h2>
                <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
                  Configure the LLM provider and model used by the chatbot for answering user questions.
                </p>

                <div className="space-y-4">
                  <div className="flex items-center space-x-2">
                    <input
                      type="checkbox"
                      id="inheritChatbotService"
                      className="rounded border-gray-300 dark:border-[#3D3D3D]"
                      checked={!useCustomChatbot}
                      onChange={(e) => {
                        setUseCustomChatbot(!e.target.checked);
                        clearTestResults();
                      }}
                    />
                    <label htmlFor="inheritChatbotService" className="text-sm font-medium text-black dark:text-white">
                      Inherit from completion service
                    </label>
                  </div>

                  {useCustomChatbot && (
                    <>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                          Provider <span className="text-red-500">*</span>
                        </label>
                        <Select
                          value={chatbotProvider}
                          onValueChange={(value) => {
                            setChatbotProvider(value);
                            setChatbotProviderConfig({});
                            setChatbotModelName("");
                            clearTestResults();
                          }}
                        >
                          <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {LLM_PROVIDERS.map((p) => (
                              <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>

                      {renderProviderFields(chatbotProvider, chatbotProviderConfig, setChatbotProviderConfig)}

                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                          Model Name <span className="text-red-500">*</span>
                        </label>
                        <Input
                          type="text"
                          className="dark:border-[#3D3D3D] dark:bg-background"
                          placeholder={getModelPlaceholder(chatbotProvider, 'llm')}
                          value={chatbotModelName}
                          onChange={(e) => { setChatbotModelName(e.target.value); clearTestResults(); }}
                        />
                      </div>

                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                          Temperature
                        </label>
                        <Input
                          type="number"
                          className="dark:border-[#3D3D3D] dark:bg-background"
                          placeholder="0"
                          min="0"
                          max="2"
                          step="0.1"
                          value={chatbotTemperature}
                          onChange={(e) => { setChatbotTemperature(e.target.value); clearTestResults(); }}
                        />
                      </div>
                    </>
                  )}
                </div>
              </div>

              {/* Multimodal Service Provider */}
              <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
                <h2 className="text-lg font-semibold mb-4 text-black dark:text-white">
                  Multimodal Service
                </h2>
                <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">
                  Configure the provider for processing images and multimodal content (vision tasks).
                </p>

                <div className="space-y-4">
                  <div className="flex items-center space-x-2">
                    <input
                      type="checkbox"
                      id="inheritMultimodalService"
                      className="rounded border-gray-300 dark:border-[#3D3D3D]"
                      checked={!useCustomMultimodal}
                      onChange={(e) => {
                        setUseCustomMultimodal(!e.target.checked);
                        clearTestResults();
                      }}
                    />
                    <label htmlFor="inheritMultimodalService" className="text-sm font-medium text-black dark:text-white">
                      Inherit from completion service
                    </label>
                  </div>
                  {!useCustomMultimodal && (
                    <p className="text-xs text-amber-600 dark:text-amber-400">
                      Ensure your completion model supports vision input. Use "Test Connection" to verify.
                    </p>
                  )}

                  {useCustomMultimodal && (
                    <>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                          Provider
                        </label>
                        <Select value={multimodalProvider} onValueChange={(value) => handleProviderChange(value, 'multimodal')}>
                          <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                            <SelectValue placeholder="Select provider" />
                          </SelectTrigger>
                          <SelectContent>
                            {LLM_PROVIDERS.map((p) => (
                              <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>

                      {renderProviderFields(multimodalProvider, multimodalConfig, setMultimodalConfig)}

                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                          Model Name <span className="text-red-500">*</span>
                        </label>
                        <Input
                          type="text"
                          className="dark:border-[#3D3D3D] dark:bg-background"
                          placeholder={getModelPlaceholder(multimodalProvider, 'multimodal')}
                          value={multimodalModelName}
                          onChange={(e) => {
                            setMultimodalModelName(e.target.value);
                            clearTestResults();
                          }}
                        />
                      </div>
                    </>
                  )}
                </div>
              </div>

              {/* Embedding Service Provider */}
              <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
                <h2 className="text-lg font-semibold mb-4 text-black dark:text-white">
                  Embedding Service
                </h2>
                <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-6">
                  Configure the provider for generating embeddings.
                </p>

                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Provider
                    </label>
                    <Select value={embeddingProvider} onValueChange={(value) => handleProviderChange(value, 'embedding')}>
                      <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-background">
                        <SelectValue placeholder="Select provider" />
                      </SelectTrigger>
                      <SelectContent>
                        {LLM_PROVIDERS.map((p) => (
                          <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  {renderProviderFields(embeddingProvider, embeddingConfig, setEmbeddingConfig)}

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Model Name <span className="text-red-500">*</span>
                    </label>
                    <Input
                      type="text"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder={getModelPlaceholder(embeddingProvider, 'embedding')}
                      value={embeddingModel}
                      onChange={(e) => {
                        setEmbeddingModel(e.target.value);
                        clearTestResults();
                      }}
                    />
                  </div>
                </div>
              </div>
            </>
          )}


          {message && (
            <div
              className={`p-4 rounded-lg text-sm mb-4 ${
                messageType === "success"
                  ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                  : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
              }`}
            >
              {message}
            </div>
          )}

          {/* Test Results */}
          {testResults && (
            <div className="space-y-3 mb-4">
              <h3 className="text-sm font-semibold text-black dark:text-white">Connection Test Results:</h3>
              
              {testResults.completion && testResults.completion.status !== "not_tested" && (
                <div className={`p-3 rounded-lg text-sm ${
                  testResults.completion.status === "success"
                    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                    : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                }`}>
                  <strong>Completion Model:</strong> {testResults.completion.message}
                </div>
              )}
              
              {testResults.chatbot && testResults.chatbot.status !== "not_tested" && (
                <div className={`p-3 rounded-lg text-sm ${
                  testResults.chatbot.status === "success"
                    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                    : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                }`}>
                  <strong>Chatbot Model:</strong> {testResults.chatbot.message}
                </div>
              )}

              {testResults.multimodal && testResults.multimodal.status !== "not_tested" && (
                <div className={`p-3 rounded-lg text-sm ${
                  testResults.multimodal.status === "success"
                    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                    : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                }`}>
                  <strong>Multimodal Model:</strong> {testResults.multimodal.message}
                </div>
              )}

              {testResults.embedding && testResults.embedding.status !== "not_tested" && (
                <div className={`p-3 rounded-lg text-sm ${
                  testResults.embedding.status === "success"
                    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                    : "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                }`}>
                  <strong>Embedding Model:</strong> {testResults.embedding.message}
                </div>
              )}
            </div>
          )}

          {/* Buttons */}
          <div className="flex gap-3">
            {!isChatbotOnlyMode && (
              <Button
                onClick={handleTestConnection}
                disabled={isTesting || isSaving}
                className="flex-1 bg-blue-600 hover:bg-blue-700 text-white"
              >
                <CheckCircle2 className="h-4 w-4 mr-2" />
                {isTesting ? "Testing..." : "Test Connection"}
              </Button>
            )}

            <Button 
              onClick={handleSave} 
              disabled={isSaving || isTesting || (!isChatbotOnlyMode && !connectionTested)}
              className="gradient text-white flex-1"
            >
              {isSaving ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Saving...
                </>
              ) : (
                <>
                  <Save className="h-4 w-4 mr-2" />
                  Save Configuration
                </>
              )}
            </Button>
          </div>
        </div>
        </fieldset>
      </div>
    </div>
  );
};

export default LLMConfig;
