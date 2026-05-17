## 🛡️ We built a GraphRAG system for cybersecurity threat intelligence — and the results blew us away.

**Hackathon project: CyberGraph RAG**
Built on @TigerGraph + Gemini Flash

We benchmarked 3 approaches on 5 complex threat intelligence queries (APT41, ShadowPad, Log4Shell, WannaCry attack chains):

📊 **Results:**

| | LLM-Only | Basic RAG | GraphRAG |
|---|---|---|---|
| Latency | 9.8s | 5.5s | **3.4s** ✅ |
| Tokens | 975 | 1,121 | **642** ✅ |
| Cost/Q | $0.000285 | $0.000128 | **$0.000048** ✅ |
| Accuracy | 3.2/5 | 4.0/5 | **4.8/5** ✅ |

**GraphRAG won on every metric.**

🔑 Key insight: Cybersecurity knowledge is a graph.

APT41 → USES → ShadowPad → EXPLOITS → CVE-2021-44228 → TARGETS → Telecom

A flat vector database discards this structure. TigerGraph preserves it — enabling multi-hop reasoning that answers "which CVEs did APT41 exploit to compromise SE Asian telecoms?" in a single traversal.

⚡ 34% token reduction vs LLM-Only
⚡ 50% accuracy improvement
⚡ 2x faster than LLM-Only
⚡ Full attack chain reconstruction from entity graphs

The demo dashboard shows side-by-side responses across all 3 pipelines with live token counts, latency, and cost.

Full code + benchmark: [GitHub link]
Technical write-up: [Blog link]

#TigerGraph #GraphRAG #Cybersecurity #AI #LLM #HackathonProject #ThreatIntelligence
