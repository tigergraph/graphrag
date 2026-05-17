import requests
import time
import os
import json
import logging
import sys
from requests.auth import HTTPBasicAuth

# --- Configuration ---
API_BASE_URL = "http://127.0.0.1:80"  # Proxied via Nginx
GRAPH_NAME = "cyber_graph"            # Your target graph
USERNAME = "tigergraph"
PASSWORD = "tigergraph"

# Setup Logging and Encoding
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # Fallback for older python versions if needed
        pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class GraphRAGIngestor:
    def __init__(self, base_url, graph_name, username, password):
        self.base_url = base_url.rstrip('/')
        self.graph_name = graph_name
        self.auth = HTTPBasicAuth(username, password)
        self.headers = {"Accept": "application/json"}

    def create_graph(self):
        """
        Step 0.1: Create the graph if it doesn't exist.
        """
        url = f"{self.base_url}/ui/{self.graph_name}/create_graph"
        logger.info(f"🔨 Creating graph: {self.graph_name}...")
        try:
            response = requests.post(url, auth=self.auth)
            response.raise_for_status()
            logger.info(f"✅ Graph created/verified: {response.json().get('message')}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info(f"ℹ️ Graph {self.graph_name} already exists.")
            else:
                logger.error(f"❌ Graph Creation Failed: {e}")
                raise

    def initialize_graph(self):
        """
        Step 0.2: Initialize the graph with GraphRAG schema and queries.
        """
        url = f"{self.base_url}/ui/{self.graph_name}/initialize_graph"
        logger.info(f"⚙️ Initializing graph schema for {self.graph_name}...")
        try:
            response = requests.post(url, auth=self.auth)
            response.raise_for_status()
            logger.info(f"✅ Graph initialized successfully.")
        except Exception as e:
            logger.error(f"❌ Graph Initialization Failed: {e}")
            raise

    def upload_file(self, file_path):
        """
        PHASE 1: Upload raw file to the server's local storage.
        Endpoint: /ui/{graphname}/uploads
        Expected Response: JSON with status 'success' and list of uploaded files.
        Failure Points: Disk full, permissions, or Nginx client_max_body_size exceeded.
        """
        start_time = time.time()
        url = f"{self.base_url}/ui/{self.graph_name}/uploads"
        
        filename = os.path.basename(file_path)
        logger.info(f"🚀 Phase 1: Uploading {filename}...")

        try:
            with open(file_path, 'rb') as f:
                files = [('files', (filename, f, 'application/octet-stream'))]
                # overwrite=true allows us to re-upload files during testing
                response = requests.post(url, auth=self.auth, files=files, params={"overwrite": "true"})
                response.raise_for_status()
                
            duration = time.time() - start_time
            logger.info(f"✅ Upload successful. Duration: {duration:.2f}s")
            return duration
        except Exception as e:
            logger.error(f"❌ Phase 1 Failed: {e}")
            raise

    def create_ingest_job(self):
        """
        PHASE 2: Pre-process uploaded files and prepare a TigerGraph Loading Job.
        Endpoint: /ui/{graphname}/create_ingest
        Expected Response: JSON containing 'load_job_id' and 'data_source_id' dict.
        Failure Points: File format not supported, OCR errors (if PDF), or missing temp directories.
        """
        start_time = time.time()
        url = f"{self.base_url}/ui/{self.graph_name}/create_ingest"
        
        logger.info(f"⚙️ Phase 2: Preparing ingestion config...")

        # 'server' source points to the 'uploads' directory where Phase 1 saved files
        payload = {
            "data_source": "server",
            "data_source_config": {
                "data_path": f"uploads/{self.graph_name}"
            },
            "file_format": "multi" # Handles PDF, TXT, MD automatically
        }

        try:
            response = requests.post(url, auth=self.auth, json=payload)
            response.raise_for_status()
            data = response.json()
            
            duration = time.time() - start_time
            logger.info(f"✅ Ingest config created. Duration: {duration:.2f}s")
            return data, duration
        except Exception as e:
            logger.error(f"❌ Phase 2 Failed: {e}")
            raise

    def run_ingestion(self, ingest_config):
        """
        PHASE 3: Trigger the actual load job into TigerGraph vertices.
        Endpoint: /ui/{graphname}/ingest
        Expected Response: Job status details (JobId, Log location).
        Failure Points: TigerGraph GSQL server down, loading job not installed, or data corruption.
        """
        start_time = time.time()
        url = f"{self.base_url}/ui/{self.graph_name}/ingest"
        
        logger.info(f"📥 Phase 3: Loading data into TigerGraph...")

        # We pass the exact keys returned from Phase 2
        payload = {
            "load_job_id": ingest_config["load_job_id"],
            "data_source_id": ingest_config["data_source_id"],
            "file_path": "in_temp_storage" 
        }

        try:
            response = requests.post(url, auth=self.auth, json=payload)
            response.raise_for_status()
            
            duration = time.time() - start_time
            logger.info(f"✅ Data loaded into vertices. Duration: {duration:.2f}s")
            return duration
        except Exception as e:
            logger.error(f"❌ Phase 3 Failed: {e}")
            raise

    def rebuild_graph(self):
        """
        PHASE 4: Trigger the GraphRAG extraction (Entities, Relationships, Communities).
        Endpoint: /ui/{graphname}/rebuild_graph
        Note: This is an ASYNC trigger.
        Failure Points: LLM API keys invalid, Rate limits, or ECC service down.
        """
        start_time = time.time()
        url = f"{self.base_url}/ui/{self.graph_name}/rebuild_graph"
        
        logger.info(f"🧠 Phase 4: Triggering GraphRAG Rebuild (Extraction)...")

        try:
            response = requests.post(url, auth=self.auth)
            response.raise_for_status()
            logger.info("✅ Rebuild triggered successfully.")
            return start_time
        except Exception as e:
            logger.error(f"❌ Phase 4 Trigger Failed: {e}")
            raise

    def monitor_rebuild(self, trigger_time):
        """
        Monitoring: Poll the status until the GraphRAG extraction is complete.
        Endpoint: /ui/{graphname}/rebuild_status
        """
        url = f"{self.base_url}/ui/{self.graph_name}/rebuild_status"
        logger.info("⏳ Monitoring extraction progress (this may take a few minutes)...")
        
        while True:
            try:
                response = requests.get(url, auth=self.auth)
                response.raise_for_status()
                status_data = response.json()
                
                is_running = status_data.get("is_running", False)
                status = status_data.get("status", "unknown")
                
                if not is_running and status in ["completed", "failed", "idle"]:
                    duration = time.time() - trigger_time
                    if status == "completed":
                        logger.info(f"🎊 GraphRAG Rebuild Complete! Total Rebuild Time: {duration:.2f}s")
                    else:
                        logger.warning(f"⚠️ Rebuild finished with status: {status}")
                    return duration
                
                logger.info(f"   - Current Status: {status}...")
                time.sleep(10) # Poll every 10 seconds
                
            except Exception as e:
                logger.warning(f"   - Status check failed (retrying): {e}")
                time.sleep(5)

def run_full_ingestion(file_path):
    ingestor = GraphRAGIngestor(API_BASE_URL, GRAPH_NAME, USERNAME, PASSWORD)
    
    print("\n" + "="*50)
    print(f"🛠️  STARTING INGESTION FOR: {os.path.basename(file_path)}")
    print("="*50 + "\n")

    max_retries = 3
    retry_delay = 10

    for attempt in range(max_retries):
        try:
            # ingestor.create_graph()
            # ingestor.initialize_graph()
            break
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ Initial steps failed (Attempt {attempt+1}/{max_retries}). Retrying in {retry_delay}s... Error: {e}")
                time.sleep(retry_delay)
            else:
                print(f"❌ CRITICAL FAILURE: Failed after {max_retries} attempts. {e}")
                return

    try:
        t1 = ingestor.upload_file(file_path)
        config, t2 = ingestor.create_ingest_job()
        t3 = ingestor.run_ingestion(config)
        
        # Rebuild phase
        rebuild_start = ingestor.rebuild_graph()
        t4 = ingestor.monitor_rebuild(rebuild_start)

        total_ingest = t1 + t2 + t3
        
        print("\n" + "="*50)
        print("🏁 FINAL METRICS")
        print("-" * 50)
        print(f"📊 Total Ingestion Duration: {total_ingest:.2f}s")
        print(f"🧠 Total Rebuild Duration:   {t4:.2f}s")
        print(f"✨ OVERALL SUCCESS:          {os.path.basename(file_path)} is now in the Graph!")
        print("="*50 + "\n")
    except Exception as e:
        print("\n" + "!"*50)
        print(f"❌ CRITICAL FAILURE: {e}")
        print("!"*50 + "\n")

if __name__ == "__main__":
    # Example usage
    test_file = "cybersecurity_test.txt"
    if os.path.exists(test_file):
        run_full_ingestion(test_file)
    else:
        print(f"❌ Error: Please create '{test_file}' before running.")
        print("   Quick Fix: echo 'APT-28 Malware Report...' > threat_report.txt")
