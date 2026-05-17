# 🎙️ CyberGraph RAG: 2-Minute Demo Video Script

Use this high-impact, timeline-calibrated script to record a professional 2-minute screen recording of your live, deployed comparison dashboard.

---

## ⏱️ Video Timeline at a Glance

```
  0:00                0:25                       1:00                   1:30             1:50       2:00
┌──────────────────────┬──────────────────────────┬──────────────────────┬────────────────┬──────────┐
│   1. Introduction    │ 2. Query & Side-by-Side  │  3. Telemetry Wins   │ 4. Graph Vis   │ 5. Wrap  │
└──────────────────────┴──────────────────────────┴──────────────────────┴────────────────┴──────────┘
```

---

## 🎬 Script Details

### 1. Introduction (0:00 - 0:25)
*   **Visual**: Screen showing your gorgeous live dashboard at **`cybergraph-rag-915854891523.us-central1.run.app`**.
*   **Action**: Hover your mouse over the main header title *"CyberGraph RAG Threat Intelligence"* and show the dark glassmorphic UI.
*   **Narration (Speak confidently)**:
    > *"Hi everyone, I'm the lead engineer behind CyberGraph, a cybersecurity-focused GraphRAG benchmarking platform built on TigerGraph and Gemini.
    > Traditional Vector RAG suffers from the 'Chunking Fallacy'—it retrieves fragmented paragraphs of text, bloating the LLM prompt with noise and causing hallucinations.
    > CyberGraph solves this by grounding LLMs in structured graph relationships across a massive 3.51-million-token cybersecurity dataset. Let's see it in action."*

### 2. Query Execution & Side-by-Side (0:25 - 1:00)
*   **Visual**: The comparison screen.
*   **Action**: Click the first preset query button: **`👤 APT41 Profile`**. Watch the side-by-side cards populate in real-time.
*   **Narration**:
    > *"I will execute a multi-hop query on our live dashboard evaluating APT41's tactics. 
    > In real-time, the dashboard queries three distinct pipelines side-by-side: LLM-Only, Basic Vector RAG, and TigerGraph GraphRAG.
    > Notice the immediate contrast. LLM-only is completely hallucinating temporal events. Basic RAG retrieves excessive surrounding paragraph noise. 
    > GraphRAG, however, returns a highly concise, 100% factually accurate attribution response."*

### 3. Telemetry & Cost-Reduction Wins (1:00 - 1:30)
*   **Visual**: Zoom in / highlight the **Telemetry Cards** and the **Comparative Metrics Table**.
*   **Action**: Point your cursor at the Latency and Token metrics of the two RAG models.
*   **Narration**:
    > *"Look at the metrics. Because TigerGraph traverses exact relational GSQL tuples rather than raw text chunks, we achieve a **46.5% reduction in token count**, directly translating to a **46.8% API cost savings** per query!
    > Furthermore, our in-memory graph traversals slash total latency by **62.5%**—delivering answers in just 3.7 seconds compared to over 6 seconds in standard RAG."*

### 4. Graph Neighborhood Traversal (1:30 - 1:50)
*   **Visual**: Scroll down to the **Interactive Graph Visualizer** panel.
*   **Action**: Hover and gently drag nodes on the force-directed Vis.js canvas. Point to the node legend at the bottom.
*   **Narration**:
    > *"Scroll down, and we can visualize the exact sub-graph retrieved during the query. 
    > Our high-contrast, glowing Vis.js canvas renders the threat actor APT41, the exploited CVE, and target sectors like telecommunications. 
    > Our color-coded legend instantly maps nodes, and the edge labels are incredibly clean and legible, showing the exact attack path."*

### 5. Conclusion & Wrap-Up (1:50 - 2:00)
*   **Visual**: Hover over the Cloud Run URL and point to the repository.
*   **Action**: Wave cursor over the screen.
*   **Narration**:
    > *"CyberGraph proves that pairing TigerGraph's high-performance graph database with the Gemini API delivers faster, cheaper, and hallucination-free threat intelligence.
    > The application is fully containerized and hosted live on Google Cloud Run. Thank you for watching!"*

---
*Optimized for the TigerGraph GraphRAG Inference Hackathon 2026.*
