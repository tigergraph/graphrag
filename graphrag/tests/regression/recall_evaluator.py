"""GraphRAG Regression — Recall@K Evaluator

Measures retrieval recall against a labelled dataset using:

    Recall@K = (ground-truth chunks found in top-K retrieved chunks)
               / (total ground-truth chunks for that question)

Averaged over all questions to produce a final Avg Recall@K score.

Dataset layout expected:
    test_questions/<DatasetName>/
    ├── data/<source>_corpus.txt      — one raw text file per source (GraphRAG chunks naturally)
    ├── questions.csv                 — single column: question
    └── ground_truth_chunks.csv       — columns: question_index, chunk_index, context
                                        Recall denominator = GT chunks per question (not total dataset chunks).

Matching strategy — two options via --match:

  embedding (default):
    Each GT chunk and each retrieved chunk are embedded with the model from
    server_config.json (e.g. gemini-embedding-001).  A GT chunk is "found" when
    cosine similarity ≥ threshold (default 0.75).  Handles paraphrasing and
    contextual retrievers correctly.

  llm:
    An LLM (same model as completions in server_config.json) judges whether the
    retrieved chunk contains the same factual information as the GT chunk.
    Most accurate but slowest and costliest.

Run via:
    ./graphrag/tests/regression/run_recall.sh --dataset Multihop30 --graphname <name>
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

if "/code" not in sys.path:
    sys.path.insert(0, "/code")

warnings.filterwarnings("ignore")
warnings.showwarning = lambda *a, **k: None
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ── ANSI colours (mirrors evaluator.py) ──────────────────────────────────────
if os.environ.get("NO_COLOR"):
    _USE_COLOR = False
elif os.environ.get("FORCE_COLOR"):
    _USE_COLOR = True
else:
    _USE_COLOR = sys.stdout.isatty()

_G = "\033[32m" if _USE_COLOR else ""
_R = "\033[31m" if _USE_COLOR else ""
_Y = "\033[33m" if _USE_COLOR else ""
_C = "\033[36m" if _USE_COLOR else ""
_B = "\033[1m"  if _USE_COLOR else ""
_X = "\033[0m"  if _USE_COLOR else ""

# ── Constants ─────────────────────────────────────────────────────────────────
_DEFAULT_K         = 5
_MAX_CHUNKS        = 50     # max chunks extracted from GraphRAG response
_MAX_CHUNK_CH      = 5000   # truncation per chunk before matching — keep higher than GT chunk size (4000)
_EMBED_THRESHOLD   = 0.70   # cosine similarity threshold for embedding match
                            # Calibrated on Multihop30: genuine misses cluster at 0.54–0.64;
                            # true matches cluster at 0.70–0.85. Gap is clear and stable.
_EMBED_MAX_CHARS   = 8000   # truncate text before embedding (API limit safety)
_EMBED_CONCURRENCY = 4      # max parallel embedding API calls
_LLM_CHUNK_CHARS   = 4000   # truncate chunk text sent to LLM judge

# Corpus passages are stored in the graph with a "===== Title =====" header prepended
# by the ingestion pipeline.  GT chunks never have this header, so we strip it from
# retrieved text before embedding / LLM comparison to avoid a spurious similarity drop.
_TITLE_HEADER_RE = re.compile(r"^\s*={2,}[^=\n]+={2,}\s*\n?", re.MULTILINE)

# ── Shared caches (embedding vectors, LLM verdicts) ───────────────────────────
_embed_cache: Dict[str, List[float]] = {}
_llm_cache:   Dict[str, bool]        = {}
_embed_lock   = threading.Lock()
_llm_lock     = threading.Lock()
_embed_sem    = threading.Semaphore(_EMBED_CONCURRENCY)


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class RecallQuestion:
    index: int            # 1-based, matches question_index in ground_truth_contexts.csv
    question: str
    gt_contexts: List[str]                        # GT chunk text (from ground_truth_chunks.csv)
    gt_titles:   List[Optional[str]] = field(default_factory=list)  # Wikipedia title (may be None)


@dataclass
class RecallResult:
    # ── written to CSV ────────────────────────────────────────────────────────
    question_index: int
    question: str
    gt_context_count: int           = 0
    retrieved_chunk_count: int      = 0
    matched_count: int              = 0
    recall_at_k: Optional[float]    = None
    agent_mode: Optional[str]       = None
    search_type_used: Optional[str] = None
    response_time_seconds: float    = 0.0
    # ── CLI-only ──────────────────────────────────────────────────────────────
    error: Optional[str]            = field(default=None, repr=False)
    answered_question: bool         = field(default=False, repr=False)
    matched_indices: List[int]      = field(default_factory=list, repr=False)
    unmatched_contexts: List[str]   = field(default_factory=list, repr=False)
    embed_scores: List[float]       = field(default_factory=list, repr=False)  # max cosine per GT chunk


# ─── Loaders ─────────────────────────────────────────────────────────────────

def _detect_encoding(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read(4)
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    try:
        import chardet
        with open(path, "rb") as f:
            detected = chardet.detect(f.read())
        enc = detected.get("encoding") or "utf-8"
        return enc if (detected.get("confidence") or 0) >= 0.5 else "utf-8"
    except ImportError:
        return "utf-8"


def _read_single_column(path: str, column: str) -> List[str]:
    encoding = _detect_encoding(path)
    with open(path, newline="", encoding=encoding) as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        sys.exit(f"ERROR: {os.path.basename(path)} is empty.")
    header = rows[0]
    try:
        col_idx = header.index(column)
    except ValueError:
        sys.exit(
            f"ERROR: {os.path.basename(path)} must have a '{column}' column. "
            f"Found: {header}"
        )
    results = []
    for row in rows[1:]:
        if not row:
            continue
        value = ",".join(row[col_idx:]).strip()
        if value:
            results.append(value)
    return results


def load_recall_questions(dataset_dir: str) -> List[RecallQuestion]:
    """Load questions.csv and ground_truth_chunks.csv.

    ground_truth_chunks.csv columns:
      question_index  — 1-based int
      chunk_index     — chunk number within the question (1-based)
      context         — chunk text (~400-500 chars)

    Denominator for Recall@K is the number of GT chunks for THAT question,
    not the total chunks in the dataset.
    Run build_gt_chunks.py once after setup to generate this file.
    """
    q_path      = os.path.join(dataset_dir, "questions.csv")
    chunks_path = os.path.join(dataset_dir, "ground_truth_chunks.csv")

    if not os.path.exists(q_path):
        sys.exit(f"ERROR: questions.csv not found at {q_path}")
    if not os.path.exists(chunks_path):
        sys.exit(
            f"ERROR: ground_truth_chunks.csv not found in {dataset_dir}.\n"
            f"Run build_gt_chunks.py first:\n"
            f"  python {dataset_dir}/build_gt_chunks.py"
        )

    questions_raw = _read_single_column(q_path, "question")

    encoding = _detect_encoding(chunks_path)
    gt_map: Dict[int, List[str]] = {}
    with open(chunks_path, newline="", encoding=encoding) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                idx = int(row["question_index"])
            except (KeyError, ValueError):
                continue
            ctx = row.get("context", "").strip()
            if ctx:
                gt_map.setdefault(idx, []).append(ctx)

    result: List[RecallQuestion] = []
    for i, question in enumerate(questions_raw, start=1):
        chunks = gt_map.get(i, [])
        if not chunks:
            print(f"  {_Y}WARNING: no GT chunks for Q{i} — "
                  f"recall will be 0 for this question{_X}", flush=True)
        result.append(RecallQuestion(
            index=i, question=question,
            gt_contexts=chunks, gt_titles=[],
        ))

    return result


# ─── Embedding cosine similarity matching ────────────────────────────────────

def _cosine(a: List[float], b: List[float]) -> float:
    dot    = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _get_embedding_service():
    """Return the GraphRAG embedding service (already configured in the container)."""
    from common.config import get_embedding_service
    return get_embedding_service()


def _embed(text: str, **_kwargs) -> Optional[List[float]]:
    """Return embedding for text using GraphRAG's configured embedding service (cached).
    Extra kwargs (api_key, model) are accepted but ignored — config comes from
    the container's server_config.json via common.config.get_embedding_service().
    Returns None on any error.
    """
    cache_key = text[:200]
    with _embed_lock:
        if cache_key in _embed_cache:
            return _embed_cache[cache_key]
    try:
        with _embed_sem:
            svc = _get_embedding_service()
            vecs = svc.embed_documents([text[:_EMBED_MAX_CHARS]])
        vec = vecs[0] if vecs else None
        if vec:
            with _embed_lock:
                _embed_cache[cache_key] = vec
        return vec
    except Exception as e:
        logging.warning("Embedding error: %s", e)
        return None


def _check_embedding_api(**_kwargs) -> Optional[str]:
    """Return None if the embedding service works, else an error string."""
    vec = _embed("ping")
    if vec is None:
        return "embedding service unavailable — check container logs"
    return None


def _chunk_found_embedding(gt_chunk: str, chunks: List[str],
                            threshold: float = _EMBED_THRESHOLD,
                            **_kwargs) -> Tuple[bool, float]:
    """Check whether any retrieved chunk is similar enough to the GT chunk.

    Returns:
        (found, max_cosine_similarity)
        found  — True if max_cosine_similarity >= threshold
        max_cosine_similarity — highest cosine score observed (0.0 if no embeddings)
    """
    gt_vec = _embed(gt_chunk)
    if gt_vec is None:
        return False, 0.0
    max_sim = 0.0
    for retrieved in chunks:
        retrieved_vec = _embed(retrieved)
        if retrieved_vec is not None:
            sim = _cosine(gt_vec, retrieved_vec)
            if sim > max_sim:
                max_sim = sim
    return max_sim >= threshold, max_sim


# ─── LLM judge matching ────────────────────────────────────────────────────────

_LLM_PROMPT = """\
You are evaluating information retrieval quality for a RAG system.

