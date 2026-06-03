import React, { useState, useEffect } from "react";
import { Server, Save, CheckCircle2, AlertCircle, RefreshCw, Loader2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { safeJson } from "@/utils/safeJson";

const GraphDBConfig = () => {
  // Default values for fields — shown as placeholders, used if user leaves field empty
  const DEFAULTS = {
    hostname: "http://tigergraph",
    restppPort: "9000",
    gsPort: "14240",
    username: "tigergraph",
    defaultTimeout: "300",
    defaultMemThreshold: "5000",
    defaultThreadLimit: "8",
  };

  const [hostname, setHostname] = useState("");
  const [restppPort, setRestppPort] = useState("");
  const [gsPort, setGsPort] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [getToken, setGetToken] = useState(false);
  const [useApiToken, setUseApiToken] = useState(false);
  const [apiToken, setApiToken] = useState("");
  const [defaultTimeout, setDefaultTimeout] = useState("");
  const [defaultMemThreshold, setDefaultMemThreshold] = useState("");
  const [defaultThreadLimit, setDefaultThreadLimit] = useState("");

  /** Return the effective value: user input if non-empty, otherwise the default */
  const effective = (value: string, key: keyof typeof DEFAULTS) => value || DEFAULTS[key];
  
  // Track original values to detect changes
  const [originalHostname, setOriginalHostname] = useState("");
  const [originalUsername, setOriginalUsername] = useState("");
  
  const [isLoading, setIsLoading] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [connectionTested, setConnectionTested] = useState(false);
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState<"success" | "error" | "">("");

  // Vector store status — polled from /health so the operator sees
  // when the background retry loop has recovered the store and can
  // force an immediate retry after fixing TigerGraph.
  const [storeStatus, setStoreStatus] = useState<"ok" | "initializing" | "error" | "unknown">("unknown");
  const [storeError, setStoreError] = useState<string | null>(null);
  const [isRetryingStore, setIsRetryingStore] = useState(false);
  const [retryMessage, setRetryMessage] = useState("");

  const fetchStoreStatus = async () => {
    try {
      const creds = sessionStorage.getItem("auth");
      if (!creds) return;
      const r = await fetch("/ui/admin/embedding_store_status", {
        headers: { Authorization: creds },
      });
      if (!r.ok) return;
      const data = await safeJson(r);
      setStoreStatus(data?.status || "unknown");
      setStoreError(data?.error || null);
    } catch {
      // Ignore — leave previous status visible.
    }
  };

  // One-shot status check on page entry — vector-store outages are
  // rare and the background retry loop self-heals; if the operator
  // needs to force recovery they click Retry Connection (shown only
  // on error).
  useEffect(() => {
    fetchStoreStatus();
  }, []);

  const handleRetryEmbeddingStore = async () => {
    setIsRetryingStore(true);
    setRetryMessage("");
    try {
      const creds = sessionStorage.getItem("auth");
      const r = await fetch("/ui/admin/retry_embedding_store", {
        method: "POST",
        headers: { Authorization: creds! },
      });
      const data = await safeJson(r);
      if (!r.ok) {
        throw new Error(data.detail || `Retry failed: ${r.statusText}`);
      }
      setStoreStatus(data.status || "unknown");
      setStoreError(data.error || null);
      if (data.status === "ok") {
        setRetryMessage("✅ Vector store reconnected.");
      } else {
        setRetryMessage(`⚠️ Retry attempted, store still ${data.status}.`);
      }
    } catch (err: any) {
      setRetryMessage(`❌ ${err.message || "Retry failed."}`);
    } finally {
      setIsRetryingStore(false);
    }
  };

  useEffect(() => {
    fetchConfig();
  }, []);


  const fetchConfig = async () => {
    setIsLoading(true);
    try {
      const creds = sessionStorage.getItem("auth");
      const response = await fetch("/ui/config", {
        headers: { Authorization: creds! },
      });

      if (!response.ok) {
        throw new Error("Failed to fetch configuration");
      }

      const data = await response.json();
      const dbConfig = data.db_config;

      if (dbConfig) {
        // Only populate state with non-default values; defaults show as placeholders
        const h = dbConfig.hostname || "";
        const u = dbConfig.username || "";
        setHostname(h === DEFAULTS.hostname ? "" : h);
        setOriginalHostname(h || DEFAULTS.hostname);
        setRestppPort(dbConfig.restppPort === DEFAULTS.restppPort ? "" : (dbConfig.restppPort || ""));
        setGsPort(dbConfig.gsPort === DEFAULTS.gsPort ? "" : (dbConfig.gsPort || ""));
        setUsername(u === DEFAULTS.username ? "" : u);
        setOriginalUsername(u || DEFAULTS.username);
        setPassword(dbConfig.password || "");
        setGetToken(dbConfig.getToken || false);
        const token = dbConfig.apiToken || "";
        setApiToken(token);
        setUseApiToken(!!token);
        const t = String(dbConfig.default_timeout || "");
        const m = String(dbConfig.default_mem_threshold || "");
        const tl = String(dbConfig.default_thread_limit || "");
        setDefaultTimeout(t === DEFAULTS.defaultTimeout ? "" : t);
        setDefaultMemThreshold(m === DEFAULTS.defaultMemThreshold ? "" : m);
        setDefaultThreadLimit(tl === DEFAULTS.defaultThreadLimit ? "" : tl);
      }
    } catch (error: any) {
      console.error("Error fetching config:", error);
      setMessage(`Failed to load configuration: ${error.message}`);
      setMessageType("error");
    } finally {
      setIsLoading(false);
    }
  };

  const handleTestConnection = async () => {
    setIsTesting(true);
    setMessage("");
    setMessageType("");
    setConnectionTested(false);

    try {
      const creds = sessionStorage.getItem("auth");
      const testConfig: any = {
        hostname: effective(hostname, "hostname"),
        restppPort: effective(restppPort, "restppPort"),
        gsPort: effective(gsPort, "gsPort"),
        username: effective(username, "username"),
        password,
        getToken,
      };
      if (useApiToken && apiToken.trim()) {
        testConfig.apiToken = apiToken.trim();
      }

      const response = await fetch("/ui/config/db/test", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: creds!,
        },
        body: JSON.stringify(testConfig),
      });

      const result = await response.json();

      if (response.ok && result.status === "success") {
        setConnectionTested(true);
        const versionBit = result.tg_version
          ? ` (TigerGraph ${result.tg_version})`
          : "";
        const vectorBit = result.vector_details
          ? `\nVector Store: ${result.vector_supported ? "✓" : "⚠️"} ${result.vector_details}`
          : "";
        setMessage(
          `Connection successful${versionBit}. You can now save the configuration.${vectorBit}`
        );
        setMessageType("success");
      } else {
        setMessage(result.message || "Connection test failed");
        setMessageType("error");
      }
    } catch (error: any) {
      setMessage(`Connection test error: ${error.message}`);
      setMessageType("error");
    } finally {
      setIsTesting(false);
    }
  };

  const handleSave = async () => {
    if (!connectionTested) {
      setMessage("Please test the connection first before saving");
      setMessageType("error");
      return;
    }
    setIsSaving(true);
    setMessage("");
    setMessageType("");

    try {
      const creds = sessionStorage.getItem("auth");
      const effectiveHostname = effective(hostname, "hostname");
      const effectiveUsername = effective(username, "username");
      const dbConfigData: any = {
        hostname: effectiveHostname,
        restppPort: effective(restppPort, "restppPort"),
        gsPort: effective(gsPort, "gsPort"),
        username: effectiveUsername,
        password,
        getToken,
        default_timeout: parseInt(effective(defaultTimeout, "defaultTimeout")),
        default_mem_threshold: parseInt(effective(defaultMemThreshold, "defaultMemThreshold")),
        default_thread_limit: parseInt(effective(defaultThreadLimit, "defaultThreadLimit")),
      };
      if (useApiToken && apiToken.trim()) {
        dbConfigData.apiToken = apiToken.trim();
      }

      const response = await fetch("/ui/config/db", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: creds!,
        },
        body: JSON.stringify(dbConfigData),
      });

      const result = await response.json();

      if (response.ok) {
        setMessage("GraphDB configuration saved successfully!");
        setMessageType("success");
        setConnectionTested(false); // Reset after save
        
        // Check if hostname or username changed from what was loaded
        const hostnameChanged = originalHostname && effectiveHostname !== originalHostname;
        const usernameChanged = originalUsername && effectiveUsername !== originalUsername;
        
        // If hostname OR username changed, redirect to login so services reconnect
        if (hostnameChanged || usernameChanged) {
          const reason = hostnameChanged 
            ? "GraphDB hostname changed. Please relogin with the new credentials to connect to the new instance."
            : "GraphDB username changed. Please relogin with the new credentials.";
          
          setTimeout(() => {
            // Clear sessionStorage and redirect to login
            sessionStorage.removeItem("auth");
            alert(reason);
            window.location.href = "/"; // Redirect to root (login page)
          }, 2000); // Give user 2 seconds to see the success message
        } else {
          // Update originals after successful save (only if no redirect)
          setOriginalHostname(effectiveHostname);
          setOriginalUsername(effectiveUsername);
          // Reflect the just-triggered embedding-store re-init in the
          // indicator. Background init typically lands within a few
          // seconds; poll a second time so the operator sees the real
          // outcome (ok or error) without navigating away.
          fetchStoreStatus();
          setTimeout(fetchStoreStatus, 3000);
          setTimeout(fetchStoreStatus, 8000);
        }
      } else {
        setMessage(result.detail || "Failed to save configuration");
        setMessageType("error");
      }
    } catch (error: any) {
      setMessage(`Save error: ${error.message}`);
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
              <Server className="h-6 w-6 text-tigerOrange" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-black dark:text-white">
                Graph Database Configuration
              </h1>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                Configure your TigerGraph database connection and settings
              </p>
            </div>
          </div>
        </div>

        <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
          <fieldset>
            <div className="space-y-6">
            <div>
              <h2 className="text-lg font-semibold mb-4 text-black dark:text-white">
                Database Connection
              </h2>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-6">
                Configure your TigerGraph database connection parameters.
              </p>

              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Hostname
                  </label>
                  <Input
                    type="text"
                    className="dark:border-[#3D3D3D] dark:bg-background"
                    placeholder="http://tigergraph"
                    value={hostname}
                    onChange={(e) => {
                      setHostname(e.target.value);
                      setConnectionTested(false);
                      setMessage("");
                      setMessageType("");
                    }}
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    TigerGraph server hostname
                  </p>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      RESTPP Port
                    </label>
                    <Input
                      type="text"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="9000"
                      value={restppPort}
                      onChange={(e) => {
                        setRestppPort(e.target.value);
                        setConnectionTested(false);
                        setMessage("");
                        setMessageType("");
                      }}
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      GS Port
                    </label>
                    <Input
                      type="text"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="14240"
                      value={gsPort}
                      onChange={(e) => {
                        setGsPort(e.target.value);
                        setConnectionTested(false);
                        setMessage("");
                        setMessageType("");
                      }}
                    />
                  </div>
                </div>

                <div className="flex items-center space-x-2">
                  <input
                    type="checkbox"
                    id="useApiToken"
                    className="rounded border-gray-300 dark:border-[#3D3D3D]"
                    checked={useApiToken}
                    onChange={(e) => {
                      setUseApiToken(e.target.checked);
                      if (!e.target.checked) setApiToken("");
                      setConnectionTested(false);
                      setMessage("");
                      setMessageType("");
                    }}
                  />
                  <label htmlFor="useApiToken" className="text-sm font-medium text-black dark:text-white">
                    Use pre-generated API Token
                  </label>
                </div>

                {useApiToken ? (
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      API Token
                    </label>
                    <Input
                      type="password"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="Enter API token"
                      value={apiToken}
                      onChange={(e) => {
                        setApiToken(e.target.value);
                        setConnectionTested(false);
                        setMessage("");
                        setMessageType("");
                      }}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Token-based auth is used instead of username/password for API requests
                    </p>
                  </div>
                ) : (
                  <>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                          Username
                        </label>
                        <Input
                          type="text"
                          className="dark:border-[#3D3D3D] dark:bg-background"
                          placeholder={DEFAULTS.username}
                          value={username}
                          onChange={(e) => {
                            setUsername(e.target.value);
                            setConnectionTested(false);
                            setMessage("");
                            setMessageType("");
                          }}
                        />
                      </div>

                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                          Password
                        </label>
                        <Input
                          type="password"
                          className="dark:border-[#3D3D3D] dark:bg-background"
                          placeholder="Enter password"
                          value={password}
                          onChange={(e) => {
                            setPassword(e.target.value);
                            setConnectionTested(false);
                            setMessage("");
                            setMessageType("");
                          }}
                        />
                      </div>
                    </div>

                    <div className="flex items-center space-x-2">
                      <input
                        type="checkbox"
                        id="getToken"
                        className="rounded border-gray-300 dark:border-[#3D3D3D]"
                        checked={getToken}
                        onChange={(e) => {
                          setGetToken(e.target.checked);
                          setConnectionTested(false);
                          setMessage("");
                          setMessageType("");
                        }}
                      />
                      <label htmlFor="getToken" className="text-sm font-medium text-black dark:text-white">
                        Get Token
                      </label>
                    </div>
                  </>
                )}

                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Default Timeout
                    </label>
                    <Input
                      type="number"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="300"
                      value={defaultTimeout}
                      onChange={(e) => setDefaultTimeout(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Seconds
                    </p>
                  </div>

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Memory Threshold
                    </label>
                    <Input
                      type="number"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="5000"
                      value={defaultMemThreshold}
                      onChange={(e) => setDefaultMemThreshold(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      MB
                    </p>
                  </div>

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                      Thread Limit
                    </label>
                    <Input
                      type="number"
                      className="dark:border-[#3D3D3D] dark:bg-background"
                      placeholder="8"
                      value={defaultThreadLimit}
                      onChange={(e) => setDefaultThreadLimit(e.target.value)}
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Threads
                    </p>
                  </div>
                </div>

                {message && (
                  <div
                    className={`p-4 rounded-lg whitespace-pre-line ${
                      messageType === "success"
                        ? "bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-200"
                        : "bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-200"
                    }`}
                  >
                    {message}
                  </div>
                )}

                <div className="flex items-center gap-4">
                  <Button
                    onClick={handleTestConnection}
                    disabled={isTesting || (!password.trim() && !(useApiToken && apiToken.trim()))}
                    className="bg-blue-600 hover:bg-blue-700 text-white"
                  >
                    <CheckCircle2 className="h-4 w-4 mr-2" />
                    {isTesting ? "Testing..." : "Test Connection"}
                  </Button>

                  <Button
                    onClick={handleSave}
                    disabled={!connectionTested || isSaving}
                    className="gradient text-white"
                  >
                    <Save className="h-4 w-4 mr-2" />
                    {isSaving ? "Saving..." : "Save Configuration"}
                  </Button>

                  {!password.trim() && !(useApiToken && apiToken.trim()) && (
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      Enter password or API token to test connection
                    </p>
                  )}

                  <div className="ml-auto flex items-center gap-2">
                    {storeStatus === "error" ? (
                      <Button
                        onClick={handleRetryEmbeddingStore}
                        disabled={isRetryingStore}
                        variant="outline"
                        className="dark:border-[#3D3D3D] text-red-700 dark:text-red-300 border-red-300 dark:border-red-900"
                      >
                        {isRetryingStore ? (
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        ) : (
                          <RefreshCw className="h-4 w-4 mr-2" />
                        )}
                        {isRetryingStore ? "Reconnecting…" : "Reconnect Vector Store"}
                      </Button>
                    ) : (
                      <span
                        className="text-sm text-gray-700 dark:text-gray-300 inline-flex items-center gap-1.5"
                        title={
                          storeStatus === "ok"
                            ? "Vector store connected"
                            : storeStatus === "initializing"
                            ? "Vector store initializing"
                            : "Vector store status unavailable"
                        }
                      >
                        Vector Store
                        {storeStatus === "ok" && (
                          <CheckCircle2 className="h-4 w-4 text-green-600 dark:text-green-400" />
                        )}
                        {storeStatus === "initializing" && (
                          <Loader2 className="h-4 w-4 text-blue-600 dark:text-blue-400 animate-spin" />
                        )}
                        {storeStatus === "unknown" && (
                          <AlertCircle className="h-4 w-4 text-gray-400" />
                        )}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </div>
            </div>
          </fieldset>

          {storeStatus === "error" && (storeError || retryMessage) && (
            <div className="mt-4 px-4 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 text-sm break-words">
              {retryMessage || storeError}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default GraphDBConfig;

