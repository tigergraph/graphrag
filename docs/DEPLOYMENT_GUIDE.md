# 🚀 CyberGraph RAG: Production Deployment & Hosting Guide

This guide details exactly **HOW** and **WHERE** to deploy the complete containerized CyberGraph RAG benchmarking system for a fully functional, production-ready environment.

---

## 🏛️ Deployment Architecture Overview

```
                      Internet (HTTPS)
                              │
                              ▼
                ┌───────────────────────────┐
                │    Google Cloud Load      │
                │         Balancer          │
                └─────────────┬─────────────┘
                              │
             ┌────────────────┴────────────────┐
             ▼ (Port 8888)                     ▼ (Port 8000)
    ┌──────────────────┐              ┌──────────────────┐
    │  Cloud Run App   │              │  Cloud Run App   │
    │  (Dashboard UI)  │              │ (FastAPI Backend)│
    └──────────────────┘              └────────┬─────────┘
                                               │ (VPC Connector)
                                               ▼
                                      ┌──────────────────┐
                                      │ Compute Engine VM│
                                      │ (TigerGraph DB)  │
                                      └──────────────────┘
```

---

## 🛠️ Option 1: The Recommended Cloud Stack (GCP Cloud Run + GCE)
This is the most scalable, professional, and cost-efficient architecture to deploy for hackathon evaluations.

### Component 1: Host TigerGraph on Google Compute Engine (GCE)
Because TigerGraph requires permanent storage and substantial in-memory compiler capacity (16GB RAM minimum), it is best hosted on a dedicated VM instance:

1.  **Create the VM**:
    *   Navigate to GCP Console $\rightarrow$ **Compute Engine** $\rightarrow$ **VM Instances**.
    *   Choose Machine Type: **`e2-standard-4`** (4 vCPUs, 16 GB memory).
    *   OS/Disk: Ubuntu 22.04 LTS with 50 GB Balanced Persistent Disk.
    *   Networking: Enable HTTP/HTTPS traffic.
2.  **Launch TigerGraph via Docker**:
    SSH into the GCE VM and run:
    ```bash
    sudo apt-get update && sudo apt-get install -y docker.io docker-compose
    # Start TigerGraph Community Container
    sudo docker run -d --name tigergraph -p 9000:9000 -p 14240:14240 -v ~/tg_data:/var/lib/tigergraph tigergraph/community:4.2.2
    ```

### Component 2: Deploy GraphRAG Backend to Google Cloud Run
Google Cloud Run is a fully managed serverless platform that scales your container on-demand.

1.  **Build the Docker Image**:
    Use Google Cloud Build to compile and push your GraphRAG backend to GCP Artifact Registry:
    ```bash
    gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/cybergraph-rag-backend
    ```
2.  **Deploy Container to Cloud Run**:
    Deploy the image, ensuring you pass your Gemini API Key as an environment variable:
    ```bash
    gcloud run deploy cybergraph-backend \
      --image gcr.io/YOUR_PROJECT_ID/cybergraph-rag-backend \
      --platform managed \
      --region us-central1 \
      --allow-unauthenticated \
      --set-env-vars="GOOGLE_API_KEY=YOUR_GEMINI_API_KEY_HERE,TG_HOST=http://YOUR_GCE_VM_INTERNAL_IP"
    ```

---

## 🐳 Option 2: Enterprise Kubernetes (Google GKE)
For a single, completely unified deployments, utilize the pre-compiled **[graphrag-k8s.yml](file:///c:/TigerGraph/graphrag-k8s.yml)** manifest included in the repository.

1.  **Initialize GKE Cluster**:
    ```bash
    gcloud container clusters create cybergraph-cluster \
      --num-nodes=3 \
      --machine-type=e2-standard-4 \
      --region=us-central1
    ```
2.  **Apply Kubernetes Manifests**:
    Connect kubectl to the cluster and deploy:
    ```bash
    gcloud container clusters get-credentials cybergraph-cluster --region us-central1
    kubectl apply -f graphrag-k8s.yml
    ```
3.  **Audit Services**:
    ```bash
    kubectl get services
    ```
    Grab the External LoadBalancer IP for `graphrag-ui-service` and navigate to it on port 8888!

---

## ⚡ Option 3: The Simplest 5-Minute VM Host (Compute Engine VM Only)
To get the fully functional stack online instantly without VPC networks or serverless boundaries:

1.  Launch a single VM instance on GCP: **`e2-standard-4`** (Ubuntu 22.04, 16GB RAM).
2.  Open VM Firewall ports in your GCP VPC firewall:
    *   `8888` (Comparison Dashboard)
    *   `8000` (FastAPI Proxy Router)
3.  SSH into the VM, clone this repository, and insert your Gemini API Key in `configs/server_config.json`.
4.  Launch the Docker Compose stack:
    ```bash
    docker-compose up -d
    ```
5.  Start GSQL compiler & run data ingestion:
    ```bash
    docker exec -u tigergraph tigergraph /home/tigergraph/tigergraph/app/cmd/gadmin start all
    python ingest_data.py
    python dashboard_api.py
    ```
6.  Navigate to: `http://YOUR_VM_EXTERNAL_IP:8888` inside your browser to access the live dashboard!

---
*Created for the TigerGraph GraphRAG Inference Hackathon 2026.*
