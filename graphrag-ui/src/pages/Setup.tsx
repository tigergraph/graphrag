import React, { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {Database,Upload,RefreshCw,Loader2,Trash2,FolderUp,Cloud,ArrowLeft,CloudDownload,CloudCog,CloudLightning} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useConfirm } from "@/hooks/useConfirm";

const DEFAULT_MAX_UPLOAD_SIZE_MB = 100;
const envUploadLimit = Number(import.meta.env.VITE_MAX_UPLOAD_SIZE_MB);
const MAX_UPLOAD_SIZE_MB =
  Number.isFinite(envUploadLimit) && envUploadLimit > 0 ? envUploadLimit : DEFAULT_MAX_UPLOAD_SIZE_MB;
const MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024;

const formatBytes = (bytes: number) => {
  if (bytes === 0) return "0 Bytes";
  const units = ["Bytes", "KB", "MB", "GB", "TB"];
  const exponent = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / Math.pow(1024, exponent);
  const rounded = value >= 10 || exponent === 0 ? value.toFixed(0) : value.toFixed(1);
  return `${rounded} ${units[exponent]}`;
};

const Setup = () => {
  const navigate = useNavigate();
  const [confirm, confirmDialog, isConfirmDialogOpen] = useConfirm();
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);

  const [initializeGraphOpen, setInitializeGraphOpen] = useState(false);
  const [graphName, setGraphName] = useState("");
  const [isInitializing, setIsInitializing] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [statusType, setStatusType] = useState<"success" | "error" | "">("");

  // Data Ingest state
  const [ingestOpen, setIngestOpen] = useState(false);
  const [ingestGraphName, setIngestGraphName] = useState(""); // Graph to use for ingestion
  const [selectedFiles, setSelectedFiles] = useState<FileList | null>(null);
  const [uploadedFiles, setUploadedFiles] = useState<any[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadMessage, setUploadMessage] = useState("");
  const [isIngesting, setIsIngesting] = useState(false);
  const [ingestMessage, setIngestMessage] = useState("");

  // Ingestion temp files state
  const [tempSessionId, setTempSessionId] = useState<string | null>(null);
  const [tempFiles, setTempFiles] = useState<any[]>([]);
  const [showTempFiles, setShowTempFiles] = useState(false);
  const [ingestJobData, setIngestJobData] = useState<any>(null);
  const [directIngestion, setDirectIngestion] = useState(false);

  // Refresh state
  const [refreshOpen, setRefreshOpen] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshMessage, setRefreshMessage] = useState("");
  const [refreshGraphName, setRefreshGraphName] = useState("");
  const [isRebuildRunning, setIsRebuildRunning] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(false);

  // S3 / Bedrock state
  const [fileFormat, setFileFormat] = useState<"json" | "multi">("multi"); // default to multi (documents)
  const [awsAccessKey, setAwsAccessKey] = useState("");
  const [awsSecretKey, setAwsSecretKey] = useState("");
  const [dataPath, setDataPath] = useState("");
  const [inputBucket, setInputBucket] = useState("");
  const [outputBucket, setOutputBucket] = useState("");
  const [regionName, setRegionName] = useState("");

  // Cloud Download state
  const [cloudProvider, setCloudProvider] = useState<"s3" | "gcs" | "azure">("s3");
  const [cloudAccessKey, setCloudAccessKey] = useState("");
  const [cloudSecretKey, setCloudSecretKey] = useState("");
  const [cloudBucket, setCloudBucket] = useState("");
  const [cloudRegion, setCloudRegion] = useState("");
  const [cloudPrefix, setCloudPrefix] = useState("");
  // GCS specific
  const [gcsProjectId, setGcsProjectId] = useState("");
  const [gcsCredentials, setGcsCredentials] = useState("");
  // Azure specific
  const [azureAccountName, setAzureAccountName] = useState("");
  const [azureAccountKey, setAzureAccountKey] = useState("");
  const [azureContainer, setAzureContainer] = useState("");
  // Cloud download files state
  const [downloadedFiles, setDownloadedFiles] = useState<any[]>([]);
  const [isDownloading, setIsDownloading] = useState(false);
  const [downloadMessage, setDownloadMessage] = useState("");

  // Active tab state
  const [activeTab, setActiveTab] = useState("upload");

  // -------------------------
  // Networking helpers
  // -------------------------

  // Fetch uploaded files
  const fetchUploadedFiles = async () => {
    if (!ingestGraphName) return;

    try {
      const creds = localStorage.getItem("creds");
      const response = await fetch(`/ui/${ingestGraphName}/uploads/list`, {
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();
      setUploadedFiles(data.files || []);
    } catch (error) {
      console.error("Error fetching files:", error);
    }
  };

  // -------------------------
  // Upload handlers
  // -------------------------
  const handleUploadFiles = async () => {
    if (!selectedFiles || selectedFiles.length === 0) {
      setUploadMessage("Please select files to upload");
      return;
    }

    if (!ingestGraphName) {
      setUploadMessage("Please enter a graph name");
      return;
    }

    const filesArray = Array.from(selectedFiles);

    // Check if any single file exceeds the server limit
    const oversizedFiles = filesArray.filter((file) => file.size > MAX_UPLOAD_SIZE_BYTES);
    if (oversizedFiles.length > 0) {
      const names = oversizedFiles.map((file) => `${file.name} (${formatBytes(file.size)})`).join(", ");
      setUploadMessage(
        `❌ ${names} ${oversizedFiles.length === 1 ? "exceeds" : "exceed"} the ${MAX_UPLOAD_SIZE_MB} MB limit per file. ` +
          `Please split or compress ${oversizedFiles.length === 1 ? "this file" : "these files"}.`
      );
      return;
    }

    const totalSize = filesArray.reduce((sum, file) => sum + file.size, 0);

    // If total size exceeds limit and we have multiple files, upload one by one
    if (totalSize > MAX_UPLOAD_SIZE_BYTES && filesArray.length > 1) {
      await handleBatchUpload(filesArray);
      return;
    }

    // Single file or files within limit - upload normally
    setIsUploading(true);
    setUploadMessage("Uploading files...");

    try {
      const creds = localStorage.getItem("creds");
      const formData = new FormData();
      filesArray.forEach((file) => formData.append("files", file));

      const response = await fetch(`/ui/${ingestGraphName}/uploads?overwrite=true`, {
        method: "POST",
        headers: { Authorization: `Basic ${creds}` },
        body: formData,
      });

      const data = await response.json();
      if (data.status === "success") {
        setUploadMessage("✅ Successfully uploaded the files. Wait for file processing");
        setSelectedFiles(null);
        await fetchUploadedFiles();
        setIsUploading(false);

        // Step 2: Call create_ingest to process uploaded files in background
        console.log("Calling handleCreateIngestAfterUpload from main upload...");
        handleCreateIngestAfterUpload("uploaded").catch((err) => {
          console.error("Error in background processing:", err);
        });
      } else {
        setUploadMessage(`⚠️ ${data.message}`);
        setIsUploading(false);
      }
    } catch (error: any) {
      console.error("Upload error:", error);
      setUploadMessage(`❌ Error: ${error.message}`);
      setIsUploading(false);
    }
  };

  // Handle batch upload when total size exceeds limit - upload one file at a time
  const handleBatchUpload = async (filesArray: File[]) => {
    setIsUploading(true);
    setUploadMessage("Total size exceeds limit. Uploading files one by one...");

    try {
      const creds = localStorage.getItem("creds");
      let uploadedCount = 0;
      let failedCount = 0;
      const totalFiles = filesArray.length;

      // Upload files one at a time to avoid 413 errors
      for (let i = 0; i < filesArray.length; i++) {
        const file = filesArray[i];
        const fileNumber = i + 1;

        setUploadMessage(`Uploading file ${fileNumber}/${totalFiles}: ${file.name} (${formatBytes(file.size)})...`);

        const formData = new FormData();
        formData.append("files", file);

        try {
          const response = await fetch(`/ui/${ingestGraphName}/uploads?overwrite=true`, {
            method: "POST",
            headers: { Authorization: `Basic ${creds}` },
            body: formData,
          });

          if (!response.ok) {
            throw new Error(`Upload failed with status ${response.status}`);
          }

          const data = await response.json();
          if (data.status === "success") {
            uploadedCount++;
          } else {
            failedCount++;
            console.error(`File ${file.name} failed:`, data);
          }
        } catch (err) {
          console.error(`File ${file.name} error:`, err);
          failedCount++;
        }
      }

      // Show final result
      if (failedCount === 0) {
        setUploadMessage(`✅ Successfully uploaded all ${uploadedCount} files. Processing...`);
      } else {
        setUploadMessage(`⚠️ Uploaded ${uploadedCount} files successfully, ${failedCount} failed. Processing...`);
      }

      setSelectedFiles(null);
      await fetchUploadedFiles();

      // Step 2: Call create_ingest to process uploaded files
      console.log("Calling handleCreateIngestAfterUpload...");
      await handleCreateIngestAfterUpload("uploaded");
      console.log("handleCreateIngestAfterUpload completed");
    } catch (error: any) {
      console.error("Upload error:", error);
      setUploadMessage(`❌ Batch upload error: ${error.message}`);
    } finally {
      setIsUploading(false);
    }
  };

  // -------------------------
  // Delete uploaded / downloaded file handlers
  // (these already included session_id logic)
  // -------------------------
  const handleDeleteFile = async (filename: string) => {
    if (!ingestGraphName) return;

    console.log("Deleting file:", filename);
    console.log("tempSessionId:", tempSessionId);

    try {
      const creds = localStorage.getItem("creds");

      // Delete original file (backend will also delete processed content from JSONL if session_id is provided)
      const url = tempSessionId
        ? `/ui/${ingestGraphName}/uploads?filename=${encodeURIComponent(filename)}&session_id=${tempSessionId}`
        : `/ui/${ingestGraphName}/uploads?filename=${encodeURIComponent(filename)}`;

      const response = await fetch(url, {
        method: "DELETE",
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();

      setUploadMessage(`✅ ${data.message}`);
      await fetchUploadedFiles();

      // Refresh temp files list if session exists
      if (tempSessionId) {
        await fetchTempFiles(tempSessionId);
      }
    } catch (error: any) {
      console.error("Delete error:", error);
      setUploadMessage(`❌ Error: ${error.message}`);
    }
  };

  const handleDeleteAllFiles = async () => {
    if (!ingestGraphName) return;

    const shouldDelete = await confirm("Are you sure you want to delete all uploaded files?");
    if (!shouldDelete) return;

    try {
      const creds = localStorage.getItem("creds");
      const response = await fetch(`/ui/${ingestGraphName}/uploads`, {
        method: "DELETE",
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();

      // Also clear temp session
      if (tempSessionId) {
        await handleDeleteAllTempFiles();
      }

      setUploadMessage(`✅ ${data.message}`);
      await fetchUploadedFiles();
    } catch (error: any) {
      setUploadMessage(`❌ Error: ${error.message}`);
    }
  };

  // -------------------------
  // Cloud download handlers
  // -------------------------
  const fetchDownloadedFiles = async () => {
    if (!ingestGraphName) return;

    try {
      const creds = localStorage.getItem("creds");
      const response = await fetch(`/ui/${ingestGraphName}/cloud/list`, {
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();
      setDownloadedFiles(data.files || []);
    } catch (error) {
      console.error("Error fetching downloaded files:", error);
    }
  };

  const handleCloudDownload = async () => {
    if (!ingestGraphName) {
      setDownloadMessage("Please select a graph");
      return;
    }

    setIsDownloading(true);
    setDownloadMessage("Downloading files from cloud storage...");

    try {
      const creds = localStorage.getItem("creds");

      // Prepare request body based on provider
      let requestBody: any = { provider: cloudProvider };

      if (cloudProvider === "s3") {
        if (!cloudAccessKey || !cloudSecretKey || !cloudBucket || !cloudRegion) {
          setDownloadMessage("❌ Please fill in all S3 credentials");
          setIsDownloading(false);
          return;
        }
        requestBody = {
          ...requestBody,
          access_key: cloudAccessKey,
          secret_key: cloudSecretKey,
          bucket: cloudBucket,
          region: cloudRegion,
          prefix: cloudPrefix,
        };
      } else if (cloudProvider === "gcs") {
        if (!gcsProjectId || !gcsCredentials || !cloudBucket) {
          setDownloadMessage("❌ Please fill in all GCS credentials");
          setIsDownloading(false);
          return;
        }
        requestBody = {
          ...requestBody,
          project_id: gcsProjectId,
          gcs_credentials_json: gcsCredentials,
          bucket: cloudBucket,
          prefix: cloudPrefix,
        };
      } else if (cloudProvider === "azure") {
        if (!azureAccountName || !azureAccountKey || !azureContainer) {
          setDownloadMessage("❌ Please fill in all Azure credentials");
          setIsDownloading(false);
          return;
        }
        requestBody = {
          ...requestBody,
          account_name: azureAccountName,
          account_key: azureAccountKey,
          container: azureContainer,
          prefix: cloudPrefix,
        };
      }

      const response = await fetch(`/ui/${ingestGraphName}/cloud/download`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(requestBody),
      });

      const data = await response.json();
      if (data.status === "success") {
        setDownloadMessage("✅ Successfully downloaded the files. Wait for file processing");
        await fetchDownloadedFiles();
        setIsDownloading(false);

        // Step 2: Call create_ingest to process downloaded files in background
        handleCreateIngestAfterUpload("downloaded").catch((err) => {
          console.error("Error in background processing:", err);
        });
      } else if (data.status === "warning") {
        setDownloadMessage(`⚠️ ${data.message}`);
        setIsDownloading(false);
      } else {
        setDownloadMessage(`❌ ${data.message || "Download failed"}`);
        setIsDownloading(false);
      }
    } catch (error: any) {
      setDownloadMessage(`❌ Error: ${error.message}`);
      setIsDownloading(false);
    }
  };

  const handleDeleteDownloadedFile = async (filename: string) => {
    if (!ingestGraphName) return;

    try {
      const creds = localStorage.getItem("creds");

      // Delete original file (backend will also delete processed content from JSONL if session_id is provided)
      const url = tempSessionId
        ? `/ui/${ingestGraphName}/cloud/delete?filename=${encodeURIComponent(filename)}&session_id=${tempSessionId}`
        : `/ui/${ingestGraphName}/cloud/delete?filename=${encodeURIComponent(filename)}`;

      const response = await fetch(url, {
        method: "DELETE",
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();

      setDownloadMessage(`✅ ${data.message}`);
      await fetchDownloadedFiles();

      // Refresh temp files list if session exists
      if (tempSessionId) {
        await fetchTempFiles(tempSessionId);
      }
    } catch (error: any) {
      setDownloadMessage(`❌ Error: ${error.message}`);
    }
  };

  const handleDeleteAllDownloadedFiles = async () => {
    if (!ingestGraphName) return;

    const shouldDelete = await confirm("Are you sure you want to delete all downloaded files?");
    if (!shouldDelete) return;

    try {
      const creds = localStorage.getItem("creds");
      const response = await fetch(`/ui/${ingestGraphName}/cloud/delete`, {
        method: "DELETE",
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();
      setDownloadMessage(`✅ ${data.message}`);
      await fetchDownloadedFiles();
    } catch (error: any) {
      setDownloadMessage(`❌ Error: ${error.message}`);
    }
  };

  // -------------------------
  // Temp session helpers (colleague flow)
  // -------------------------
  // Fetch temp processed files (server endpoint: GET /ui/{graph}/ingestion_temp/list?session_id=)
  const fetchTempFiles = async (sessionId: string) => {
    if (!ingestGraphName || !sessionId) return;

    try {
      const creds = localStorage.getItem("creds");
      const response = await fetch(`/ui/${ingestGraphName}/ingestion_temp/list?session_id=${sessionId}`, {
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();
      if (data.status === "success" && data.sessions.length > 0) {
        setTempFiles(data.sessions[0].files || []);
        setShowTempFiles(true);
      }
    } catch (error) {
      console.error("Error fetching temp files:", error);
    }
  };

  // Delete a specific temp file
  const handleDeleteTempFile = async (filename: string) => {
    if (!ingestGraphName || !tempSessionId) return;

    try {
      const creds = localStorage.getItem("creds");
      const response = await fetch(
        `/ui/${ingestGraphName}/ingestion_temp/delete?session_id=${tempSessionId}&filename=${encodeURIComponent(filename)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Basic ${creds}` },
        }
      );
      const data = await response.json();
      if (data.status === "success") {
        setIngestMessage(`✅ ${data.message}`);
        // Refresh the temp files list
        await fetchTempFiles(tempSessionId);
      }
    } catch (error: any) {
      setIngestMessage(`❌ Error: ${error.message}`);
    }
  };

  // Delete all temp files for session
  const handleDeleteAllTempFiles = async () => {
    if (!ingestGraphName || !tempSessionId) return;

    try {
      const creds = localStorage.getItem("creds");
      const response = await fetch(
        `/ui/${ingestGraphName}/ingestion_temp/delete?session_id=${tempSessionId}`,
        {
          method: "DELETE",
          headers: { Authorization: `Basic ${creds}` },
        }
      );
      const data = await response.json();
      if (data.status === "success") {
        setIngestMessage(`✅ ${data.message}`);
        setTempFiles([]);
        setShowTempFiles(false);
        setTempSessionId(null);
      }
    } catch (error: any) {
      setIngestMessage(`❌ Error: ${error.message}`);
    }
  };

  // Delete temp files matching original filename (client convenience)
  const handleDeleteTempFilesForOriginal = async (originalFilename: string) => {
    console.log("handleDeleteTempFilesForOriginal called with:", originalFilename);

    if (!ingestGraphName || !tempSessionId) {
      console.log("No graph name or session ID, returning");
      return;
    }

    try {
      // Extract base name without extension (e.g., "document.pdf" -> "document")
      const baseName = originalFilename.replace(/\.[^/.]+$/, "");
      console.log("Base name:", baseName);

      const creds = localStorage.getItem("creds");

      // Fetch temp files to find matches
      const response = await fetch(`/ui/${ingestGraphName}/ingestion_temp/list?session_id=${tempSessionId}`, {
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();
      console.log("Temp files list response:", data);

      if (data.status === "success" && data.sessions.length > 0) {
        const files = data.sessions[0].files || [];
        console.log("All temp files:", files.map((f: any) => f.filename));

        // Find temp files matching pattern: doc_{idx}_{baseName}*.json
        const matchingFiles = files.filter((f: any) => f.filename.includes(`_${baseName}`));
        console.log("Matching files to delete:", matchingFiles.map((f: any) => f.filename));

        // Delete each matching file
        for (const file of matchingFiles) {
          console.log("Deleting temp file:", file.filename);
          const deleteResponse = await fetch(
            `/ui/${ingestGraphName}/ingestion_temp/delete?session_id=${tempSessionId}&filename=${encodeURIComponent(file.filename)}`,
            {
              method: "DELETE",
              headers: { Authorization: `Basic ${creds}` },
            }
          );
          const deleteData = await deleteResponse.json();
          console.log("Delete response:", deleteData);
        }

        console.log(`Successfully deleted ${matchingFiles.length} temp file(s)`);
      } else {
        console.log("No temp files found or empty sessions");
      }
    } catch (error: any) {
      console.error("Error deleting temp files:", error);
    }
  };

  // -------------------------
  // Ingest flows (create ingest, run ingest)
  // -------------------------
  const handleRunIngest = async () => {
    if (!ingestJobData) {
      setIngestMessage("❌ No ingest job data available");
      return;
    }

    setIsIngesting(true);
    setIngestMessage("Running final document ingest...");

    try {
      const creds = localStorage.getItem("creds");

      const loadingInfo = {
        load_job_id: ingestJobData.load_job_id,
        data_source_id: ingestJobData.data_source_id,
        file_path: ingestJobData.data_path,
      };

      const ingestResponse = await fetch(`/ui/${ingestGraphName}/ingest`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(loadingInfo),
      });

      if (!ingestResponse.ok) {
        const errorData = await ingestResponse.json();
        throw new Error(errorData.detail || `Failed to run ingest: ${ingestResponse.statusText}`);
      }

      const ingestData = await ingestResponse.json();
      console.log("Ingest response:", ingestData);

      setIngestMessage(`✅ Data ingested successfully! Processed ${tempFiles.length} documents.`);

      // Clear temp state
      setTempFiles([]);
      setShowTempFiles(false);
      setTempSessionId(null);
      setIngestJobData(null);
    } catch (error: any) {
      console.error("Error running ingest:", error);
      setIngestMessage(`❌ Error: ${error.message}`);
    } finally {
      setIsIngesting(false);
    }
  };

  // Ingest files into knowledge graph (uploaded or downloaded)
  // Creates ingest job and either shows temp session or runs ingestion directly
  const handleIngestDocuments = async (sourceType: "uploaded" | "downloaded" = "uploaded") => {
    if (!ingestGraphName) {
      setIngestMessage("Please select a graph");
      return;
    }

    const folderPath = sourceType === "uploaded" ? `uploads/${ingestGraphName}` : `downloaded_files_cloud/${ingestGraphName}`;

    setIsIngesting(true);
    setIngestMessage("Step 1/2: Creating ingest job...");

    try {
      const creds = localStorage.getItem("creds");

      // Step 1: Create ingest job
      const createIngestConfig = {
        data_source: "server",
        data_source_config: {
          data_path: folderPath,
        },
        loader_config: {},
        file_format: "multi",
      };

      const createResponse = await fetch(`/ui/${ingestGraphName}/create_ingest`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(createIngestConfig),
      });

      if (!createResponse.ok) {
        const errorData = await createResponse.json();
        throw new Error(errorData.detail || `Failed to create ingest job: ${createResponse.statusText}`);
      }

      const createData = await createResponse.json();
      console.log("Create ingest response:", createData);

      // Check if temp files were created (for server data source)
      const sessionId = createData.data_source_id?.temp_session_id;

      if (sessionId && !directIngestion) {
        // Files are saved to temp storage - show them for review (only if not direct ingestion)
        setTempSessionId(sessionId);
        setIngestJobData({
          load_job_id: createData.load_job_id,
          data_source_id: createData.data_source_id,
          data_path: createData.data_path || createData.file_path,
        });
        setIngestMessage(`✅ Processed ${createData.data_source_id.file_count} files. Review them below before ingesting.`);
        await fetchTempFiles(sessionId);
        setIsIngesting(false);
      } else {
        // No temp files (e.g., S3 Bedrock) OR direct ingestion enabled - proceed directly to ingest
        setIngestMessage("Step 2/2: Running document ingest...");

        const loadingInfo = {
          load_job_id: createData.load_job_id,
          data_source_id: createData.data_source_id,
          file_path: createData.data_path || createData.file_path,
        };

        const ingestResponse = await fetch(`/ui/${ingestGraphName}/ingest`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Basic ${creds}`,
          },
          body: JSON.stringify(loadingInfo),
        });

        if (!ingestResponse.ok) {
          const errorData = await ingestResponse.json();
          throw new Error(errorData.detail || `Failed to run ingest: ${ingestResponse.statusText}`);
        }

        const ingestData = await ingestResponse.json();
        console.log("Ingest response:", ingestData);

        setIngestMessage(`✅ Data ingested successfully! Processed documents from ${folderPath}/`);
        setIsIngesting(false);
      }
    } catch (error: any) {
      console.error("Error ingesting data:", error);
      setIngestMessage(`❌ Error: ${error.message}`);
      setIsIngesting(false);
    }
  };

  // Called automatically after upload or cloud download finishes.
  // Creates an ingest job that processes files into a temp session (JSONL) and stores session id in client.
  const handleCreateIngestAfterUpload = async (sourceType: "uploaded" | "downloaded" = "uploaded") => {
    console.log("handleCreateIngestAfterUpload called with sourceType:", sourceType);
    console.log("ingestGraphName:", ingestGraphName);

    if (!ingestGraphName) {
      console.log("No graph name, returning early");
      return;
    }

    const folderPath = sourceType === "uploaded" ? `uploads/${ingestGraphName}` : `downloaded_files_cloud/${ingestGraphName}`;

    console.log("folderPath:", folderPath);

    try {
      const creds = localStorage.getItem("creds");

      // Call create_ingest to process files
      const createIngestConfig = {
        data_source: "server",
        data_source_config: {
          data_path: folderPath,
        },
        loader_config: {},
        file_format: "multi",
      };

      console.log("Calling create_ingest with config:", createIngestConfig);

      const createResponse = await fetch(`/ui/${ingestGraphName}/create_ingest`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(createIngestConfig),
      });

      console.log("create_ingest response status:", createResponse.status);

      if (!createResponse.ok) {
        const errorData = await createResponse.json();
        console.error("create_ingest error:", errorData);
        throw new Error(errorData.detail || `Failed to create ingest job: ${createResponse.statusText}`);
      }

      const createData = await createResponse.json();
      console.log("create_ingest response data:", createData);

      const sessionId = createData.data_source_id?.temp_session_id;
      console.log("Session ID:", sessionId);

      if (sessionId) {
        // Save session ID for later ingest
        setTempSessionId(sessionId);
        setIngestJobData({
          load_job_id: createData.load_job_id,
          data_source_id: createData.data_source_id,
          data_path: createData.data_path || createData.file_path,
        });

        console.log("Direct ingestion enabled:", directIngestion);

        if (directIngestion) {
          // Direct ingestion - proceed to ingest immediately
          setUploadMessage("Running direct ingestion...");
          await handleRunIngest();
        } else {
          // Save for later - files ready for ingestion
          setUploadMessage(`✅ Successfully processed ${createData.data_source_id.file_count} files. Ready for ingestion.`);
        }
      } else {
        console.warn("No session ID returned from create_ingest");
      }
    } catch (error: any) {
      console.error("Error in create_ingest:", error);
      setUploadMessage(`❌ Processing error: ${error.message}`);
    }
  };

  // -------------------------
  // S3 / Bedrock ingest
  // Replace/merge your AmazonBDA flow with this consolidated handler
  // -------------------------
  const handleAmazonBDAIngest = async () => {
    if (!ingestGraphName) {
      setIngestMessage("Please select a graph");
      return;
    }

    // Validate inputs based on file format
    if (!awsAccessKey || !awsSecretKey) {
      setIngestMessage("❌ Please provide AWS Access Key and Secret Key");
      return;
    }

    if (fileFormat === "multi") {
      if (!inputBucket || !outputBucket || !regionName) {
        setIngestMessage("❌ Please provide Input Bucket, Output Bucket, and Region Name");
        return;
      }

      // Ask for confirmation if using Bedrock (multi format)
      const shouldProceed = await confirm(
        `You're using AWS Bedrock for multimodal document processing. This will trigger AWS Bedrock BDA to process your documents from the input bucket (${inputBucket}) and store the results in the output bucket (${outputBucket}). Please confirm to proceed.`
      );
      if (!shouldProceed) {
        setIngestMessage("Operation cancelled by user.");
        return;
      }
    } else if (fileFormat === "json") {
      if (!dataPath) {
        setIngestMessage("❌ Please provide Data Path (e.g., s3://bucket-name/path/to/data)");
        return;
      }
    }

    setIsIngesting(true);
    setIngestMessage("Step 1/2: Creating ingest job...");

    try {
      const creds = localStorage.getItem("creds");

      // Step 1: Create ingest job
      const createIngestConfig: any = {
        data_source: "s3",
        data_source_config: {
          aws_access_key: awsAccessKey,
          aws_secret_key: awsSecretKey,
        },
        loader_config: {
          doc_id_field: "doc_id",
          content_field: "content",
          doc_type: fileFormat === "multi" ? "markdown" : "",
        },
        file_format: fileFormat,
      };

      // Add format-specific configuration
      if (fileFormat === "multi") {
        createIngestConfig.data_source_config.input_bucket = inputBucket;
        createIngestConfig.data_source_config.output_bucket = outputBucket;
        createIngestConfig.data_source_config.region_name = regionName;
        setIngestMessage("Step 1/2: Creating ingest job and triggering AWS Bedrock BDA processing...");
      } else if (fileFormat === "json") {
        createIngestConfig.loader_config.doc_id_field = "url";
      }

      const createResponse = await fetch(`/ui/${ingestGraphName}/create_ingest`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(createIngestConfig),
      });

      if (!createResponse.ok) {
        const errorData = await createResponse.json();
        throw new Error(errorData.detail || `Failed to create ingest job: ${createResponse.statusText}`);
      }

      const createData = await createResponse.json();
      console.log("Create ingest response:", createData);

      // Step 2: Run ingest
      setIngestMessage("Step 2/2: Running document ingest...");

      // Determine file path based on format
      let filePath = "";
      if (fileFormat === "multi") {
        filePath = outputBucket; // For multi format, use output bucket
      } else if (fileFormat === "json") {
        filePath = dataPath; // For json format, use the provided data path
      }

      const loadingInfo = {
        load_job_id: createData.load_job_id,
        data_source_id: createData.data_source_id,
        file_path: filePath,
      };

      const ingestResponse = await fetch(`/ui/${ingestGraphName}/ingest`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify(loadingInfo),
      });

      if (!ingestResponse.ok) {
        const errorData = await ingestResponse.json();
        throw new Error(errorData.detail || `Failed to run ingest: ${ingestResponse.statusText}`);
      }

      const ingestData = await ingestResponse.json();
      console.log("Ingest response:", ingestData);

      if (fileFormat === "multi") {
        setIngestMessage(
          `✅ Data ingested successfully! AWS Bedrock BDA processed documents from ${inputBucket} and loaded results from ${outputBucket}.`
        );
      } else {
        setIngestMessage(`✅ Data ingested successfully! Processed documents from ${dataPath}.`);
      }
    } catch (error: any) {
      console.error("Error ingesting S3 data:", error);
      setIngestMessage(`❌ Error: ${error.message}`);
    } finally {
      setIsIngesting(false);
    }
  };

  // -------------------------
  // Rebuild / Refresh handlers
  // -------------------------
  const checkRebuildStatus = async (graphName: string, showLoadingMessage: boolean = false) => {
    if (!graphName) return;

    setIsCheckingStatus(true);
    if (showLoadingMessage) {
      setRefreshMessage("Checking rebuild status...");
    }

    try {
      const creds = localStorage.getItem("creds");
      const statusResponse = await fetch(`/ui/${graphName}/rebuild_status`, {
        method: "GET",
        headers: {
          Authorization: `Basic ${creds}`,
        },
      });

      if (statusResponse.ok) {
        const statusData = await statusResponse.json();
        const wasRunning = isRebuildRunning;
        const isCurrentlyRunning = statusData.is_running || false;

        setIsRebuildRunning(isCurrentlyRunning);

        if (isCurrentlyRunning) {
          const startTime = statusData.started_at ? new Date(statusData.started_at * 1000).toLocaleString() : "unknown time";
          setRefreshMessage(`⚠️ A rebuild is already in progress for "${graphName}" (started at ${startTime}). Please wait for it to complete.`);
        } else {
          // Rebuild is not running anymore
          if (wasRunning && statusData.status === "completed") {
            // Just finished
            setRefreshMessage(`✅ Rebuild completed successfully for "${graphName}".`);
          } else if (statusData.status === "failed") {
            setRefreshMessage(`❌ Previous rebuild failed: ${statusData.error || "Unknown error"}`);
          } else {
            // Clear message only if we're not showing loading message
            if (!showLoadingMessage) {
              setRefreshMessage("");
            }
          }
        }
      }
    } catch (error: any) {
      console.error("Error checking rebuild status:", error);
      // On error, assume it's safe to proceed
      setIsRebuildRunning(false);
      setRefreshMessage("");
    } finally {
      setIsCheckingStatus(false);
    }
  };

  const handleRefreshGraph = async () => {
    if (!refreshGraphName) {
      setRefreshMessage("Please select a graph");
      return;
    }

    // Double-check status one more time before submitting
    if (isRebuildRunning) {
      setRefreshMessage(`⚠️ A rebuild is already in progress. Please wait for it to complete.`);
      return;
    }

    // Ask user to confirm before proceeding with refresh
    const shouldRefresh = await confirm(
      `Are you sure you want to refresh the knowledge graph "${refreshGraphName}"? This will rebuild the graph content.`
    );
    if (!shouldRefresh) {
      setRefreshMessage("Operation cancelled by user.");
      return;
    }

    setIsRefreshing(true);
    setRefreshMessage("Submitting rebuild request...");

    try {
      const creds = localStorage.getItem("creds");

      const response = await fetch(`/ui/${refreshGraphName}/rebuild_graph`, {
        method: "POST",
        headers: {
          Authorization: `Basic ${creds}`,
        },
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || `Failed to refresh graph: ${response.statusText}`);
      }

      const data = await response.json();
      console.log("Refresh response:", data);

      setRefreshMessage(`✅ Refresh submitted successfully! The knowledge graph "${refreshGraphName}" is being rebuilt.`);
      setIsRebuildRunning(true);
    } catch (error: any) {
      console.error("Error refreshing graph:", error);
      setRefreshMessage(`❌ Error: ${error.message}`);
    } finally {
      setIsRefreshing(false);
    }
  };

  // -------------------------
  // Init effects and helpers
  // -------------------------
  useEffect(() => {
    const store = JSON.parse(localStorage.getItem("site") || "{}");
    if (store.graphs && Array.isArray(store.graphs)) {
      setAvailableGraphs(store.graphs);
      // Auto-select first graph if available
      if (store.graphs.length > 0 && !ingestGraphName) {
        setIngestGraphName(store.graphs[0]);
      }
      // Auto-select first graph for refresh as well
      if (store.graphs.length > 0 && !refreshGraphName) {
        setRefreshGraphName(store.graphs[0]);
      }
    }
  }, []);

  // Load files when ingest dialog opens or graph name changes
  useEffect(() => {
    if (ingestOpen && ingestGraphName) {
      fetchUploadedFiles();
      fetchDownloadedFiles();
    }
  }, [ingestOpen, ingestGraphName]);

  const handleInitializeGraph = async () => {
    if (!graphName.trim()) {
      setStatusMessage("Please enter a graph name");
      setStatusType("error");
      return;
    }

    setIsInitializing(true);
    setStatusMessage("Creating graph and initializing GraphRAG schema...");
    setStatusType("");

    try {
      // Get credentials from localStorage
      const creds = localStorage.getItem("creds");
      if (!creds) {
        throw new Error("Not authenticated. Please login first.");
      }

      // Step 1: Create the graph
      setStatusMessage("Step 1/2: Creating graph...");
      const createResponse = await fetch(`/ui/${graphName}/create_graph`, {
        method: "POST",
        headers: {
          Authorization: `Basic ${creds}`,
        },
      });

      const createData = await createResponse.json();

      if (!createResponse.ok) {
        throw new Error(createData.detail || createData.message || `Failed to create graph: ${createResponse.statusText}`);
      }

      if (createData.status !== "success") {
        if (createData.message && createData.message.includes("already exists")) {
          // Ask user to confirm before proceeding with initialization
          const shouldInitialize = await confirm(`Graph "${graphName}" already exists. Do you want to initialize it with GraphRAG schema?`);
          if (!shouldInitialize) {
            setStatusMessage("Operation cancelled by user.");
            setStatusType("error");
            setIsInitializing(false);
            return;
          }
        } else {
          throw new Error(createData.message || `Failed to create graph: ${createData.details}`);
        }
      }

      // Step 2: Initialize the graph with GraphRAG schema
      setStatusMessage("Step 2/2: Initializing GraphRAG schema...");
      const initResponse = await fetch(`/ui/${graphName}/initialize_graph`, {
        method: "POST",
        headers: {
          Authorization: `Basic ${creds}`,
        },
      });

      const initData = await initResponse.json();

      if (!initResponse.ok) {
        throw new Error(initData.detail || `Failed to initialize graph: ${initResponse.statusText}`);
      }

      if (initData.status !== "success") {
        setStatusMessage(initData.message || `Failed to initialize graph: ${initData.details}`);
        setStatusType("error");
        setIsInitializing(false);
        return;
      }

      setStatusMessage(`✅ Graph "${graphName}" created and initialized successfully! You can now close this dialog.`);
      setStatusType("success");

      // Add the new graph to the available graphs list
      const newGraph = graphName;
      setAvailableGraphs((prev) => {
        if (!prev.includes(newGraph)) {
          const updated = [...prev, newGraph];
          // Update localStorage as well
          const store = JSON.parse(localStorage.getItem("site") || "{}");
          store.graphs = updated;
          localStorage.setItem("site", JSON.stringify(store));
          return updated;
        }
        return prev;
      });

      // Set the newly created graph as selected for ingestion
      setIngestGraphName(graphName);
      setRefreshGraphName(graphName);
      setGraphName("");
    } catch (error: any) {
      console.error("Error creating graph:", error);
      setStatusMessage(`❌ Error: ${error.message}`);
      setStatusType("error");
    } finally {
      setIsInitializing(false);
    }
  };

  // Check rebuild status when graph selection or dialog state changes
  useEffect(() => {
    if (refreshOpen && refreshGraphName) {
      // Check status immediately when dialog opens
      checkRebuildStatus(refreshGraphName, true);

      // Set up polling to check status every 5 seconds while dialog is open
      const intervalId = setInterval(() => {
        checkRebuildStatus(refreshGraphName, false);
      }, 5000);

      return () => clearInterval(intervalId);
    }
  }, [refreshOpen, refreshGraphName]);

  // -------------------------
  // Render
  // -------------------------
  return (
    <div className="h-[100vh] w-full bg-white dark:bg-background p-8">
      <div className="max-w-7xl mx-auto">
        <div className="mb-10">
          <Button variant="outline" onClick={() => navigate("/chat")} className="mb-4 dark:border-[#3D3D3D]">
            <ArrowLeft className="h-4 w-4 mr-2" />
            Back to Chat
          </Button>
          <h1 className="text-2xl font-bold mb-2 text-black dark:text-white">Knowledge Graph Administration</h1>
          <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">Configure and manage your knowledge graphs</p>
        </div>

        {/* Three cards displayed horizontally */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Section 1: Initialize Knowledge Graph */}
          <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
            <div className="mb-4">
              <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
                <Database className="h-6 w-6 text-tigerOrange" />
              </div>
              <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">Initialize Knowledge Graph</h2>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">Create the knowledge graph schema and queries for future document ingestion.</p>
            </div>
            <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
              <Button className="gradient w-full text-white" onClick={() => setInitializeGraphOpen(true)}>
                <Database className="h-4 w-4 mr-2" />
                Initialize Graph
              </Button>
            </div>
          </div>

          {/* Section 2: Data Ingest for a KG */}
          <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
            <div className="mb-4">
              <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
                <Upload className="h-6 w-6 text-tigerOrange" />
              </div>
              <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">Ingest to Knowledge Graph</h2>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">Upload and ingest documents into your knowledge graph for future content processing.</p>
            </div>
            <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
              <Button className="gradient w-full text-white" onClick={() => setIngestOpen(true)}>
                <Upload className="h-4 w-4 mr-2" />
                Ingest Document
              </Button>
            </div>
          </div>

          {/* Section 3: Refresh Knowledge Graph */}
          <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 bg-white dark:bg-shadeA flex flex-col h-full">
            <div className="mb-4">
              <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center mb-4">
                <RefreshCw className="h-6 w-6 text-tigerOrange" />
              </div>
              <h2 className="text-lg font-semibold mb-2 text-black dark:text-white">Refresh Knowledge Graph</h2>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9] mb-4">Process new documents in your knowledge graph to refresh its content.</p>
            </div>
            <div className="mt-auto pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
              <Button className="gradient w-full text-white" onClick={() => setRefreshOpen(true)}>
                <RefreshCw className="h-4 w-4 mr-2" />
                Refresh Graph
              </Button>
            </div>
          </div>
        </div>

        {/* Initialize Graph Dialog */}
        <Dialog
          open={initializeGraphOpen}
          onOpenChange={(open) => {
            // Prevent closing if confirm dialog is open
            if (!open && isConfirmDialogOpen) {
              return;
            }
            setInitializeGraphOpen(open);
          }}
        >
          <DialogContent className="sm:max-w-[500px] bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]" onInteractOutside={(e) => e.preventDefault()}>
            <DialogHeader>
              <DialogTitle className="text-black dark:text-white">Initialize Knowledge Graph</DialogTitle>
              <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
                Enter the name of your knowledge graph. The system will create it if necessary and initialize it with the GraphRAG schema.
              </DialogDescription>
            </DialogHeader>

            <div className="py-4">
              <div className="mb-4">
                <label className="block text-sm font-medium mb-2 text-black dark:text-white">Knowledge Graph Name</label>
                <Input
                  placeholder="e.g., MyKnowledgeGraph"
                  value={graphName}
                  onChange={(e) => setGraphName(e.target.value)}
                  disabled={isInitializing}
                  className="dark:border-[#3D3D3D] dark:bg-shadeA"
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !isInitializing) {
                      handleInitializeGraph();
                    }
                  }}
                />
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
              {statusType === "success" ? (
                <Button
                  className="gradient text-white w-full"
                  onClick={() => {
                    setInitializeGraphOpen(false);
                    setGraphName("");
                    setStatusMessage("");
                    setStatusType("");
                    // No need to reload - graph list updates dynamically!
                  }}
                >
                  Done
                </Button>
              ) : (
                <>
                  <Button
                    variant="outline"
                    onClick={() => {
                      setInitializeGraphOpen(false);
                      setGraphName("");
                      setStatusMessage("");
                      setStatusType("");
                    }}
                    disabled={isInitializing}
                    className="dark:border-[#3D3D3D]"
                  >
                    Cancel
                  </Button>
                  <Button onClick={handleInitializeGraph} disabled={isInitializing || !graphName.trim()} className="gradient text-white">
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

        {/* Data Ingest Dialog */}
        <Dialog
          open={ingestOpen}
          onOpenChange={(open) => {
            // Prevent closing if confirm dialog is open
            if (!open && isConfirmDialogOpen) {
              return;
            }
            setIngestOpen(open);
          }}
        >
          <DialogContent className="sm:max-w-[700px] bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D] max-h-[80vh] overflow-y-auto" onInteractOutside={(e) => e.preventDefault()}>
            <DialogHeader>
              <DialogTitle className="text-black dark:text-white">Document Ingestion for Knowledge Graph</DialogTitle>
              <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">
                Upload files locally, download from cloud storage, or configure Amazon Bedrock Data Automation for document ingestion
              </DialogDescription>
            </DialogHeader>

            {/* Graph Name Selection */}
            <div className="mb-4">
              <label className="block text-sm font-medium mb-2 text-black dark:text-white">Target Graph Name</label>
              <Select value={ingestGraphName} onValueChange={setIngestGraphName} disabled={isIngesting}>
                <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-shadeA" disabled={isIngesting}>
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

            <Tabs value={activeTab} onValueChange={(value) => {
              if (!isIngesting) {
                setActiveTab(value);
              }
            }} className="w-full">
              <TabsList className="grid w-full grid-cols-3">
                <TabsTrigger value="upload" disabled={isIngesting}>
                  <FolderUp className="h-4 w-4 mr-2" />
                  Upload Files
                </TabsTrigger>
                <TabsTrigger value="cloudDownload" disabled={isIngesting}>
                  <CloudDownload className="h-4 w-4 mr-2" />
                  Download from Cloud
                </TabsTrigger>
                <TabsTrigger value="s3" disabled={isIngesting}>
                  <CloudCog className="h-4 w-4 mr-2" />
                  Amazon BDA
                </TabsTrigger>
              </TabsList>

              {/* Upload Data Tab */}
              <TabsContent value="upload" className="space-y-4">
                <div className="space-y-4">
                  <p className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Upload local files to the server and ingest them into your knowledge graph.</p>
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">Select Files</label>
                    <Input type="file" multiple onChange={(e) => setSelectedFiles(e.target.files)} disabled={isUploading} className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">Maximum upload per request: {MAX_UPLOAD_SIZE_MB} MB. {ingestGraphName ? `Upload destination: uploads/${ingestGraphName}/` : ""}</p>
                  </div>

                  {/* Direct Ingestion Checkbox */}
                  <div className="flex items-center mt-3 mb-2">
                    <input
                      type="checkbox"
                      id="directIngestion"
                      checked={directIngestion}
                      onChange={(e) => setDirectIngestion(e.target.checked)}
                      className="mr-2 h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                    />
                    <label htmlFor="directIngestion" className="text-sm text-gray-700 dark:text-gray-300">
                      Direct Ingestion (upload + process + ingest all at once)
                    </label>
                  </div>

                  <div className="flex gap-2">
                    <Button onClick={handleUploadFiles} disabled={isUploading || !selectedFiles} className="gradient text-white">
                      {isUploading ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Uploading...
                        </>
                      ) : (
                        <>
                          <Upload className="h-4 w-4 mr-2" />
                          Upload Files
                        </>
                      )}
                    </Button>

                    {uploadedFiles.length > 0 && (
                      <Button onClick={handleDeleteAllFiles} variant="outline" className="dark:border-[#3D3D3D]">
                        <Trash2 className="h-4 w-4 mr-2" />
                        Delete All
                      </Button>
                    )}
                  </div>

                  {uploadMessage && <div className="p-3 rounded-lg text-sm bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300">{uploadMessage}</div>}

                  {/* Ingest Data Section */}
                  {uploadedFiles.length > 0 && (
                    <div className="border-t border-gray-300 dark:border-[#3D3D3D] pt-4 mt-4">
                      <h3 className="text-sm font-medium mb-2 text-black dark:text-white">Ingest Documents into Knowledge Graph</h3>
                      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">Process uploaded files and add them to the knowledge graph</p>
                      <Button onClick={handleRunIngest} disabled={isIngesting || !tempSessionId} className="gradient text-white w-full">
                        {isIngesting ? (
                          <>
                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                            Ingesting...
                          </>
                        ) : (
                          <>
                            <Database className="h-4 w-4 mr-2" />
                            Ingest Documents into {ingestGraphName}
                          </>
                        )}
                      </Button>
                      {ingestMessage && (
                        <div className={`p-3 rounded-lg text-sm mt-3 ${
                          ingestMessage.includes("✅")
                            ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                            : ingestMessage.includes("❌")
                            ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                            : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                        }`}>{ingestMessage}</div>
                      )}
                    </div>
                  )}

                  {/* Uploaded Files List */}
                  {uploadedFiles.length > 0 && (
                    <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-4">
                      <h3 className="text-sm font-medium mb-3 text-black dark:text-white">Uploaded Files ({uploadedFiles.length})</h3>
                      <div className="space-y-2 max-h-48 overflow-y-auto">
                        {uploadedFiles.map((file, index) => (
                          <div key={index} className="flex items-center justify-between p-2 bg-gray-50 dark:bg-shadeA rounded">
                            <span className="text-sm text-black dark:text-white truncate flex-1">{file.filename}</span>
                            <Button onClick={() => handleDeleteFile(file.filename)} variant="outline" size="sm" className="ml-2 dark:border-[#3D3D3D]">
                              <Trash2 className="h-3 w-3" />
                            </Button>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </TabsContent>

              {/* Download from Cloud Storage Tab */}
              <TabsContent value="cloudDownload" className="space-y-4">
                <div className="space-y-4">
                  <p className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Download files from cloud storage and ingest them into your knowledge graph.</p>
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">Cloud Storage Provider</label>
                    <Select value={cloudProvider} onValueChange={(value: "s3" | "gcs" | "azure") => setCloudProvider(value)}>
                      <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-shadeA">
                        <SelectValue placeholder="Select cloud provider" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="s3">Amazon S3</SelectItem>
                        <SelectItem value="gcs">Google Cloud Storage</SelectItem>
                        <SelectItem value="azure">Azure Blob Storage</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {/* Amazon S3 */}
                  {cloudProvider === "s3" && (
                    <>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">AWS Access Key</label>
                        <Input type="text" value={cloudAccessKey} onChange={(e) => setCloudAccessKey(e.target.value)} placeholder="Enter AWS access key" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">AWS Secret Key</label>
                        <Input type="password" value={cloudSecretKey} onChange={(e) => setCloudSecretKey(e.target.value)} placeholder="Enter AWS secret key" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">S3 Bucket Name</label>
                        <Input type="text" value={cloudBucket} onChange={(e) => setCloudBucket(e.target.value)} placeholder="my-bucket-name" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Region</label>
                        <Input type="text" value={cloudRegion} onChange={(e) => setCloudRegion(e.target.value)} placeholder="us-east-1" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Prefix/Path (Optional)</label>
                        <Input type="text" value={cloudPrefix} onChange={(e) => setCloudPrefix(e.target.value)} placeholder="folder/subfolder/" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                    </>
                  )}

                  {/* Google Cloud Storage */}
                  {cloudProvider === "gcs" && (
                    <>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Project ID</label>
                        <Input type="text" value={gcsProjectId} onChange={(e) => setGcsProjectId(e.target.value)} placeholder="my-project-id" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Service Account JSON</label>
                        <textarea value={gcsCredentials} onChange={(e) => setGcsCredentials(e.target.value)} placeholder='{"type": "service_account", ...}' rows={4} className="w-full p-2 rounded border dark:border-[#3D3D3D] dark:bg-shadeA text-sm" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Bucket Name</label>
                        <Input type="text" value={cloudBucket} onChange={(e) => setCloudBucket(e.target.value)} placeholder="my-gcs-bucket" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Prefix/Path (Optional)</label>
                        <Input type="text" value={cloudPrefix} onChange={(e) => setCloudPrefix(e.target.value)} placeholder="folder/subfolder/" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                    </>
                  )}

                  {/* Azure Blob Storage */}
                  {cloudProvider === "azure" && (
                    <>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Storage Account Name</label>
                        <Input type="text" value={azureAccountName} onChange={(e) => setAzureAccountName(e.target.value)} placeholder="mystorageaccount" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Account Key</label>
                        <Input type="password" value={azureAccountKey} onChange={(e) => setAzureAccountKey(e.target.value)} placeholder="Enter account key" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Container Name</label>
                        <Input type="text" value={azureContainer} onChange={(e) => setAzureContainer(e.target.value)} placeholder="my-container" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Prefix/Path (Optional)</label>
                        <Input type="text" value={cloudPrefix} onChange={(e) => setCloudPrefix(e.target.value)} placeholder="folder/subfolder/" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                    </>
                  )}

                  <div className="pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
                    <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">Download destination: downloaded_files_cloud/{ingestGraphName}/</p>
                    <Button onClick={handleCloudDownload} disabled={isDownloading} className="gradient text-white w-full">
                      {isDownloading ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Downloading...
                        </>
                      ) : (
                        <>
                          <CloudDownload className="h-4 w-4 mr-2" />
                          Download Files from {cloudProvider === "s3" ? "S3" : cloudProvider === "gcs" ? "GCS" : "Azure"}
                        </>
                      )}
                    </Button>
                  </div>

                  {downloadMessage && (
                    <div className={`p-3 rounded-lg text-sm mt-3 ${
                      downloadMessage.includes("✅")
                        ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                        : downloadMessage.includes("❌")
                        ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                        : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                    }`}>{downloadMessage}</div>
                  )}

                  {/* Downloaded Files List */}
                  {downloadedFiles.length > 0 && (
                    <div className="mt-4 border-t border-gray-300 dark:border-[#3D3D3D] pt-4">
                      <div className="flex justify-between items-center mb-2">
                        <h4 className="text-sm font-medium text-black dark:text-white">Downloaded Files ({downloadedFiles.length})</h4>
                        <Button onClick={handleDeleteAllDownloadedFiles} variant="outline" size="sm" className="dark:border-[#3D3D3D]">
                          <Trash2 className="h-3 w-3 mr-1" />
                          Delete All
                        </Button>
                      </div>
                      <ul className="space-y-2 max-h-40 overflow-y-auto">
                        {downloadedFiles.map((file, index) => (
                          <li key={index} className="flex justify-between items-center p-2 bg-gray-50 dark:bg-shadeA rounded text-sm">
                            <span className="text-black dark:text-white truncate flex-1">{file.name}</span>
                            <Button onClick={() => handleDeleteDownloadedFile(file.name)} variant="ghost" size="sm" className="ml-2 text-red-500 hover:text-red-700 dark:hover:text-red-400">
                              <Trash2 className="h-3 w-3" />
                            </Button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* Ingest Downloaded Data Section */}
                  {downloadedFiles.length > 0 && (
                    <div className="border-t border-gray-300 dark:border-[#3D3D3D] pt-4 mt-4">
                      <h3 className="text-sm font-medium mb-2 text-black dark:text-white">Ingest Documents into Knowledge Graph</h3>
                      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">Process downloaded files and add them to the knowledge graph</p>

                      <Button onClick={handleRunIngest} disabled={isIngesting || !tempSessionId} className="gradient text-white w-full">
                        {isIngesting ? (
                          <>
                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                            Ingesting...
                          </>
                        ) : (
                          <>
                            <Database className="h-4 w-4 mr-2" />
                            Ingest Documents into {ingestGraphName}
                          </>
                        )}
                      </Button>
                      {ingestMessage && (
                        <div className={`p-3 rounded-lg text-sm mt-3 ${
                          ingestMessage.includes("✅")
                            ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                            : ingestMessage.includes("❌")
                            ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                            : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                        }`}>{ingestMessage}</div>
                      )}
                    </div>
                  )}
                </div>
              </TabsContent>

              {/* S3 / Bedrock Tab */}
              <TabsContent value="s3" className="space-y-4">
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">File Format</label>
                    <Select value={fileFormat} onValueChange={(value: "json" | "multi") => setFileFormat(value)}>
                      <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-shadeA">
                        <SelectValue placeholder="Select file format" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="json">JSON</SelectItem>
                        <SelectItem value="multi">Multi</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {/* Common fields */}
                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">AWS Access Key</label>
                    <Input type="text" value={awsAccessKey} onChange={(e) => setAwsAccessKey(e.target.value)} placeholder="Enter AWS access key" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                  </div>

                  <div>
                    <label className="block text-sm font-medium mb-2 text-black dark:text-white">AWS Secret Key</label>
                    <Input type="password" value={awsSecretKey} onChange={(e) => setAwsSecretKey(e.target.value)} placeholder="Enter AWS secret key" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                  </div>

                  {/* Conditional fields based on file format */}
                  {fileFormat === "json" ? (
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">Data Path</label>
                      <Input type="text" value={dataPath} onChange={(e) => setDataPath(e.target.value)} placeholder="s3://bucket-name/path/to/data" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                    </div>
                  ) : (
                    <>
                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Input Bucket</label>
                        <Input type="text" value={inputBucket} onChange={(e) => setInputBucket(e.target.value)} placeholder="Enter input bucket name" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>

                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Output Bucket</label>
                        <Input type="text" value={outputBucket} onChange={(e) => setOutputBucket(e.target.value)} placeholder="Enter output bucket name" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>

                      <div>
                        <label className="block text-sm font-medium mb-2 text-black dark:text-white">Region Name</label>
                        <Input type="text" value={regionName} onChange={(e) => setRegionName(e.target.value)} placeholder="e.g., us-east-1" className="dark:border-[#3D3D3D] dark:bg-shadeA" />
                      </div>
                    </>
                  )}

                  {/* Ingest S3 Bedrock Data Section */}
                  <div className="border-t border-gray-300 dark:border-[#3D3D3D] pt-4 mt-4">
                    <h3 className="text-sm font-medium mb-2 text-black dark:text-white">Ingest S3 Data into Knowledge Graph</h3>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">Process S3 data and add it to the knowledge graph using AWS Bedrock BDA for multimodal documents</p>
                    <Button onClick={handleAmazonBDAIngest} disabled={isIngesting} className="gradient text-white w-full">
                      {isIngesting ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Ingesting...
                        </>
                      ) : (
                        <>
                          <Database className="h-4 w-4 mr-2" />
                          Ingest from S3 into {ingestGraphName}
                        </>
                      )}
                    </Button>
                    {ingestMessage && (
                      <div className={`p-3 rounded-lg text-sm mt-3 ${
                        ingestMessage.includes("✅")
                          ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                          : ingestMessage.includes("❌")
                          ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                          : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                      }`}>{ingestMessage}</div>
                    )}
                  </div>
                </div>
              </TabsContent>
            </Tabs>

            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => {
                  setIngestOpen(false);
                  setUploadMessage("");
                }}
                className="dark:border-[#3D3D3D]"
              >
                Close
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Refresh Graph Dialog */}
        <Dialog
          open={refreshOpen}
          onOpenChange={(open) => {
            // Prevent closing if confirm dialog is open
            if (!open && isConfirmDialogOpen) {
              return;
            }
            setRefreshOpen(open);
          }}
        >
          <DialogContent className="sm:max-w-[500px] bg-white dark:bg-background border-gray-300 dark:border-[#3D3D3D]" onInteractOutside={(e) => e.preventDefault()}>
            <DialogHeader>
              <DialogTitle className="text-black dark:text-white">Refresh Knowledge Graph</DialogTitle>
              <DialogDescription className="text-gray-600 dark:text-[#D9D9D9]">Rebuild the graph content of your knowledge graph</DialogDescription>
            </DialogHeader>

            <div className="py-4 space-y-4">
              <div>
                <label className="block text-sm font-medium mb-2 text-black dark:text-white">Select Graph to Refresh</label>
                <Select value={refreshGraphName} onValueChange={setRefreshGraphName}>
                  <SelectTrigger className="dark:border-[#3D3D3D] dark:bg-shadeA">
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
                <p className="text-sm text-yellow-800 dark:text-yellow-200 font-medium">⚠️ Warning</p>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">This operation will rebuild the graph content that will interrupt related queries. Please confirm to proceed.</p>
              </div>

              {refreshMessage && (
                <div className={`p-3 rounded-lg text-sm ${
                  refreshMessage.includes("✅")
                    ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                    : refreshMessage.includes("❌")
                    ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                    : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                }`}>{refreshMessage}</div>
              )}
            </div>

            <DialogFooter>
              <Button variant="outline" onClick={() => { setRefreshOpen(false); }} disabled={isRefreshing} className="dark:border-[#3D3D3D]">Close</Button>
              <Button onClick={handleRefreshGraph} disabled={isRefreshing || !refreshGraphName || isRebuildRunning || isCheckingStatus} className="gradient text-white">
                {isRefreshing ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Submitting...
                  </>
                ) : isCheckingStatus ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Checking Status...
                  </>
                ) : isRebuildRunning ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Rebuild In Progress...
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

        {/* User Confirmation Dialog */}
        {confirmDialog}
      </div>
    </div>
  );
};

export default Setup;
