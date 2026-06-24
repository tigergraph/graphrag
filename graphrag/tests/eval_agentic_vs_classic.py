# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Parity / quality eval harness: agentic vs classic chat engine.

Runs a fixed question set through both engines against a live graph and
reports, per question and in aggregate: answered?, response_type, latency,
and total token usage. Use it to check the agentic engine doesn't regress
the classic one before flipping the default in a deployment.

Run inside the graphrag container (where config + embedding store are
available), e.g.::

    docker exec graphrag python3 -m tests.eval_agentic_vs_classic \\
        --graph SchemaAwareE2E_1778743253 --user tigergraph --password tigergraph

Add ``--questions q1 "q2" ...`` to override the default set.
"""

import argparse
import json
import time

DEFAULT_QUESTIONS = [
    "How many documents are in the graph?",
    "What topics do the documents cover?",
    "How many documents are in the graph and what are they about?",
    "Summarize the main themes across the corpus.",
]


def _run_one(agent, question):
    t0 = time.time()
    answered, rtype, tokens, err = False, "", 0, None
    try:
        resp = agent.question_for_agent(question, [])
        answered = bool(getattr(resp, "answered_question", False))
        rtype = getattr(resp, "response_type", "") or ""
        qs = getattr(resp, "query_sources", None) or {}
        tokens = int((qs.get("token_usage") or {}).get("total_tokens", 0) or 0)
    except Exception as e:  # keep the harness going across questions
        err = f"{type(e).__name__}: {e}"
    return {
        "latency_s": round(time.time() - t0, 2),
        "answered": answered,
        "response_type": rtype,
        "total_tokens": tokens,
        "error": err,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--user", default="tigergraph")
    ap.add_argument("--password", default="tigergraph")
    ap.add_argument("--questions", nargs="*", default=None)
    ap.add_argument("--use-cypher", action="store_true", default=True)
    args = ap.parse_args()

    from common.config import (
        get_chat_config, get_llm_service, get_embedding_service, get_embedding_store,
    )
    from common.db.connections import get_db_connection_pwd_manual
    from agent.agent import TigerGraphAgent
    from agent.agentic_agent import AgenticAgent

    g = args.graph
    conn = get_db_connection_pwd_manual(g, args.user, args.password)
    llm = get_llm_service(get_chat_config(g))
    emb = get_embedding_service()
    store = get_embedding_store(g)

    def make_classic():
        return TigerGraphAgent(llm, conn, emb, store, use_cypher=args.use_cypher, ws=None)

    def make_agentic():
        return AgenticAgent(llm, conn, emb, store, use_cypher=args.use_cypher, ws=None)

    questions = args.questions or DEFAULT_QUESTIONS
    rows = []
    for q in questions:
        classic = _run_one(make_classic(), q)
        agentic = _run_one(make_agentic(), q)
        rows.append({"question": q, "classic": classic, "agentic": agentic})
        print(f"\nQ: {q}")
        print(f"  classic : answered={classic['answered']} type={classic['response_type']:<10} "
              f"{classic['latency_s']}s {classic['total_tokens']} tok"
              + (f"  ERR={classic['error']}" if classic['error'] else ""))
        print(f"  agentic : answered={agentic['answered']} type={agentic['response_type']:<10} "
              f"{agentic['latency_s']}s {agentic['total_tokens']} tok"
              + (f"  ERR={agentic['error']}" if agentic['error'] else ""))

    def _agg(key):
        c = [r["classic"][key] for r in rows]
        a = [r["agentic"][key] for r in rows]
        return c, a

    n = len(rows)
    c_ans = sum(1 for r in rows if r["classic"]["answered"])
    a_ans = sum(1 for r in rows if r["agentic"]["answered"])
    c_lat, a_lat = _agg("latency_s")
    c_tok, a_tok = _agg("total_tokens")
    print("\n=== AGGREGATE ===")
    print(f"  answered:   classic {c_ans}/{n}   agentic {a_ans}/{n}")
    print(f"  avg latency: classic {sum(c_lat)/n:.1f}s   agentic {sum(a_lat)/n:.1f}s")
    print(f"  avg tokens:  classic {sum(c_tok)//n}   agentic {sum(a_tok)//n}")
    print("\nEVAL_JSON: " + json.dumps({
        "n": n, "classic_answered": c_ans, "agentic_answered": a_ans,
        "classic_avg_latency": round(sum(c_lat)/n, 2), "agentic_avg_latency": round(sum(a_lat)/n, 2),
        "classic_avg_tokens": sum(c_tok)//n, "agentic_avg_tokens": sum(a_tok)//n,
    }))


if __name__ == "__main__":
    main()
