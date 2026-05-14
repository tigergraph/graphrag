import React, { useState, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Upload,
  Loader2,
  Trash2,
  FolderUp,
  CloudDownload,
  CloudLightning,
  Database,
} from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useConfirm } from "@/hooks/useConfirm";
import { pingIdleTimer } from "@/hooks/useIdleTimeout";

interface IngestGraphProps {
  isModal?: boolean;
}

const DEFAULT_MAX_UPLOAD_SIZE_MB = 100;
const envUploadLimit = Number(import.meta.env.VITE_MAX_UPLOAD_SIZE_MB);
const MAX_UPLOAD_SIZE_MB =
  Number.isFinite(envUploadLimit) && envUploadLimit > 0
    ? envUploadLimit
    : DEFAULT_MAX_UPLOAD_SIZE_MB;
const MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024;

const formatBytes = (bytes: number) => {
  if (bytes === 0) return "0 Bytes";
  const units = ["Bytes", "KB", "MB", "GB", "TB"];
  const exponent = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / Math.pow(1024, exponent);
  const rounded =
    value >= 10 || exponent === 0 ? value.toFixed(0) : value.toFixed(1);
  return `${rounded} ${units[exponent]}`;
};

const IngestGraph: React.FC<IngestGraphProps> = ({ isModal = false }) => {
  const [confirm, confirmDialog] = useConfirm();
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);
  // Seed from the shared ``selectedGraph`` so the dropdown matches
  // whatever was last picked elsewhere (KGAdmin refresh, Bot, etc.).
  const [ingestGraphName, setIngestGraphName] = useState(
    sessionStorage.getItem("selectedGraph") || ""
  );
  const [selectedFiles, setSelectedFiles] = useState<FileList | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadedFiles, setUploadedFiles] = useState<any[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadMessage, setUploadMessage] = useState("");
  const [isProcessingFiles, setIsProcessingFiles] = useState(false);
  const [isIngesting, setIsIngesting] = useState(false);
  const [ingestMessage, setIngestMessage] = useState("");
  const [ingestJobData, setIngestJobData] = useState<any>(null);
  const [directIngestion, setDirectIngestion] = useState(false);
  const [activeTab, setActiveTab] = useState("upload");

  // S3 state
  const [awsAccessKey, setAwsAccessKey] = useState("");
  const [awsSecretKey, setAwsSecretKey] = useState("");
  const [inputBucket, setInputBucket] = useState("");
  const [outputBucket, setOutputBucket] = useState("");
  const [regionName, setRegionName] = useState("");
  const [skipBDAProcessing, setSkipBDAProcessing] = useState(false);

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

  // Fetch uploaded files
  const fetchUploadedFiles = async () => {
    if (!ingestGraphName) return;

    try {
      const creds = sessionStorage.getItem("creds");
      const response = await fetch(`/ui/${ingestGraphName}/uploads/list`, {
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();
      setUploadedFiles(data.files || []);
    } catch (error) {
      console.error("Error fetching files:", error);
    }
  };

  // Upload files
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
    const oversizedFiles = filesArray.filter(
      (file) => file.size > MAX_UPLOAD_SIZE_BYTES
    );
    if (oversizedFiles.length > 0) {
      const names = oversizedFiles
        .map((file) => `${file.name} (${formatBytes(file.size)})`)
        .join(", ");
      setUploadMessage(
        `❌ ${names} ${
          oversizedFiles.length === 1 ? "exceeds" : "exceed"
        } the ${MAX_UPLOAD_SIZE_MB} MB limit per file. ` +
          `Please split or compress ${
            oversizedFiles.length === 1 ? "this file" : "these files"
          }.`
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
    setIngestMessage("");

    try {
      const creds = sessionStorage.getItem("creds");
      const formData = new FormData();
      filesArray.forEach((file) => formData.append("files", file));

      const response = await fetch(
        `/ui/${ingestGraphName}/uploads?overwrite=true`,
        {
          method: "POST",
          headers: { Authorization: `Basic ${creds}` },
          body: formData,
        }
      );

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || `Upload failed: ${response.statusText}`);
      }

      const data = await response.json();
      if (data.status === "success") {
        const uploadedCount = selectedFiles?.length || 0;
        setUploadMessage("✅ Successfully uploaded the files. Processing...");
        setSelectedFiles(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
        await fetchUploadedFiles();
        setIsUploading(false);

        console.log("Calling handleCreateIngestAfterUpload from main upload...");
        setIsProcessingFiles(true);
        handleCreateIngestAfterUpload("uploaded", uploadedCount).catch((err) => {
          console.error("Error in background processing:", err);
          setUploadMessage(`❌ Processing error: ${err.message}`);
        }).finally(() => setIsProcessingFiles(false));
      } else {
        setUploadMessage(`⚠️ ${data.message}`);
        setIsUploading(false);
      }
    } catch (error: any) {
      console.error("Upload error:", error);
      const isLockConflict = error.message?.includes("currently being processed");
      setUploadMessage(isLockConflict ? `⚠️ ${error.message}` : `❌ Error: ${error.message}`);
      setIsUploading(false);
    }
  };

  // Handle batch upload when total size exceeds limit - upload one file at a time
  const handleBatchUpload = async (filesArray: File[]) => {
    setIsUploading(true);
    setUploadMessage("Total size exceeds limit. Uploading files one by one...");
    setIngestMessage("");

    try {
      const creds = sessionStorage.getItem("creds");
      let uploadedCount = 0;
      let failedCount = 0;
      const totalFiles = filesArray.length;

      // Upload files one at a time to avoid 413 errors
      for (let i = 0; i < filesArray.length; i++) {
        const file = filesArray[i];
        const fileNumber = i + 1;

        setUploadMessage(
          `Uploading file ${fileNumber}/${totalFiles}: ${file.name} (${formatBytes(
            file.size
          )})...`
        );

        const formData = new FormData();
        formData.append("files", file);

        try {
          const response = await fetch(
            `/ui/${ingestGraphName}/uploads?overwrite=true`,
            {
              method: "POST",
              headers: { Authorization: `Basic ${creds}` },
              body: formData,
            }
          );

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
      if (fileInputRef.current) fileInputRef.current.value = "";
      await fetchUploadedFiles();

      console.log("Calling handleCreateIngestAfterUpload...");
      setIsProcessingFiles(true);
      try {
        await handleCreateIngestAfterUpload("uploaded", uploadedCount);
        console.log("handleCreateIngestAfterUpload completed");
      } finally {
        setIsProcessingFiles(false);
      }
    } catch (error: any) {
      console.error("Upload error:", error);
      setUploadMessage(`❌ Batch upload error: ${error.message}`);
    } finally {
      setIsUploading(false);
    }
  };

  // Delete a specific file
  const handleDeleteFile = async (filename: string) => {
    if (!ingestGraphName) return;

    try {
      const creds = sessionStorage.getItem("creds");
      const response = await fetch(
        `/ui/${ingestGraphName}/uploads?filename=${encodeURIComponent(filename)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Basic ${creds}` },
        }
      );
      const data = await response.json();
      setUploadMessage(`✅ ${data.message}`);
      await fetchUploadedFiles();
    } catch (error: any) {
      setUploadMessage(`❌ Error: ${error.message}`);
    }
  };

  // Delete all files
  const handleDeleteAllFiles = async () => {
    if (!ingestGraphName) return;

    const shouldDelete = await confirm(
      "Are you sure you want to delete all uploaded files?"
    );
    if (!shouldDelete) return;

    try {
      const creds = sessionStorage.getItem("creds");
      const response = await fetch(`/ui/${ingestGraphName}/uploads`, {
        method: "DELETE",
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();
      setUploadMessage(`✅ ${data.message}`);
      await fetchUploadedFiles();
    } catch (error: any) {
      setUploadMessage(`❌ Error: ${error.message}`);
    }
  };

  // Fetch downloaded files from cloud
  const fetchDownloadedFiles = async () => {
    if (!ingestGraphName) return;

    try {
      const creds = sessionStorage.getItem("creds");
      const response = await fetch(`/ui/${ingestGraphName}/cloud/list`, {
        headers: { Authorization: `Basic ${creds}` },
      });
      const data = await response.json();
      setDownloadedFiles(data.files || []);
    } catch (error) {
      console.error("Error fetching downloaded files:", error);
    }
  };

  // Handle cloud download
  const handleCloudDownload = async () => {
    if (!ingestGraphName) {
      setDownloadMessage("Please select a graph");
      return;
    }

    setIsDownloading(true);
    setDownloadMessage("Downloading files from cloud storage...");

    try {
      const creds = sessionStorage.getItem("creds");

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

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || `Download failed: ${response.statusText}`);
      }
      const data = await response.json();
      if (data.status === "success") {
        const downloadCount = data.downloaded_files?.length || downloadedFiles.length;
        setDownloadMessage("✅ Successfully downloaded the files. Processing...");
        await fetchDownloadedFiles();
        setIsDownloading(false);
        setIsProcessingFiles(true);
        handleCreateIngestAfterUpload("downloaded", downloadCount).catch((err) => {
          console.error("Error in background processing:", err);
          setDownloadMessage(`❌ Processing error: ${err.message}`);
        }).finally(() => setIsProcessingFiles(false));
      } else if (data.status === "warning") {
        setDownloadMessage(`⚠️ ${data.message}`);
        setIsDownloading(false);
      } else {
        setDownloadMessage(`❌ ${data.message || "Download failed"}`);
        setIsDownloading(false);
      }
    } catch (error: any) {
      const isLockConflict = error.message?.includes("currently being processed");
      setDownloadMessage(isLockConflict ? `⚠️ ${error.message}` : `❌ Error: ${error.message}`);
    } finally {
      setIsDownloading(false);
    }
  };

  // Delete a specific downloaded file
  const handleDeleteDownloadedFile = async (filename: string) => {
    if (!ingestGraphName) return;

    try {
      const creds = sessionStorage.getItem("creds");
      const response = await fetch(
        `/ui/${ingestGraphName}/cloud/delete?filename=${encodeURIComponent(
          filename
        )}`,
        {
          method: "DELETE",
          headers: { Authorization: `Basic ${creds}` },
        }
      );
      const data = await response.json();
      setDownloadMessage(`✅ ${data.message}`);
      await fetchDownloadedFiles();
    } catch (error: any) {
      setDownloadMessage(`❌ Error: ${error.message}`);
    }
  };

  // Delete all downloaded files
  const handleDeleteAllDownloadedFiles = async () => {
    if (!ingestGraphName) return;

    const shouldDelete = await confirm(
      "Are you sure you want to delete all downloaded files?"
    );
    if (!shouldDelete) return;

    try {
      const creds = sessionStorage.getItem("creds");
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

  const handleRunIngest = async (sourceType: "uploaded" | "downloaded" = "uploaded") => {
    if (!ingestGraphName) {
      setIngestMessage("❌ Please select a graph");
      return;
    }
    setIsIngesting(true);
    setIngestMessage("Ingesting documents into knowledge graph...");
    try {
      const creds = sessionStorage.getItem("creds");
      const folderPath = sourceType === "uploaded" ? `uploads/${ingestGraphName}` : `downloaded_files_cloud/${ingestGraphName}`;

      // If no cached job from a prior create_ingest, run it now. The
      // backend's /ingest endpoint expects the data_source_id dict
      // shape that create_ingest emits (with the resolved JSONL temp
      // folder at top-level ``data_path``); building a fallback in
      // the UI loses that contract.
      let jobData = ingestJobData;
      if (!jobData) {
        setIngestMessage("Step 1/2: Preparing ingest job...");
        const createResp = await fetch(
          `/ui/${ingestGraphName}/create_ingest`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Basic ${creds}`,
            },
            body: JSON.stringify({
              data_source: "server",
              data_source_config: { data_path: folderPath },
              loader_config: {},
              file_format: "multi",
            }),
          }
        );
        if (!createResp.ok) {
          const err = await createResp.json();
          throw new Error(
            err.detail || `Failed to create ingest job: ${createResp.statusText}`
          );
        }
        const createData = await createResp.json();
        jobData = {
          load_job_id: createData.load_job_id,
          data_source_id: createData.data_source_id,
          data_path: createData.data_path || folderPath,
        };
        setIngestJobData(jobData);
        setIngestMessage("Step 2/2: Loading documents into knowledge graph...");
      }

      const ingestResponse = await fetch(`/ui/${ingestGraphName}/ingest`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${creds}`,
        },
        body: JSON.stringify({
          load_job_id: jobData.load_job_id,
          data_source_id: jobData.data_source_id,
          file_path: jobData.data_path,
        }),
      });

      if (!ingestResponse.ok) {
        const errorData = await ingestResponse.json();
        throw new Error(errorData.detail || `Failed to ingest: ${ingestResponse.statusText}`);
      }

      const ingestData = await ingestResponse.json();
      console.log("Ingest response:", ingestData);

      setIngestMessage(`✅ Ingestion completed successfully!`);
      setUploadMessage("");
    } catch (error: any) {
      console.error("Error during ingestion:", error);
      const isRebuildConflict = error.message?.includes("currently being rebuilt");
      setIngestMessage(isRebuildConflict ? `⚠️ ${error.message}` : `❌ Error: ${error.message}`);
    } finally {
      setIsIngesting(false);
    }
  };

  // Ingest files into knowledge graph (uploaded or downloaded)
  const handleIngestDocuments = async (sourceType: "uploaded" | "downloaded" = "uploaded") => {
    if (!ingestGraphName) {
      setIngestMessage("Please select a graph");
      return;
    }

    const folderPath = sourceType === "uploaded" ? `uploads/${ingestGraphName}` : `downloaded_files_cloud/${ingestGraphName}`;
    const fileCount = sourceType === "uploaded" ? uploadedFiles.length : downloadedFiles.length;

    setIsIngesting(true);
    setIngestMessage("Step 1/2: Creating ingest job...");

    try {
      const creds = sessionStorage.getItem("creds");

      const createIngestConfig = {
        data_source: "server",
        data_source_config: { data_path: folderPath },
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

      // Store ingest job data for later use
      setIngestJobData({
        load_job_id: createData.load_job_id,
        data_source_id: createData.data_source_id,
        data_path: folderPath,
      });

      if (!directIngestion) {
        setIngestMessage(`✅ ${fileCount} file(s) ready for ingestion.`);
        setIsIngesting(false);
      } else {
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
      const isRebuildConflict = error.message?.includes("currently being rebuilt");
      setIngestMessage(isRebuildConflict ? `⚠️ ${error.message}` : `❌ Error: ${error.message}`);
      setIsIngesting(false);
    }
  };

  // Called automatically after upload or cloud download finishes
  const handleCreateIngestAfterUpload = async (sourceType: "uploaded" | "downloaded" = "uploaded", fileCountParam?: number) => {
    console.log("handleCreateIngestAfterUpload called with sourceType:", sourceType);
    console.log("ingestGraphName:", ingestGraphName);

    if (!ingestGraphName) {
      console.log("No graph name, returning early");
      return;
    }

    const folderPath = sourceType === "uploaded" ? `uploads/${ingestGraphName}` : `downloaded_files_cloud/${ingestGraphName}`;
    const fileCount = fileCountParam || (sourceType === "uploaded" ? uploadedFiles.length : downloadedFiles.length);
    console.log("folderPath:", folderPath);
    console.log("fileCount:", fileCount);

    try {
      const creds = sessionStorage.getItem("creds");

      const createIngestConfig = {
        data_source: "server",
        data_source_config: { data_path: folderPath },
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

      setIngestJobData({
        load_job_id: createData.load_job_id,
        data_source_id: createData.data_source_id,
        data_path: folderPath,
      });

      console.log("Direct ingestion enabled:", directIngestion);

      if (directIngestion) {
        if (sourceType === "uploaded") {
          setUploadMessage("Running direct ingestion...");
        } else {
          setDownloadMessage("Running direct ingestion...");
        }
        await handleRunIngest(sourceType);
      } else {
        if (sourceType === "uploaded") {
          setUploadMessage(`✅ ${fileCount} file(s) ready for ingestion.`);
        } else {
          setDownloadMessage(`✅ ${fileCount} file(s) ready for ingestion.`);
        }
      }
    } catch (error: any) {
      console.error("Error in create_ingest:", error);
      if (sourceType === "uploaded") {
        setUploadMessage(`❌ Processing error: ${error.message}`);
      } else {
        setDownloadMessage(`❌ Processing error: ${error.message}`);
      }
    }
  };

  // Ingest files from S3 with Amazon BDA
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

    if (skipBDAProcessing) {
      // When skipping BDA, only output bucket and region are required
      if (!outputBucket || !regionName) {
        setIngestMessage("❌ Please provide Output Bucket and Region Name");
        return;
      }
    } else {
      // When using BDA, all fields are required
      if (!inputBucket || !outputBucket || !regionName) {
        setIngestMessage(
          "❌ Please provide Input Bucket, Output Bucket, and Region Name"
        );
        return;
      }
    }

    // Ask for confirmation
    const confirmMessage = skipBDAProcessing
      ? `You're skipping Amazon BDA processing and will ingest directly from the output bucket (${outputBucket}). Please confirm to proceed.`
      : `You're using Amazon BDA for multimodal document processing. This will trigger Amazon BDA to process your documents from the input bucket (${inputBucket}) and store the results in the output bucket (${outputBucket}) and then ingest them into your knowledge graph. Please confirm to proceed.`;

    const shouldProceed = await confirm(confirmMessage);
    if (!shouldProceed) {
      setIngestMessage("Operation cancelled by user.");
      return;
    }

    setIsIngesting(true);

    try {
      const creds = sessionStorage.getItem("creds");
      let loadingInfo: any = {};

      if (skipBDAProcessing) {
        // Skip BDA processing - create ingest job that reads directly from output bucket
        const runIngestConfig: any = {
          data_source: "bda",
          aws_access_key: awsAccessKey,
          aws_secret_key: awsSecretKey,
          output_bucket: outputBucket,
          region_name: regionName,
          bda_jobs: [],
          loader_config: {
            doc_id_field: "doc_id",
            content_field: "content",
            doc_type: "markdown",
          },
          file_format: "multi",
        };

        setIngestMessage("Step 1/2: Creating ingest job from output bucket...");

        // Run ingest directly
        loadingInfo = {
          load_job_id: "load_documents_content_json",
          data_source_id: runIngestConfig,
          file_path: outputBucket,
        };
        setIngestMessage(
          `Step 2/2: Running document ingestion for all files in ${outputBucket}...`
        );
      } else {
        // Step 1: Create ingest job with BDA processing
        const createIngestConfig: any = {
          data_source: "bda",
          data_source_config: {
            aws_access_key: awsAccessKey,
            aws_secret_key: awsSecretKey,
            input_bucket: inputBucket,
            output_bucket: outputBucket,
            region_name: regionName,
          },
          loader_config: {
            doc_id_field: "doc_id",
            content_field: "content",
            doc_type: "markdown",
          },
          file_format: "multi",
        };

        setIngestMessage(
          "Step 1/2: Triggering Amazon BDA processing and creating ingest job..."
        );

        const createResponse = await fetch(
          `/ui/${ingestGraphName}/create_ingest`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Basic ${creds}`,
            },
            body: JSON.stringify(createIngestConfig),
          }
        );

        if (!createResponse.ok) {
          const errorData = await createResponse.json();
          throw new Error(
            errorData.detail ||
              `Failed to create ingest job: ${createResponse.statusText}`
          );
        }

        const createData = await createResponse.json();

        // Step 2: Run ingest
        loadingInfo = {
          load_job_id: createData.load_job_id,
          data_source_id: createData.data_source_id,
          file_path: outputBucket,
        };

        const filesToIngest = createData.data_source_id.bda_jobs.map(
          (job: any) => job.jobId.split("/").at(-1)
        );
        setIngestMessage(
          `Step 2/2: Running document ingest for ${filesToIngest.length} files in ${outputBucket}...`
        );
      }

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
        throw new Error(
          errorData.detail || `Failed to run ingest: ${ingestResponse.statusText}`
        );
      }

      const ingestData = await ingestResponse.json();
      const filesIngested = ingestData.summary.map((file: any) => file.file_path);

      setIngestMessage(
        `✅ Document ingestion completed successfully! Ingested ${filesIngested.length} into your knowledge graph.`
      );
    } catch (error: any) {
      console.error("Error ingesting files:", error);
      setIngestMessage(`❌ Error: ${error.message}`);
    } finally {
      setIsIngesting(false);
    }
  };

  // Keep the idle timer alive while any long-running upload / conversion
  // / ingest is in flight. Ping every 60s — actively resets the idle
  // countdown instead of relying on a pause/resume event sequence that
  // can drift in nested-modal contexts.
  useEffect(() => {
    if (!(isUploading || isProcessingFiles || isIngesting)) return;
    pingIdleTimer();
    const id = setInterval(() => pingIdleTimer(), 60_000);
    return () => clearInterval(id);
  }, [isUploading, isProcessingFiles, isIngesting]);

  // Load available graphs. Seed from sessionStorage for instant render,
  // then refresh from /ui/list_graphs so newly-initialized graphs show
  // up without a re-login.
  useEffect(() => {
    const store = JSON.parse(sessionStorage.getItem("site") || "{}");
    if (store.graphs && Array.isArray(store.graphs)) {
      setAvailableGraphs(store.graphs);
      if (store.graphs.length > 0 && !ingestGraphName) {
        setIngestGraphName(store.graphs[0]);
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
        if (graphs.length > 0 && !ingestGraphName) {
          setIngestGraphName(graphs[0]);
        }
      })
      .catch(() => {
        /* keep cached value; not fatal */
      });
  }, []);

  // Keep the Ingest dialog's graph picker in sync with the shared
  // ``selectedGraph`` so changing the graph elsewhere (KGAdmin
  // refresh, Bot) immediately reflects here.
  useEffect(() => {
    const handler = () => {
      const next = sessionStorage.getItem("selectedGraph") || "";
      if (next && next !== ingestGraphName) setIngestGraphName(next);
    };
    window.addEventListener("graphrag:selectedGraph", handler);
    return () => window.removeEventListener("graphrag:selectedGraph", handler);
  }, [ingestGraphName]);

  // Load files when graph name changes
  useEffect(() => {
    if (ingestGraphName) {
      fetchUploadedFiles();
      fetchDownloadedFiles();
    }
  }, [ingestGraphName]);

  return (
    <div className={isModal ? "" : "p-8"}>
      <div className={isModal ? "" : "max-w-5xl mx-auto"}>
        {!isModal && (
          <>
            <div className="mb-6">
              <h1 className="text-2xl font-bold text-black dark:text-white mb-2">
                Ingest to Knowledge Graph
              </h1>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                Upload and ingest documents into your knowledge graph for future content processing
              </p>
            </div>
          </>
        )}

        <div className="bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6">
          {/* Graph Name Selection */}
          <div className="mb-4">
            <label className="block text-sm font-medium mb-2 text-black dark:text-white">
              Target Graph Name
            </label>
            <Select
              value={ingestGraphName}
              onValueChange={(v) => {
                setIngestGraphName(v);
                sessionStorage.setItem("selectedGraph", v);
                window.dispatchEvent(new Event("graphrag:selectedGraph"));
              }}
              disabled={isIngesting}
            >
              <SelectTrigger
                className="dark:border-[#3D3D3D] dark:bg-shadeA"
                disabled={isIngesting}
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

          <Tabs
            value={activeTab}
            onValueChange={(value) => {
              // Block tab switching when ingesting
              if (!isIngesting) {
                setActiveTab(value);
              }
            }}
            className="w-full"
          >
            <TabsList className="grid w-full grid-cols-3">
              <TabsTrigger value="upload" disabled={isIngesting}>
                <FolderUp className="h-4 w-4 mr-2" />
                Upload Files
              </TabsTrigger>
              <TabsTrigger value="cloudDownload" disabled={isIngesting}>
                <CloudDownload className="h-4 w-4 mr-2" />
                Download from Cloud
              </TabsTrigger>
              <TabsTrigger value="AmazonBDA" disabled={isIngesting}>
                <CloudLightning className="h-4 w-4 mr-2" />
                Use Amazon BDA
              </TabsTrigger>
            </TabsList>

            {/* Upload Data Tab */}
            <TabsContent value="upload" className="space-y-4">
              <div className="space-y-4">
                <p className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">
                  Upload local files to the server and ingest them into your knowledge
                  graph.
                </p>
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Select Files
                  </label>
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    onChange={(e) => setSelectedFiles(e.target.files)}
                    disabled={isUploading}
                    className="hidden"
                  />
                  <div className="flex items-center gap-3">
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => fileInputRef.current?.click()}
                      disabled={isUploading}
                      className="border-blue-500 text-blue-600 hover:bg-blue-50 dark:border-blue-400 dark:text-blue-400 dark:hover:bg-blue-900/20"
                    >
                      <FolderUp className="h-4 w-4 mr-2" />
                      Choose Files
                    </Button>
                    <span className="text-sm italic text-gray-400 dark:text-gray-500">
                      {selectedFiles && selectedFiles.length > 0
                        ? `${selectedFiles.length} file${selectedFiles.length > 1 ? "s" : ""} selected`
                        : "No files selected"}
                    </span>
                  </div>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                    Maximum upload per request: {MAX_UPLOAD_SIZE_MB} MB.
                  </p>
                  {selectedFiles && (() => {
                    const SUPPORTED_EXTENSIONS = new Set([".txt", ".md", ".pdf", ".docx", ".doc", ".html", ".htm", ".json", ".csv", ".xlsx", ".xls", ".xml", ".jpeg", ".jpg", ".png", ".gif", ".jsonl"]);
                    const files = Array.from(selectedFiles);
                    const unsupported = files.filter((f) => !SUPPORTED_EXTENSIONS.has(f.name.slice(f.name.lastIndexOf(".")).toLowerCase()));
                    const hasCsvExcel = files.some((f) => [".csv", ".xlsx", ".xls"].includes(f.name.slice(f.name.lastIndexOf(".")).toLowerCase()));
                    return (
                      <>
                        {unsupported.length > 0 && (
                          <div className="flex items-start gap-2 mt-2 p-2 rounded-md bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700">
                            <span className="text-red-500 mt-0.5 shrink-0">⚠️</span>
                            <p className="text-xs text-red-700 dark:text-red-300">
                              Unsupported file type{unsupported.length > 1 ? "s" : ""}: <strong>{unsupported.map((f) => f.name).join(", ")}</strong>. These files will be skipped during ingestion.
                            </p>
                          </div>
                        )}
                        {hasCsvExcel && (
                          <div className="flex items-start gap-2 mt-2 p-2 rounded-md bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700">
                            <span className="text-amber-500 mt-0.5 shrink-0">ℹ️</span>
                            <p className="text-xs text-amber-700 dark:text-amber-300">
                              CSV and Excel files will be treated as unstructured text documents.
                            </p>
                          </div>
                        )}
                      </>
                    );
                  })()}
                </div>

                <div className="flex gap-2">
                  <Button
                    onClick={handleUploadFiles}
                    disabled={isUploading || isProcessingFiles || !selectedFiles}
                    className="gradient text-white"
                  >
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
                    <Button
                      onClick={handleDeleteAllFiles}
                      variant="outline"
                      disabled={isProcessingFiles || isIngesting}
                      className="dark:border-[#3D3D3D]"
                    >
                      <Trash2 className="h-4 w-4 mr-2" />
                      Delete All
                    </Button>
                  )}
                </div>

                {uploadMessage && (
                  <div className="p-3 rounded-lg text-sm bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300">
                    {uploadMessage}
                  </div>
                )}

                {/* Uploaded Files List */}
                {uploadedFiles.length > 0 && (
                  <div className="border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-4">
                    <h3 className="text-sm font-medium mb-3 text-black dark:text-white">
                      Uploaded Files ({uploadedFiles.length})
                    </h3>
                    <div className="space-y-2 max-h-48 overflow-y-auto">
                      {uploadedFiles.map((file, index) => (
                        <div
                          key={index}
                          className="flex items-center justify-between p-2 bg-gray-50 dark:bg-shadeA rounded"
                        >
                          <span className="text-sm text-black dark:text-white truncate flex-1">
                            {file.filename}
                          </span>
                          <Button
                            onClick={() => handleDeleteFile(file.filename)}
                            variant="outline"
                            size="sm"
                            disabled={isProcessingFiles || isIngesting}
                            className="ml-2 dark:border-[#3D3D3D]"
                          >
                            <Trash2 className="h-3 w-3" />
                          </Button>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Ingest Data Section */}
                {uploadedFiles.length > 0 && (
                  <div className="border-t border-gray-300 dark:border-[#3D3D3D] pt-4 mt-4">
                    <h3 className="text-sm font-medium mb-2 text-black dark:text-white">
                      Ingest Documents into Knowledge Graph
                    </h3>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
                      Process uploaded files and add them to the knowledge graph
                    </p>
                    <Button
                      onClick={() => handleRunIngest("uploaded")}
                      disabled={isIngesting || isProcessingFiles}
                      className="gradient text-white w-full"
                    >
                      {isIngesting ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Ingesting...
                        </>
                      ) : isProcessingFiles ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Processing files...
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
                      }`}>
                        {ingestMessage}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </TabsContent>

            {/* Download from Cloud Storage Tab */}
            <TabsContent value="cloudDownload" className="space-y-4">
              <div className="space-y-4">
                <p className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">
                  Download files from cloud storage and ingest them into your knowledge
                  graph.
                </p>
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Cloud Storage Provider
                  </label>
                  <Select
                    value={cloudProvider}
                    onValueChange={(value: "s3" | "gcs" | "azure") =>
                      setCloudProvider(value)
                    }
                  >
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
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        AWS Access Key
                      </label>
                      <Input
                        type="text"
                        value={cloudAccessKey}
                        onChange={(e) => setCloudAccessKey(e.target.value)}
                        placeholder="Enter AWS access key"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        AWS Secret Key
                      </label>
                      <Input
                        type="password"
                        value={cloudSecretKey}
                        onChange={(e) => setCloudSecretKey(e.target.value)}
                        placeholder="Enter AWS secret key"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        S3 Bucket Name
                      </label>
                      <Input
                        type="text"
                        value={cloudBucket}
                        onChange={(e) => setCloudBucket(e.target.value)}
                        placeholder="my-bucket-name"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Region
                      </label>
                      <Input
                        type="text"
                        value={cloudRegion}
                        onChange={(e) => setCloudRegion(e.target.value)}
                        placeholder="us-east-1"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Prefix/Path (Optional)
                      </label>
                      <Input
                        type="text"
                        value={cloudPrefix}
                        onChange={(e) => setCloudPrefix(e.target.value)}
                        placeholder="folder/subfolder/"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                  </>
                )}

                {/* Google Cloud Storage */}
                {cloudProvider === "gcs" && (
                  <>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Project ID
                      </label>
                      <Input
                        type="text"
                        value={gcsProjectId}
                        onChange={(e) => setGcsProjectId(e.target.value)}
                        placeholder="my-project-id"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Service Account JSON
                      </label>
                      <textarea
                        value={gcsCredentials}
                        onChange={(e) => setGcsCredentials(e.target.value)}
                        placeholder='{"type": "service_account", ...}'
                        rows={4}
                        className="w-full p-2 rounded border dark:border-[#3D3D3D] dark:bg-shadeA text-sm"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Bucket Name
                      </label>
                      <Input
                        type="text"
                        value={cloudBucket}
                        onChange={(e) => setCloudBucket(e.target.value)}
                        placeholder="my-gcs-bucket"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Prefix/Path (Optional)
                      </label>
                      <Input
                        type="text"
                        value={cloudPrefix}
                        onChange={(e) => setCloudPrefix(e.target.value)}
                        placeholder="folder/subfolder/"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                  </>
                )}

                {/* Azure Blob Storage */}
                {cloudProvider === "azure" && (
                  <>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Storage Account Name
                      </label>
                      <Input
                        type="text"
                        value={azureAccountName}
                        onChange={(e) => setAzureAccountName(e.target.value)}
                        placeholder="mystorageaccount"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Account Key
                      </label>
                      <Input
                        type="password"
                        value={azureAccountKey}
                        onChange={(e) => setAzureAccountKey(e.target.value)}
                        placeholder="Enter account key"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Container Name
                      </label>
                      <Input
                        type="text"
                        value={azureContainer}
                        onChange={(e) => setAzureContainer(e.target.value)}
                        placeholder="my-container"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                        Prefix/Path (Optional)
                      </label>
                      <Input
                        type="text"
                        value={cloudPrefix}
                        onChange={(e) => setCloudPrefix(e.target.value)}
                        placeholder="folder/subfolder/"
                        className="dark:border-[#3D3D3D] dark:bg-shadeA"
                      />
                    </div>
                  </>
                )}
                {ingestGraphName && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                    Download destination: downloaded_files_cloud/{ingestGraphName}/
                  </p>
                )}

                <div className="pt-4 border-t border-gray-300 dark:border-[#3D3D3D]">
                  <Button
                    onClick={handleCloudDownload}
                    disabled={isDownloading}
                    className="gradient text-white w-full"
                  >
                    {isDownloading ? (
                      <>
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        Downloading...
                      </>
                    ) : (
                      <>
                        <CloudDownload className="h-4 w-4 mr-2" />
                        Download Files from{" "}
                        {cloudProvider === "s3"
                          ? "S3"
                          : cloudProvider === "gcs"
                          ? "GCS"
                          : "Azure"}
                      </>
                    )}
                  </Button>
                </div>

                {downloadMessage && (
                  <div
                    className={`p-3 rounded-lg text-sm mt-3 ${
                      downloadMessage.includes("✅")
                        ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                        : downloadMessage.includes("❌")
                        ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                        : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                    }`}
                  >
                    {downloadMessage}
                  </div>
                )}

                {/* Downloaded Files List */}
                {downloadedFiles.length > 0 && (
                  <div className="mt-4 border-t border-gray-300 dark:border-[#3D3D3D] pt-4">
                    <div className="flex justify-between items-center mb-2">
                      <h4 className="text-sm font-medium text-black dark:text-white">
                        Downloaded Files ({downloadedFiles.length})
                      </h4>
                      <Button
                        onClick={handleDeleteAllDownloadedFiles}
                        variant="outline"
                        size="sm"
                        className="dark:border-[#3D3D3D]"
                      >
                        <Trash2 className="h-3 w-3 mr-1" />
                        Delete All
                      </Button>
                    </div>
                    <ul className="space-y-2 max-h-40 overflow-y-auto">
                      {downloadedFiles.map((file, index) => (
                        <li
                          key={index}
                          className="flex justify-between items-center p-2 bg-gray-50 dark:bg-shadeA rounded text-sm"
                        >
                          <span className="text-black dark:text-white truncate flex-1">
                            {file.name}
                          </span>
                          <Button
                            onClick={() => handleDeleteDownloadedFile(file.name)}
                            variant="ghost"
                            size="sm"
                            className="ml-2 text-red-500 hover:text-red-700 dark:hover:text-red-400"
                          >
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
                    <h3 className="text-sm font-medium mb-2 text-black dark:text-white">
                      Ingest Documents into Knowledge Graph
                    </h3>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
                      Process downloaded files and add them to the knowledge graph
                    </p>
                    <Button
                      onClick={() => handleRunIngest("downloaded")}
                      disabled={isIngesting || isProcessingFiles}
                      className="gradient text-white w-full"
                    >
                      {isIngesting ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Ingesting...
                        </>
                      ) : isProcessingFiles ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Processing files...
                        </>
                      ) : (
                        <>
                          <Database className="h-4 w-4 mr-2" />
                          Ingest Documents into {ingestGraphName}
                        </>
                      )}
                    </Button>
                  </div>
                )}
              </div>
            </TabsContent>

            {/* Amazon BDA Configuration Tab */}
            <TabsContent value="AmazonBDA" className="space-y-4">
              <div className="space-y-4">
                <p className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">
                  Process multimodal documents stored in S3 with Amazon Bedrock Data
                  Automation and ingest them into your knowledge graph.
                </p>

                {/* Common fields */}
                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    AWS Access Key
                  </label>
                  <Input
                    type="text"
                    value={awsAccessKey}
                    onChange={(e) => setAwsAccessKey(e.target.value)}
                    placeholder="Enter AWS access key"
                    className="dark:border-[#3D3D3D] dark:bg-shadeA"
                    disabled={isIngesting}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    AWS Secret Key
                  </label>
                  <Input
                    type="password"
                    value={awsSecretKey}
                    onChange={(e) => setAwsSecretKey(e.target.value)}
                    placeholder="Enter AWS secret key"
                    className="dark:border-[#3D3D3D] dark:bg-shadeA"
                    disabled={isIngesting}
                  />
                </div>

                <div>
                  <div className="flex items-center justify-between mb-2">
                    <label className="block text-sm font-medium text-black dark:text-white">
                      Input Bucket
                    </label>
                    <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={skipBDAProcessing}
                        onChange={(e) => setSkipBDAProcessing(e.target.checked)}
                        disabled={isIngesting}
                        className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
                      />
                      <span>Skip BDA (ingest existing BDA output bucket directly)</span>
                    </label>
                  </div>
                  <Input
                    type="text"
                    value={inputBucket}
                    onChange={(e) => setInputBucket(e.target.value)}
                    placeholder="Enter input bucket name"
                    className="dark:border-[#3D3D3D] dark:bg-shadeA"
                    disabled={isIngesting || skipBDAProcessing}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Output Bucket
                  </label>
                  <Input
                    type="text"
                    value={outputBucket}
                    onChange={(e) => setOutputBucket(e.target.value)}
                    placeholder="Enter output bucket name"
                    className="dark:border-[#3D3D3D] dark:bg-shadeA"
                    disabled={isIngesting}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2 text-black dark:text-white">
                    Region Name
                  </label>
                  <Input
                    type="text"
                    value={regionName}
                    onChange={(e) => setRegionName(e.target.value)}
                    placeholder="e.g., us-east-1"
                    className="dark:border-[#3D3D3D] dark:bg-shadeA"
                    disabled={isIngesting}
                  />
                </div>

                {ingestGraphName && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                    Processing destination: Input bucket ({inputBucket || "not specified"}
                    ) → Output bucket ({outputBucket || "not specified"}) → Knowledge
                    graph ({ingestGraphName})
                  </p>
                )}

                {/* Ingest S3 Files with Amazon BDA Section */}
                <div className="border-t border-gray-300 dark:border-[#3D3D3D] pt-4 mt-4">
                  <Button
                    onClick={handleAmazonBDAIngest}
                    disabled={isIngesting}
                    className="gradient text-white w-full"
                  >
                    {isIngesting ? (
                      <>
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        Ingesting...
                      </>
                    ) : (
                      <>
                        <Database className="h-4 w-4 mr-2" />
                        Ingest from S3 Bucket into {ingestGraphName}
                      </>
                    )}
                  </Button>
                  {ingestMessage && (
                    <div
                      className={`p-3 rounded-lg text-sm mt-3 ${
                        ingestMessage.includes("✅")
                          ? "bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                          : ingestMessage.includes("❌")
                          ? "bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
                          : "bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                      }`}
                    >
                      {ingestMessage}
                    </div>
                  )}
                </div>
              </div>
            </TabsContent>
          </Tabs>
        </div>
      </div>
      {confirmDialog}
    </div>
  );
};

export default IngestGraph;