Ground-truth passage (the relevant source text):
{gt}

Retrieved chunk (returned by the retriever):
{chunk}

Task: Does the retrieved chunk contain information that is RELEVANT to the ground-truth passage?
The retrieved chunk does NOT need to be identical or cover everything — answer YES if it overlaps
on the same topic, entity, or key facts. Answer NO only if the retrieved chunk is clearly about
a completely different topic with no meaningful overlap.

Answer with a single word: YES or NO."""


def _llm_judge(gt_chunk: str, retrieved_chunk: str, **_kwargs) -> bool:
    """Ask GraphRAG's configured LLM whether the retrieved chunk contains the GT chunk's information.
    Uses the same get_llm_service / get_chat_config pattern as evaluator.py.
    Returns True for YES, False for NO or on any error.
    """
    cache_key = f"{gt_chunk[:100]}|||{retrieved_chunk[:100]}"
    with _llm_lock:
        if cache_key in _llm_cache:
            return _llm_cache[cache_key]
    try:
        from common.config import get_chat_config, get_llm_service
        from langchain_core.messages import HumanMessage

        cfg = get_chat_config()
        llm = get_llm_service(cfg)
        lc  = getattr(llm, "llm", None) or llm
        prompt = _LLM_PROMPT.format(
            gt=gt_chunk[:_LLM_CHUNK_CHARS],
            chunk=retrieved_chunk[:_LLM_CHUNK_CHARS],
        )
        resp    = lc.invoke([HumanMessage(content=prompt)])
        text    = (resp.content if hasattr(resp, "content") else str(resp)).strip().upper()
        verdict = text.startswith("YES")
        with _llm_lock:
            _llm_cache[cache_key] = verdict
        return verdict
    except Exception as e:
        logging.warning("LLM judge error: %s", e)
        return False


def _chunk_found_llm(gt_chunk: str, retrieved_chunks: List[str]) -> bool:
    """True if the LLM judges any retrieved chunk as relevant to the GT chunk."""
    for retrieved in retrieved_chunks:
        if _llm_judge(gt_chunk, retrieved):
            return True
    return False


# ─── Unified recall computation ───────────────────────────────────────────────

def _compute_recall(
    gt_contexts: List[str],
    top_k_chunks: List[str],
    match_cfg: Optional[Dict[str, Any]] = None,
    embed_threshold: float = _EMBED_THRESHOLD,
    gt_titles: Optional[List[Optional[str]]] = None,  # unused, kept for compat
) -> Tuple[float, int, List[int], List[str], List[float]]:
    """Compute Recall@K for one question.

    match_cfg keys:
      strategy  — "embedding" (default) or "llm"

    Returns:
        recall, matched_count, matched_indices, unmatched_chunks, embed_scores
        embed_scores — max cosine similarity per GT chunk (empty list when strategy="llm")
    """
    if not gt_contexts:
        return 0.0, 0, [], [], []

    strategy = (match_cfg or {}).get("strategy", "embedding")

    matched_idxs: List[int] = []
    unmatched:    List[str] = []
    embed_scores: List[float] = []

    for i, gt in enumerate(gt_contexts):
        if strategy == "llm":
            found = _chunk_found_llm(gt, top_k_chunks)
            embed_scores.append(-1.0)  # not applicable
        else:
            found, score = _chunk_found_embedding(gt, top_k_chunks, threshold=embed_threshold)
            embed_scores.append(score)

        if found:
            matched_idxs.append(i)
        else:
            unmatched.append(gt)

    recall = len(matched_idxs) / len(gt_contexts)
    return recall, len(matched_idxs), matched_idxs, unmatched, embed_scores


# ─── GraphRAG query ───────────────────────────────────────────────────────────

_RETRIEVER_LABELS = {
    "similaritysearch": "Similarity Search",
    "contextualsearch": "Contextual Search",
    "hybridsearch":     "Hybrid Search",
    "communitysearch":  "Community Search",
}


def _query_graphrag(
    url: str,
    graphname: str,
    username: str,
    password: str,
    question: str,
    mode: str,
    rag_pattern: str,
) -> dict:
    resp = httpx.get(
        f"{url}/ui/{graphname}/query",
        params={
            "q":              question,
            "mode":           mode,
            "rag_pattern":    rag_pattern,
            "include_fields": "query_sources",
        },
        auth=(username, password),
        timeout=600.0,  # agentic mode can take >300s on complex multi-hop questions
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _extract_chunk_texts(final_retrieval: dict) -> List[str]:
    """Extract text strings from a final_retrieval dict.

    Each value is either:
      - list[str]  → one text per item (common case after ingestion)
      - str        → single text value
      - dict       → extract 'content' or 'text' sub-key
    """
    texts: List[str] = []
    for v in final_retrieval.values():
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    texts.append(item)
                elif isinstance(item, dict):
                    t = item.get("content") or item.get("text") or ""
                    if t:
                        texts.append(str(t))
        elif isinstance(v, str) and v.strip():
            texts.append(v)
        elif isinstance(v, dict):
            t = v.get("content") or v.get("text") or ""
            if t:
                texts.append(str(t))
    return texts


def _extract_top_k_chunks(query_sources: dict, k: int, mode: str = "classic") -> List[str]:
    """Extract retrieved text chunks from the GraphRAG response.

    Classic mode (similarity/hybrid/contextual/community search):
        Chunks are ordered by relevance score — most relevant first.
        We return only the first k so Recall@K is honest and cannot
        benefit from chunks beyond rank k.

    Agentic mode (planned/reactive/auto agent):
        The agent calls multiple tools in arbitrary sequence — there is no
        single relevance ranking across tool calls.  Applying a k-cutoff
        here would silently drop relevant chunks the agent DID retrieve but
        that happen to appear after position k in dict-iteration order.
        We therefore return ALL chunks the agent retrieved (up to _MAX_CHUNKS)
        so the recall score reflects what the agent actually found, not an
        arbitrary ordering artefact.

    Chunk extraction strategy:
        1. Top-level  result.final_retrieval  — populated in classic mode.
        2. Per-step   result.unstructured[*].result.final_retrieval  — populated
           in agentic mode for each vector/hybrid search tool call.
        3. NO general fallback walker: agentic structural-only answers produce
           no text chunks; falling back to walking the whole response would
           extract Cypher query strings and reasoning text as fake chunks, leading
           to artificially high LLM recall (entity name matching) and artificially
           low embedding recall (near-zero cosine similarity with real passages).

    Title-header stripping:
        The ingestion pipeline prepends "===== Title =====" to each stored chunk.
        GT chunks contain only the passage body, so we strip headers before
        comparison to avoid spurious similarity penalties.
    """
    if not query_sources:
        return []

    result = query_sources.get("result") or {}
    raw: List[str] = []

    # ── Path 1: top-level final_retrieval (classic mode, and some agentic configs) ──
    top_fr = result.get("final_retrieval") or {}
    if isinstance(top_fr, dict) and top_fr:
        raw.extend(_extract_chunk_texts(top_fr))

    # ── Path 2: per-step unstructured results (agentic mode) ────────────────────────
    if not raw:
        for u_item in result.get("unstructured") or []:
            u_result = u_item.get("result") if isinstance(u_item, dict) else None
            if not isinstance(u_result, dict):
                continue
            inner_fr = u_result.get("final_retrieval") or {}
            if isinstance(inner_fr, dict) and inner_fr:
                raw.extend(_extract_chunk_texts(inner_fr))

    # ── Deduplicate, strip title headers, apply length guard, cap at _MAX_CHUNKS ────
    seen: set = set()
    out: List[str] = []
    for text in raw:
        # Strip "===== Title =====" header added by ingestion pipeline
        text = _TITLE_HEADER_RE.sub("", text, count=1).strip()
        if len(text) < 5 or text in seen:
            continue
        seen.add(text)
        out.append(text[:_MAX_CHUNK_CH])
        if len(out) >= _MAX_CHUNKS:
            break

    # For classic mode honour the K limit (ranked by relevance).
    # For agentic mode return everything the agent retrieved — no meaningful rank exists.
    if mode == "agentic":
        return out
    return out[:k]


# ─── Per-question evaluation ──────────────────────────────────────────────────

def _eval_one_recall(
    rq: RecallQuestion,
    graphname: str,
    url: str,
    username: str,
    password: str,
    mode: str,
    rag_pattern: str,
    k: int,
    match_cfg: Optional[Dict[str, Any]] = None,
    embed_threshold: float = _EMBED_THRESHOLD,
) -> RecallResult:
    r = RecallResult(
        question_index=rq.index,
        question=rq.question,
        gt_context_count=len(rq.gt_contexts),
    )
    t0 = time.monotonic()

    try:
        data = _query_graphrag(url, graphname, username, password, rq.question, mode, rag_pattern)
        r.answered_question = bool(data.get("answered_question", False))
        query_sources = data.get("query_sources") or {}
        _chosen = (query_sources.get("chosen_retriever") or "").lower().replace(" ", "")
        r.search_type_used = _RETRIEVER_LABELS.get(_chosen) or data.get("response_type") or None
        r.agent_mode = f"{mode}/{rag_pattern}"

        top_k = _extract_top_k_chunks(query_sources, k, mode=mode)
        r.retrieved_chunk_count = len(top_k)
    except httpx.HTTPStatusError as e:
        r.error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        r.response_time_seconds = time.monotonic() - t0
        return r
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}"
        r.response_time_seconds = time.monotonic() - t0
        return r
    finally:
        r.response_time_seconds = time.monotonic() - t0

    if not rq.gt_contexts:
        r.recall_at_k = 0.0
        return r

    if not top_k:
        # No text chunks retrieved — likely a structural-only (graph traversal) answer.
        # Retrieval recall cannot be measured without text chunks.
        r.recall_at_k  = 0.0
        r.matched_count = 0
        r.unmatched_contexts = list(rq.gt_contexts)
        return r

    recall, matched, matched_idxs, unmatched, embed_scores = _compute_recall(
        rq.gt_contexts, top_k,
        match_cfg=match_cfg,
        embed_threshold=embed_threshold,
    )
    r.recall_at_k        = round(recall, 4)
    r.matched_count      = matched
    r.matched_indices    = matched_idxs
    r.unmatched_contexts = unmatched
    r.embed_scores       = embed_scores

    return r


# ─── Compact / detailed printer ──────────────────────────────────────────────

def _recall_cell(r: RecallResult) -> str:
    if r.recall_at_k is None:
        return f"{_Y}  n/a{_X}"
    pct = r.recall_at_k * 100
    col = _G if pct >= 60 else _Y if pct >= 30 else _R
    bar_filled = int(pct / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    return f"{col}{pct:5.1f}%  {bar}{_X}"


def _print_compact(r: RecallResult, total: int, k: int) -> None:
    n = r.question_index
    if r.error:
        print(f"  Q{n:>3}/{total}  [{r.response_time_seconds:5.1f}s]  "
              f"{_R}error: {r.error[:100]}{_X}", flush=True)
        return
    if r.gt_context_count == 0:
        print(f"  Q{n:>3}/{total}  [{r.response_time_seconds:5.1f}s]  "
              f"{_Y}no GT contexts — skipped{_X}", flush=True)
        return
    if r.retrieved_chunk_count == 0:
        print(f"  Q{n:>3}/{total}  [{r.response_time_seconds:5.1f}s]  "
              f"{_Y}no text chunks (structural-only answer) — recall=0{_X}", flush=True)
        return
    scope = f"top-{k}" if r.retrieved_chunk_count <= k else f"all-{r.retrieved_chunk_count}"
    frac = f"({r.matched_count}/{r.gt_context_count} GT chunks in {scope})"
    print(
        f"  Q{n:>3}/{total}  [{r.response_time_seconds:5.1f}s]  "
        f"Recall@{k}={_recall_cell(r)}  {frac}",
        flush=True,
    )


def _print_detailed(r: RecallResult, total: int, k: int) -> None:
    n = r.question_index
    q_short = (r.question[:70] + "…") if len(r.question) > 71 else r.question
    print(f"  {'─' * 70}", flush=True)
    print(f"  {_B}Q{n}/{total}{_X}  [{r.response_time_seconds:.1f}s]  {q_short}", flush=True)
    if r.error:
        print(f"  {_R}error: {r.error}{_X}", flush=True)
        return
    if r.gt_context_count == 0:
        print(f"  {_Y}no ground-truth contexts — skipped{_X}", flush=True)
        return
    print(
        f"  {_C}Recall@{k}{_X} = {_recall_cell(r)}  "
        f"matched {r.matched_count}/{r.gt_context_count}  "
        f"(retrieved {r.retrieved_chunk_count} chunks)",
        flush=True,
    )
    if r.unmatched_contexts:
        strategy = "embedding"  # default; score list is non-negative only for embedding
        print(f"  {_Y}Unmatched GT chunks:{_X}", flush=True)
        # Build a map: gt_text → embed score (for embedding runs only)
        score_map: Dict[str, float] = {}
        if r.embed_scores and any(s >= 0 for s in r.embed_scores):
            # embed_scores is aligned with gt_contexts; reconstruct unmatched scores
            for idx, (score, _matched) in enumerate(
                zip(r.embed_scores, [i in r.matched_indices for i in range(len(r.embed_scores))])
            ):
                pass
            # Simpler: zip embed_scores with all GT chunks, show score for unmatched
            all_gt = r.unmatched_contexts  # only unmatched are in this list
            unmatched_scores = [s for i, s in enumerate(r.embed_scores) if i not in r.matched_indices]
            for uc, sc in zip(all_gt, unmatched_scores if unmatched_scores else [None]*len(all_gt)):
                score_str = f"  max_cos={sc:.3f}" if sc is not None and sc >= 0 else ""
                print(f"    - {uc[:100]}{'…' if len(uc) > 100 else ''}{score_str}", flush=True)
        else:
            for uc in r.unmatched_contexts:
                print(f"    - {uc[:120]}{'…' if len(uc) > 120 else ''}", flush=True)


# ─── Main eval loop ───────────────────────────────────────────────────────────

def _parse_mode(mode_str: str) -> Tuple[str, str]:
    """Parse --mode into (api_mode, rag_pattern).

    Agentic styles  (mode=agentic, rag_pattern=<style>):
      planned                → ("agentic", "planned")
      reactive               → ("agentic", "reactive")
      auto                   → ("agentic", "auto")   ← graph config picks the style

    Classic retrievers  (mode=classic, rag_pattern=<retriever>):
      classic                → ("classic", "auto")
      classic/auto           → ("classic", "auto")
      classic/similaritysearch → ("classic", "similaritysearch")
      classic/hybridsearch     → ("classic", "hybridsearch")
      classic/contextualsearch → ("classic", "contextualsearch")
      classic/communitysearch  → ("classic", "communitysearch")
    """
    s = mode_str.strip().lower()
    agent, _, retriever = s.partition("/")
    retriever = retriever or None

    if agent == "planned":
        return "agentic", "planned"
    if agent == "reactive":
        return "agentic", "reactive"
    if agent == "auto":
        return "agentic", "auto"
    if agent in ("agentic",):
        return "agentic", retriever or "auto"
    if agent == "classic":
        return "classic", retriever or "auto"

    sys.exit(
        f"ERROR: unrecognised --mode '{mode_str}'. "
        "Use: auto | planned | reactive | classic | classic/<retriever>"
    )


def run_recall_eval(
    questions: List[RecallQuestion],
    graphname: str,
    url: str,
    username: str,
    password: str,
    mode: str = "classic",
    rag_pattern: str = "auto",
    k: int = _DEFAULT_K,
    detailed: bool = False,
    match_cfg: Optional[Dict[str, Any]] = None,
    embed_threshold: float = _EMBED_THRESHOLD,
) -> List[RecallResult]:
    total   = len(questions)
    workers = min(4, total)   # 4 parallel keeps the GraphRAG server from queuing under load
    results: List[Optional[RecallResult]] = [None] * total
    printer = _print_detailed if detailed else _print_compact

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recall") as pool:
        futures = {
            pool.submit(
                _eval_one_recall,
                rq, graphname, url, username, password, mode, rag_pattern, k,
                match_cfg, embed_threshold,
            ): rq.index
            for rq in questions
        }
        for future in as_completed(futures):
            r = future.result()
            results[r.question_index - 1] = r
            printer(r, total, k)

    return [r for r in results if r is not None]


# ─── Output ───────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "question_index", "question",
    "gt_context_count", "retrieved_chunk_count", "matched_count", "recall_at_k",
    "agent_mode", "search_type_used", "response_time_seconds",
]


def write_recall_results(
    results: List[RecallResult],
    output_dir: str,
    k: int,
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(output_dir, f"recall_{ts}.csv")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for r in results:
            row = asdict(r)
            writer.writerow({col: row[col] for col in _CSV_FIELDS})

    # ── Summary ───────────────────────────────────────────────────────────────
    errors            = [r for r in results if r.error]
    no_gt             = [r for r in results if not r.error and r.gt_context_count == 0]
    structural_only   = [r for r in results if not r.error and r.gt_context_count > 0 and r.retrieved_chunk_count == 0]
    no_chunks         = structural_only  # keep alias for backwards compat
    valid             = [r for r in results if r.recall_at_k is not None]
    avg_recall   = sum(r.recall_at_k for r in valid) / len(valid) if valid else None
    agent_label  = results[0].agent_mode if results else "unknown"

    print(f"\n  {'═' * 60}")
    print(f"  {_B}Recall@{k} Summary{_X}  [{_C}{agent_label}{_X}]")
    print(f"  {'═' * 60}")
    print(f"  Questions evaluated     : {len(results)}")
    if errors:
        print(f"  Errors                  : {_R}{len(errors)}{_X}  "
              f"(Q{', Q'.join(str(r.question_index) for r in errors)})")
        for r in errors:
            print(f"    Q{r.question_index}: {r.error}", flush=True)
    if structural_only:
        print(f"  Structural-only answers : {_Y}{len(structural_only)}{_X}  "
              f"(Q{', Q'.join(str(r.question_index) for r in structural_only)})  "
              f"— graph traversal used, no text chunks, recall=0")

    if avg_recall is not None:
        pct = avg_recall * 100
        col = _G if pct >= 60 else _Y if pct >= 30 else _R
        print(f"  {_C}Avg Recall@{k}{_X}           : {col}{pct:.1f}%{_X}")

        # Per-decile breakdown
        perfect   = sum(1 for r in valid if r.recall_at_k is not None and r.recall_at_k == 1.0)
        partial   = sum(1 for r in valid if r.recall_at_k is not None and 0 < r.recall_at_k < 1.0)
        zero_hits = sum(1 for r in valid if r.recall_at_k is not None and r.recall_at_k == 0.0)
        print(f"  Full recall  (1.0)      : {_G}{perfect}{_X} questions")
        print(f"  Partial recall (0<r<1)  : {_Y}{partial}{_X} questions")
        print(f"  Zero recall  (0.0)      : {_R}{zero_hits}{_X} questions")
    else:
        print(f"  Avg Recall@{k}           : — (no valid results)")

    print(f"  {'═' * 60}")
    print(f"\n  CSV : {csv_path}\n")


# ─── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="GraphRAG Recall@K Evaluator — measures retrieval quality "
                    "using ground-truth supporting chunks."
    )
    parser.add_argument("--dataset",   required=True,
                        help="Dataset name (folder under graphrag/tests/test_questions/)")
    parser.add_argument("--graphname", required=True,
                        help="GraphRAG graph name to query")
    parser.add_argument("--config",    default="configs/server_config.json",
                        help="Path to server_config.json")
    parser.add_argument("--url",       default=None,
                        help="GraphRAG base URL (overrides server_config.json)")
    parser.add_argument("--mode", default="classic/auto",
                        help=(
                            "Query mode as <agent>[/<retriever>]. Default: classic/auto. "
                            "Examples: classic/auto  classic/similaritysearch  "
                            "classic/hybridsearch  planned  reactive"
                        ))
    parser.add_argument("--match",     default="embedding",
                        choices=["embedding", "llm"],
                        help="Matching strategy: embedding (default, cosine similarity) "
                             "or llm (LLM judge, most accurate but slower/costlier)")
    parser.add_argument("--embed-threshold", type=float, default=_EMBED_THRESHOLD,
                        help=f"Cosine similarity threshold for --match embedding "
                             f"(default: {_EMBED_THRESHOLD})")
    parser.add_argument("--k",         type=int, default=_DEFAULT_K,
                        help=f"Top-K retrieved chunks to check (default: {_DEFAULT_K})")
    parser.add_argument("--limit",     type=int, default=None,
                        help="Only run the first N questions")
    parser.add_argument("--detailed",  action="store_true",
                        help="Print per-question breakdown including unmatched GT chunks")
    parser.add_argument("--output",    default=None,
                        help="Output directory for result CSV")
    args = parser.parse_args()

    logging.disable(logging.INFO)
    warnings.simplefilter("ignore")

    config_path = os.path.abspath(args.config)
    if not os.path.exists(config_path):
        sys.exit(f"ERROR: config file not found: {config_path}")
    os.environ.setdefault("SERVER_CONFIG", config_path)

    with open(config_path) as f:
        server_cfg = json.load(f)

    db       = server_cfg.get("db_config", {})
    username = db.get("username", "")
    password = db.get("password", "")
    if not username or not password:
        sys.exit(f"ERROR: db_config.username / password not set in {config_path}")

    graphrag_url = (
        args.url
        or server_cfg.get("graphrag_config", {}).get("query_url", "http://localhost:8000")
    ).rstrip("/")

    dataset_dir = os.path.join("/code/tests/test_questions", args.dataset)
    if not os.path.isdir(dataset_dir):
        sys.exit(f"ERROR: dataset directory not found: {dataset_dir}")

    output_dir = args.output or os.path.join(os.path.dirname(__file__), "results")

    mode, rag_pattern = _parse_mode(args.mode)

    # Extract model names from server_config for display purposes only
    llm_cfg   = server_cfg.get("llm_config", {})
    emb_model = llm_cfg.get("embedding_service", {}).get("model_name", "configured")
    llm_model = llm_cfg.get("completion_service", {}).get("llm_model", "configured")

    match_cfg: Dict[str, Any] = {"strategy": args.match}

    # Verify the chosen matching service is reachable before running all questions
    if args.match == "embedding":
        logging.disable(logging.NOTSET)
        err = _check_embedding_api()
        logging.disable(logging.INFO)
        if err:
            sys.exit(f"ERROR: {err}")

    questions = load_recall_questions(dataset_dir)
    if args.limit:
        questions = questions[:args.limit]

    match_label = (
        f"embedding (model={emb_model}, threshold={args.embed_threshold})"
        if args.match == "embedding"
        else f"llm (model={llm_model})"
    )
    print(
        f"\n{_B}GraphRAG Recall@{args.k} Evaluation{_X}  "
        f"dataset={args.dataset}  graph={args.graphname}  "
        f"mode={args.mode}  match={match_label}  questions={len(questions)}"
        + (f"  {_Y}(limit={args.limit}){_X}" if args.limit else "")
        + "\n",
        flush=True,
    )

    results = run_recall_eval(
        questions       = questions,
        graphname       = args.graphname,
        url             = graphrag_url,
        username        = username,
        password        = password,
        mode            = mode,
        rag_pattern     = rag_pattern,
        k               = args.k,
        detailed        = args.detailed,
        match_cfg       = match_cfg,
        embed_threshold = args.embed_threshold,
    )
    write_recall_results(results, output_dir, args.k)

    sys.stdout.flush()
    sys.stderr.flush()
    exit_code = 1 if any(r.error for r in results) else 0
    os._exit(exit_code)
