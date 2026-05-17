"""
CyberGraph Dashboard API Server
Serves the benchmark dashboard and proxies pipeline calls.
Run: python dashboard_api.py
Visit: http://localhost:8888
"""
import sys, os, json, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# Add parent dir so we can import benchmark_engine
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from benchmark_engine import (
    pipeline_llm_only, pipeline_basic_rag, pipeline_graphrag,
    evaluate_response, BENCHMARK_QUERIES
)

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "dashboard")
PORT = int(os.environ.get("PORT", 8888))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [{time.strftime('%H:%M:%S')}] {fmt % args}", flush=True)

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(data))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(200, b"")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            fpath = os.path.join(DASHBOARD_DIR, "index.html")
            with open(fpath, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/health":
            self._send(200, json.dumps({"status": "ok"}))
        elif path == "/queries":
            self._send(200, json.dumps([{"id": q["id"], "query": q["query"]} for q in BENCHMARK_QUERIES]))
        else:
            self._send(404, json.dumps({"error": "Not found"}))

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/benchmark":
            query = body.get("query", "")
            evaluate = body.get("evaluate", False)
            ground_truth = body.get("ground_truth", "")
            skip_graphrag = body.get("skip_graphrag", False)

            if not query:
                self._send(400, json.dumps({"error": "query required"}))
                return

            print(f"\n  Query: {query[:80]}", flush=True)

            # Run all 3 pipelines
            print("  [1/3] LLM-Only...", flush=True)
            r_llm = pipeline_llm_only(query)

            print("  [2/3] Basic RAG...", flush=True)
            r_rag = pipeline_basic_rag(query)

            print("  [3/3] GraphRAG...", flush=True)
            r_graph = pipeline_graphrag(query) if not skip_graphrag else {
                "response": "GraphRAG not yet available — graph ingestion in progress.",
                "total_tokens": 0, "latency": 0.0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
            }

            # Evaluate if requested
            eval_results = {}
            if evaluate and ground_truth:
                print("  [Eval] Running LLM-as-Judge...", flush=True)
                eval_results = {
                    "llm_only":  evaluate_response(query, ground_truth, r_llm["response"]),
                    "basic_rag": evaluate_response(query, ground_truth, r_rag["response"]),
                    "graphrag":  evaluate_response(query, ground_truth, r_graph["response"]),
                }
                r_llm["evaluation"]  = eval_results["llm_only"]
                r_rag["evaluation"]  = eval_results["basic_rag"]
                r_graph["evaluation"] = eval_results["graphrag"]

            result = {
                "query": query,
                "llm_only":  r_llm,
                "basic_rag": r_rag,
                "graphrag":  r_graph,
            }
            self._send(200, json.dumps(result, ensure_ascii=False))

        elif path == "/benchmark/full":
            # Run all preset queries
            from benchmark_engine import run_benchmark
            results = run_benchmark(evaluate=body.get("evaluate", False),
                                    skip_graphrag=body.get("skip_graphrag", False))
            self._send(200, json.dumps(results, ensure_ascii=False))

        else:
            self._send(404, json.dumps({"error": "Not found"}))


def main():
    print(f"\n{'='*60}")
    print(f"  CyberGraph Dashboard API — http://localhost:{PORT}")
    print(f"  Dashboard UI — http://localhost:{PORT}/")
    print(f"{'='*60}\n")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")

if __name__ == "__main__":
    main()
