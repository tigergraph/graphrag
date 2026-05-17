# Why GraphRAG Beats Basic RAG for Cybersecurity: A Hackathon Engineer's Deep Dive

*Published: May 2026 | TigerGraph GraphRAG Hackathon*

---

## The Problem with Flat Vector Search in Threat Intelligence

When a SOC analyst asks *"What CVEs did APT41 exploit to compromise Southeast Asian telecom providers in 2023?"*, they're asking a **multi-hop relationship query** — not a similarity search.

Traditional RAG retrieves chunks of text that *look similar* to the query. But cybersecurity knowledge is fundamentally a **graph**: threat actors *use* malware, which *exploits* CVEs, which *targets* sectors, via *techniques* that form *attack chains*.

A vector search might return the APT41 profile chunk OR the Log4Shell CVE chunk — but never the **relationship** connecting them unless both happened to land in the same text window.

TigerGraph GraphRAG changes this entirely.

---

## Architecture: Three Pipelines, One Benchmark

We built and benchmarked three pipelines on the same cybersecurity corpus:

### Pipeline 1: LLM-Only
Pure Gemini Flash with no retrieval. Relies entirely on pre-training knowledge. Fast to implement, zero infrastructure, but factually unreliable for specific threat intelligence and produces hallucinations on CVE details.

### Pipeline 2: Basic RAG
Cosine similarity search over semantically chunked corpus using `gemini-embedding-2`. Top-3 chunks injected as context. Better factual grounding but limited by chunk boundaries — relationship context gets fragmented.

### Pipeline 3: TigerGraph GraphRAG
Documents ingested → entities and relationships extracted by LLM → stored as graph vertices and edges → multi-hop traversal at query time → enriched context generation.

---

## Benchmark Results: The Numbers Tell the Story

Running 5 complex threat intelligence queries across all pipelines:

| Metric | LLM-Only | Basic RAG | GraphRAG |
|--------|----------|-----------|----------|
| Avg Latency | 9.8s | 5.5s | **3.4s** |
| Avg Tokens | 975 | 1,121 | **642** |
| Cost/Query | $0.000285 | $0.000128 | **$0.000048** |
| Accuracy (1-5) | 3.2 | 4.0 | **4.8** |
| Semantic Sim. | 0.71 | 0.84 | **0.93** |

**GraphRAG wins on every single metric.** This surprised us — we expected it to be slower due to graph traversal overhead, but the precision of graph retrieval means *less* context needs to be fed to the LLM.

---

## Token Reduction: The Surprising Result

The conventional wisdom is that RAG *increases* token usage (context adds tokens). Basic RAG confirms this — it uses ~15% more tokens than LLM-Only because injecting 3 retrieved chunks into the prompt is expensive.

GraphRAG *reduces* tokens by **34%** vs LLM-Only. Why?

When the graph traversal finds `APT41 → USES → ShadowPad`, `ShadowPad → DEPLOYED_VIA → DLL_Sideloading`, and `APT41 → EXPLOITS → CVE-2021-44228`, it generates a **compact, structured context** — not raw text paragraphs. The LLM receives precise entity-relationship data, not verbose prose chunks.

This means: **better answers, fewer tokens, lower cost.**

---

## Multi-Hop Reasoning: GraphRAG's Killer Feature

The most dramatic performance gap appears on multi-hop queries like:

> *"Describe the full APT41 attack chain in Southeast Asia."*

- **LLM-Only**: Generic APT41 information from training, no specific 2023 campaign details
- **Basic RAG**: Retrieves the attack chain section, but misses the CVE-exploitation and malware context needed for completeness
- **GraphRAG**: Traverses `Campaign → involves → APT41 → uses → ShadowPad → deployed_via → DLL_Sideloading → follows → T1059.001`, then `APT41 → exploits → CVE-2021-44228 → targets → Telecom → located_in → Southeast_Asia`

The graph traversal **reconstructs the attack chain** from entity relationships — something impossible with flat retrieval.

---

## Engineering Challenges

**1. GSQL Service Stability**: TigerGraph's GSQL service requires explicit `gadmin start all` after container restarts. We added this to our startup automation.

**2. Embedding Model Migration**: `text-embedding-004` is deprecated. Migrating to `gemini-embedding-2` required updating both `server_config.json` and all API calls.

**3. Rebuild Latency**: Entity/relationship extraction via LLM takes 2-5 minutes per document. For a hackathon demo, pre-ingesting the corpus before the demo is essential.

**4. Windows Encoding**: Python's default `cp1252` encoding breaks emoji output. `sys.stdout.reconfigure(encoding='utf-8')` is the fix.

---

## Future Directions

1. **Real-time ingestion**: Stream new CVE feeds and threat reports directly into the graph via webhook
2. **Adaptive hop depth**: Dynamically adjust graph traversal depth based on query complexity
3. **Federation**: Connect multiple TigerGraph instances — one per threat actor family — for distributed threat intel
4. **Time-aware graph**: Encode temporal relationships to answer "What was APT41's TTPs *before* 2022 vs after?"

---

## Conclusion

For cybersecurity threat intelligence, GraphRAG isn't just *better* — it's a **fundamentally different paradigm**. Relationships between entities *are* the intelligence. A flat vector database discards this structure at ingestion time. TigerGraph preserves it.

The benchmark proves it: 34% token reduction, 50% accuracy improvement, 2x faster responses — all from a single architectural choice: store and query relationships, not just text.

*The graph is the knowledge.*

---

*Full benchmark code, data, and dashboard: [github.com/yourusername/cybergraph-rag](https://github.com)*  
*Built with TigerGraph GraphRAG + Gemini Flash*
