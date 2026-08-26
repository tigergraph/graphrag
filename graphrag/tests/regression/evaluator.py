"""GraphRAG Regression Evaluator

Scores a live GraphRAG graph against a labelled dataset using two metrics:
  1. Hallucination      — TigerGraphAgentHallucinationCheck (confidence 0–1)
  2. Answer Correctness — DeepEval GEval (requires answers.csv)

Dataset layout expected:
    test_questions/<DatasetName>/
    ├── data/           documents for ingest
    ├── questions.csv   single column: question
    ├── answers.csv     single column: ground_truth  (optional)
    └── ExportedGraph/  pre-built graph for --load-exported mode

Run via:
    ./graphrag/tests/regression/run_eval.sh --dataset <name> --graphname <graph>
    ./graphrag/tests/regression/run_eval.sh ... --detailed   (shows per-question reasons)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

# Silence DeepEval's progress banner + telemetry. Must be set before deepeval
# is imported anywhere (it is imported lazily inside answer_correctness).
os.environ.setdefault("DEEPEVAL_DISABLE_INDICATOR", "YES")
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

# Some libraries (langchain, opentelemetry, pyTigerGraph …) re-arm the warnings
# filter on import, so PYTHONWARNINGS / simplefilter alone don't hold. Replacing
# showwarning with a no-op drops every warning no matter what filter is active.
warnings.filterwarnings("ignore")
warnings.showwarning = lambda *a, **k: None

import httpx

# Python sets sys.path[0] to the script's directory, not /code, so `import common`
# and `import agent` fail without this. /code is where the GraphRAG app lives in
# the container (see Dockerfile: COPY graphrag/app /code && COPY common /code/common).
if "/code" not in sys.path:
    sys.path.insert(0, "/code")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ANSI colours for clean CLI output. NO_COLOR disables; FORCE_COLOR forces on
# (needed under `docker exec` where stdout is often not a detected TTY).
if os.environ.get("NO_COLOR"):
    _USE_COLOR = False
elif os.environ.get("FORCE_COLOR"):
    _USE_COLOR = True
else:
    _USE_COLOR = sys.stdout.isatty()
_G = "\033[32m" if _USE_COLOR else ""   # green
_R = "\033[31m" if _USE_COLOR else ""   # red
_Y = "\033[33m" if _USE_COLOR else ""   # yellow
_C = "\033[36m" if _USE_COLOR else ""   # cyan
_B = "\033[1m"  if _USE_COLOR else ""   # bold
_X = "\033[0m"  if _USE_COLOR else ""   # reset

_HALLUCINATION_THRESHOLD = 0.5   # confidence above which an answer is flagged hallucinated
_MAX_RETRIES  = 3                # LLM metric call retries on transient errors
_RETRY_DELAY  = 5.0              # base back-off seconds between retries (doubles each attempt)
_RETRY_ERRORS = (
    "APIConnectionError", "ConnectionError", "RateLimitError",
    "Timeout", "TimeoutError", "ServiceUnavailableError",
)
_MAX_CHUNKS      = 50            # max context passages passed to the hallucination checker
_MAX_CHUNK_CHARS = 3000          # each passage is truncated to this many characters


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class EvalQuestion:
    question: str
    ground_truth: Optional[str]


@dataclass
class EvalResult:
    # ── written to CSV ────────────────────────────────────────────────────────
    index: int
    question: str
    ground_truth: Optional[str]              = None
    generated_answer: str                    = ""
    agent_mode: Optional[str]                = None
    search_type_used: Optional[str]          = None
    retrieved_context: Optional[str]         = None
    answer_correctness: Optional[float]      = None
    answer_correctness_reason: Optional[str] = None
    hallucination: Optional[str]             = None
    hallucination_confidence: Optional[float]= None
    hallucination_reason: Optional[str]      = None
    response_time_seconds: float             = 0.0
    # ── CLI-only (not written to CSV) ─────────────────────────────────────────
    answered_question: bool                  = field(default=False, repr=False)
    error: Optional[str]                     = field(default=None,  repr=False)
    metric_errors: dict                      = field(default_factory=dict, repr=False)


# ─── Question loader ──────────────────────────────────────────────────────────

def _detect_encoding(path: str) -> str:
    """Detect CSV encoding. Handles UTF-8 with BOM (Excel default), plain UTF-8,
    Shift-JIS, and other encodings via chardet fallback."""
    with open(path, "rb") as f:
        raw = f.read(4)
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"   # UTF-8 with BOM — Excel's default UTF-8 export
    try:
        import chardet
        with open(path, "rb") as f:
            detected = chardet.detect(f.read())
        enc = detected.get("encoding") or "utf-8"
        confidence = detected.get("confidence", 0)
        return enc if confidence >= 0.5 else "utf-8"
    except ImportError:
        return "utf-8"


def _read_single_column(path: str, column: str) -> List[str]:
    """Read one column from a single-column CSV.

    These CSVs have exactly one data column (question or ground_truth).
    csv.DictReader splits unquoted values that contain commas (e.g. Japanese
    numbers like 2,693) into multiple fields and corrupts the data.  We use
    csv.reader, which correctly handles quoted multi-line fields, then
    re-join any extra fields that were produced by unquoted commas so the
    full original value is always returned.
    """
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
        # If the row has more fields than the header, the value contained
        # unquoted commas and was split — re-join everything from col_idx
        # onward to restore the original string.
        value = ",".join(row[col_idx:]).strip()
        if value:
            results.append(value)
    return results


def load_questions(dataset_dir: str) -> List[EvalQuestion]:
    """Load questions.csv and (optionally) answers.csv from dataset_dir.

    questions.csv — required, single column: question
    answers.csv   — optional, single column: ground_truth
                    rows are zipped with questions by index
    """
    q_path = os.path.join(dataset_dir, "questions.csv")
    a_path = os.path.join(dataset_dir, "answers.csv")

    if not os.path.exists(q_path):
        sys.exit(f"ERROR: questions.csv not found at {q_path}")

    questions = _read_single_column(q_path, "question")

    ground_truths: List[Optional[str]] = [None] * len(questions)
    if os.path.exists(a_path):
        answers = _read_single_column(a_path, "ground_truth")
        if len(answers) != len(questions):
            sys.exit(
                f"ERROR: questions.csv has {len(questions)} rows but "
                f"answers.csv has {len(answers)} rows — they must match."
            )
        ground_truths = [a or None for a in answers]

    return [
        EvalQuestion(question=q, ground_truth=gt)
        for q, gt in zip(questions, ground_truths)
    ]


# ─── Answer Correctness (DeepEval GEval) ─────────────────────────────────────

_EVALUATION_STEPS = [
    "Check whether the facts in the actual output contradict any facts in the expected output.",
    "Lightly penalize omissions of detail — focus on whether the main idea is correct.",
    "Vague language or differing opinions (without factual contradiction) are acceptable.",
]

def _build_deepeval_llm():
    import re as _re
    from deepeval.models.base_model import DeepEvalBaseLLM
    from langchain_core.messages import HumanMessage
    from common.config import get_chat_config, get_llm_service

    cfg          = get_chat_config()
    llm_provider = get_llm_service(cfg)
    model_name   = cfg.get("llm_model", "unknown")
    lc_model     = getattr(llm_provider, "llm", None) or llm_provider

    def _parse(text, schema):
        try:
            m = _re.search(r"\{.*\}", text, _re.DOTALL)
            if m:
                return schema(**json.loads(m.group()))
        except Exception:
            pass
        return text

    class _Judge(DeepEvalBaseLLM):
        def __init__(self):
            super().__init__(model_name)

        def load_model(self):
            return lc_model

        def generate(self, prompt: str, schema=None):
            resp = lc_model.invoke([HumanMessage(content=prompt)])
            text = resp.content if hasattr(resp, "content") else str(resp)
            return _parse(text, schema) if schema else text

        async def a_generate(self, prompt: str, schema=None):
            resp = await lc_model.ainvoke([HumanMessage(content=prompt)])
            text = resp.content if hasattr(resp, "content") else str(resp)
            return _parse(text, schema) if schema else text

        def get_model_name(self) -> str:
            return model_name

    return _Judge()


def answer_correctness(
    question: str,
    generated_answer: str,
    ground_truth: str,
) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Returns (score 0–1, reason, error). score/reason are None on failure."""
    if not ground_truth:
        return None, None, "no ground truth provided"

    try:
        import asyncio
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams
    except ImportError as e:
        return None, None, f"deepeval not installed: {e}"

    try:
        judge = _build_deepeval_llm()
    except Exception as e:
        return None, None, f"judge init failed: {e}"

    metric = GEval(
        name="Answer Correctness",
        model=judge,
        evaluation_steps=_EVALUATION_STEPS,
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.EXPECTED_OUTPUT,
        ],
    )
    test_case = LLMTestCase(
        input=question,
        actual_output=generated_answer,
        expected_output=ground_truth,
    )

    last_err: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            # _show_indicator=False suppresses DeepEval's "✨ You're running…" line
            try:
                asyncio.run(metric.a_measure(test_case, _show_indicator=False))
            except TypeError:
                asyncio.run(metric.a_measure(test_case))
            last_err = None
            break
        except Exception as e:
            last_err = e
            err_type = type(e).__name__
            retryable = any(r in err_type or r in str(e) for r in _RETRY_ERRORS)
            if retryable and attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * (2 ** (attempt - 1)))
            else:
                break

    if last_err is not None:
        return None, None, f"{type(last_err).__name__}: {last_err}"

    return (
        float(metric.score)  if metric.score  is not None else None,
        str(metric.reason).strip() if metric.reason else None,
        None,
    )


# ─── Hallucination check ──────────────────────────────────────────────────────

def hallucination_check(
    question: str,
    generation: str,
    context: str,
) -> Tuple[Optional[str], Optional[float], Optional[str], Optional[str]]:
    """Returns (verdict, confidence, reason, error).
    verdict is 'yes' (hallucinated) or 'no' (grounded).
    """
    if not context:
        return None, None, None, "no retrieved context to check against"

    try:
        from common.config import get_chat_config, get_llm_service
        # Regression-only graded grader (0-1 confidence), kept in this folder so
        # it stays independent of the production binary hallucination check.
        from hallucination_check import (
            TigerGraphAgentHallucinationCheck,
        )

        cfg    = get_chat_config()
        llm    = get_llm_service(cfg)
        result = TigerGraphAgentHallucinationCheck(llm).check_hallucination(
            generation=generation,
            context=context,
            question=question,
        )
        confidence = max(0.0, min(1.0, float(result.confidence)))
        verdict    = "yes" if confidence >= _HALLUCINATION_THRESHOLD else "no"
        return verdict, confidence, str(result.reason).strip() if result.reason else None, None
    except Exception as e:
        return None, None, None, f"{type(e).__name__}: {e}"


# ─── Context extraction ───────────────────────────────────────────────────────

def _flatten_context(query_sources: dict) -> List[str]:
    """Extract text chunks from GraphRAG query_sources response."""
    if not query_sources:
        return []

    try:
        final_retrieval = query_sources["result"]["final_retrieval"]
        if isinstance(final_retrieval, dict):
            raw: List[str] = []
            for chunks in final_retrieval.values():
                if isinstance(chunks, list):
                    raw.extend(str(c) for c in chunks if c)
                elif isinstance(chunks, str) and chunks.strip():
                    raw.append(chunks)
            if raw:
                seen: set = set()
                out: List[str] = []
                for c in raw:
                    c = c.strip()
                    if len(c) < 5 or c in seen:
                        continue
                    seen.add(c)
                    out.append(c[:_MAX_CHUNK_CHARS])
                    if len(out) >= _MAX_CHUNKS:
                        break
                return out
    except (KeyError, TypeError):
        pass

    def _walk(node):
        result: List[str] = []
        if isinstance(node, str):
            if node.strip():
                result.append(node)
        elif isinstance(node, list):
            for item in node:
                result.extend(_walk(item))
        elif isinstance(node, dict):
            for key in ("final_retrieval", "context", "contexts", "chunks",
                        "documents", "passages", "text", "result"):
                if key in node:
                    result.extend(_walk(node[key]))
            if not result:
                for v in node.values():
                    result.extend(_walk(v))
        return result

    chunks = _walk(query_sources)
    seen2: set = set()
    out2: List[str] = []
    for c in chunks:
        c = c.strip()
        if len(c) < 5 or c in seen2:
            continue
        seen2.add(c)
        out2.append(c[:_MAX_CHUNK_CHARS])
        if len(out2) >= _MAX_CHUNKS:
            break
    return out2


# ─── Core eval loop ───────────────────────────────────────────────────────────

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
            "q":             question,
            "mode":          mode,
            "rag_pattern":   rag_pattern,
            "include_fields": "query_sources",
        },
        auth=(username, password),
        timeout=600.0,  # agentic planned mode can take >120s on complex multi-hop questions
    )
    resp.raise_for_status()
    data = resp.json()
    # The /ui/query endpoint returns model_dump_json() (a string), which FastAPI
    # JSON-encodes again, so resp.json() gives a str. Parse it a second time.
    if isinstance(data, str):
        import json as _json
        data = _json.loads(data)
    return data


def _eval_one(
    idx: int,
    q: EvalQuestion,
    graphname: str,
    url: str,
    username: str,
    password: str,
    mode: str,
    rag_pattern: str,
) -> EvalResult:
    r = EvalResult(index=idx, question=q.question, ground_truth=q.ground_truth)
    t0 = time.monotonic()

    try:
        data = _query_graphrag(url, graphname, username, password, q.question, mode, rag_pattern)
        # Release 2.0.0: endpoint returns a Message object; answer is in "content"
        r.generated_answer  = data.get("content") or ""
        r.answered_question = bool(data.get("answered_question", False))
        query_sources       = data.get("query_sources") or {}
        # Map the internal chosen_retriever value to the same human-readable label
        # the UI displays (matches CustomChatMessage.tsx). Falls back to response_type.
        _RETRIEVER_LABELS = {
            "similaritysearch": "Similarity Search",
            "contextualsearch": "Contextual Search",
            "hybridsearch":     "Hybrid Search",
            "communitysearch":  "Community Search",
        }
        _chosen = (query_sources.get("chosen_retriever") or "").lower().replace(" ", "")
        r.search_type_used = _RETRIEVER_LABELS.get(_chosen) or data.get("response_type") or None
        # Record which agent was used for this run
        r.agent_mode = f"{mode}/{rag_pattern}"
        contexts            = _flatten_context(query_sources)
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

    if not r.answered_question or not r.generated_answer:
        r.metric_errors["all"] = "GraphRAG did not produce an answer"
        return r

    ctx_str = "\n\n".join(f"[Passage {i + 1}]\n{c}" for i, c in enumerate(contexts))
    r.retrieved_context = ctx_str

    tasks = {}
    if q.ground_truth:
        _q, _ans, _gt = q.question, r.generated_answer, q.ground_truth
        tasks["correctness"] = lambda _q=_q, _ans=_ans, _gt=_gt: answer_correctness(_q, _ans, _gt)

    _q, _gen, _ctx = q.question, r.generated_answer, ctx_str
    tasks["hallucination"] = lambda _q=_q, _gen=_gen, _ctx=_ctx: hallucination_check(_q, _gen, _ctx)

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {m: pool.submit(fn) for m, fn in tasks.items()}
        for metric, future in futures.items():
            try:
                val = future.result()
                if metric == "correctness":
                    r.answer_correctness, r.answer_correctness_reason, err = val
                    if err:
                        r.metric_errors["answer_correctness"] = err
                elif metric == "hallucination":
                    r.hallucination, r.hallucination_confidence, r.hallucination_reason, err = val
                    if err:
                        r.metric_errors["hallucination"] = err
                    if r.hallucination_confidence is not None:
                        r.hallucination_confidence = round(r.hallucination_confidence, 3)
            except Exception as e:
                r.metric_errors[metric] = f"unexpected: {type(e).__name__}: {e}"

    return r


def _ac_cell(r: EvalResult) -> str:
    ac = r.answer_correctness
    if ac is not None:
        col = _G if ac >= 0.75 else _Y if ac >= 0.5 else _R
        return f"{col}{ac * 100:5.1f}%{_X}"
    if r.metric_errors.get("answer_correctness"):
        return f"{_Y}  n/a{_X}"
    return "    —"


def _hal_cell(r: EvalResult) -> str:
    hal = r.hallucination
    if hal is not None:
        col     = _R if hal == "yes" else _G
        verdict = "Halluc." if hal == "yes" else "Ground."
        conf    = f" {r.hallucination_confidence * 100:3.0f}%" if r.hallucination_confidence is not None else ""
        return f"{col}{verdict}{conf}{_X}"
    if r.metric_errors.get("hallucination"):
        return f"{_Y}n/a{_X}"
    return "—"


def _print_compact(r: EvalResult, total: int) -> None:
    """One clean colourised line per question (default mode)."""
    n = r.index + 1
    if r.error:
        print(f"  Q{n:>3}/{total}  [{r.response_time_seconds:5.1f}s]  "
              f"{_R}error: {r.error[:100]}{_X}", flush=True)
        return
    if not r.answered_question:
        print(f"  Q{n:>3}/{total}  [{r.response_time_seconds:5.1f}s]  "
              f"{_Y}no answer from GraphRAG{_X}", flush=True)
        return
    extra = ""
    if r.metric_errors:
        errs = ", ".join(f"{k}: {v[:60]}" for k, v in r.metric_errors.items())
        extra = f"  {_Y}metric warn: {errs}{_X}"
    print(f"  Q{n:>3}/{total}  [{r.response_time_seconds:5.1f}s]  "
          f"AC={_ac_cell(r)}  Hal={_hal_cell(r)}{extra}", flush=True)


def _print_detailed(r: EvalResult, total: int) -> None:
    """Question, answer, scores and reasons (--detailed mode)."""
    n = r.index + 1
    q_short = (r.question[:70] + "…") if len(r.question) > 71 else r.question
    print(f"  {'─' * 70}", flush=True)
    print(f"  {_B}Q{n}/{total}{_X}  [{r.response_time_seconds:.1f}s]  {q_short}", flush=True)
    if r.error:
        print(f"  {_R}error: {r.error}{_X}", flush=True)
        return
    if not r.answered_question:
        print(f"  {_Y}GraphRAG did not produce an answer{_X}", flush=True)
        return
    print(f"  Answer : {(r.generated_answer or '')[:160]}", flush=True)

    ac_line = f"  {_C}Answer Correctness{_X} : AC={_ac_cell(r)}"
    if r.metric_errors.get("answer_correctness"):
        ac_line += f"  {_Y}({r.metric_errors['answer_correctness']}){_X}"
    print(ac_line, flush=True)
    if r.answer_correctness_reason:
        print(f"     reason: {r.answer_correctness_reason}", flush=True)

    hal_line = f"  {_C}Hallucination{_X}      : Hal={_hal_cell(r)}"
    if r.metric_errors.get("hallucination"):
        hal_line += f"  {_Y}({r.metric_errors['hallucination']}){_X}"
    print(hal_line, flush=True)
    if r.hallucination_reason:
        print(f"     reason: {r.hallucination_reason}", flush=True)


def run_eval(
    questions: List[EvalQuestion],
    graphname: str,
    url: str,
    username: str,
    password: str,
    mode: str = "agentic",
    rag_pattern: str = "planned",
    detailed: bool = False,
) -> List[EvalResult]:
    total    = len(questions)
    workers  = min(8, total)
    results: List[Optional[EvalResult]] = [None] * total
    printer  = _print_detailed if detailed else _print_compact

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval") as pool:
        futures = {
            pool.submit(_eval_one, i, q, graphname, url, username, password, mode, rag_pattern): i
            for i, q in enumerate(questions)
        }
        for future in as_completed(futures):
            r = future.result()
            results[r.index] = r
            printer(r, total)

    return [r for r in results if r is not None]


# ─── Output ───────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "question", "ground_truth", "generated_answer", "retrieved_context",
    "agent_mode", "search_type_used",
    "answer_correctness", "answer_correctness_reason",
    "hallucination", "hallucination_reason",
    "response_time_seconds",
]


def write_results(results: List[EvalResult], output_dir: str, detailed: bool = False) -> None:
    os.makedirs(output_dir, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = os.path.join(output_dir, f"results_{ts}.csv")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for r in results:
            row = asdict(r)
            writer.writerow({k: row[k] for k in _CSV_FIELDS})

    # ── Summary ───────────────────────────────────────────────────────────────
    errors    = [r for r in results if r.error]
    unanswered = [r for r in results if not r.error and not r.answered_question]
    answered  = len(results) - len(errors) - len(unanswered)
    valid_hal = [r for r in results if r.hallucination is not None]
    hal_rate  = sum(1 for r in valid_hal if r.hallucination == "yes") / len(valid_hal) if valid_hal else None
    corr_vals = [r.answer_correctness for r in results if r.answer_correctness is not None]
    avg_corr  = sum(corr_vals) / len(corr_vals) if corr_vals else None

    agent_label = results[0].agent_mode if results else "unknown"
    print(f"\n  {'═' * 60}")
    print(f"  {_B}Summary{_X}  [{_C}{agent_label}{_X}]")
    print(f"  {'═' * 60}")
    print(f"  Answered                : {_G}{answered}{_X} / {len(results)}")
    if unanswered:
        print(f"  No answer               : {_Y}{len(unanswered)}{_X}"
              f"  (Q{', Q'.join(str(r.index+1) for r in unanswered)})")
    if errors:
        print(f"  Errors                  : {_R}{len(errors)}{_X}"
              f"  (Q{', Q'.join(str(r.index+1) for r in errors)})"  )
        for r in errors:
            print(f"    Q{r.index+1}: {r.error}", flush=True)

    if avg_corr is not None:
        col = _G if avg_corr >= 0.75 else _Y if avg_corr >= 0.5 else _R
        print(f"  {_C}Avg Answer Correctness{_X}  : {col}{avg_corr * 100:.1f}%{_X}")
    elif not any(r.ground_truth for r in results):
        print(f"  Avg Answer Correctness  : — (no ground truth / answers.csv)")
    else:
        print(f"  Avg Answer Correctness  : — (metric errors, check deepeval install)")

    if hal_rate is not None:
        col = _R if hal_rate > 0.2 else _G
        print(f"  {_C}Hallucination Rate{_X}      : {col}{hal_rate * 100:.1f}%{_X}")
    else:
        print(f"  Hallucination Rate      : — (check failed or no context)")

    print(f"  {'═' * 60}")
    print(f"\n  CSV : {csv_path}\n")


# ─── CLI entry point ──────────────────────────────────────────────────────────



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GraphRAG Regression Evaluator")
    parser.add_argument("--dataset",     required=True,
                        help="Dataset name (folder under graphrag/tests/test_questions/)")
    parser.add_argument("--graphname",   required=True,
                        help="GraphRAG graph name to query")
    parser.add_argument("--config",      default="configs/server_config.json",
                        help="Path to server_config.json")
    parser.add_argument("--url",         default=None,
                        help="GraphRAG base URL (overrides server_config.json graphrag_config.query_url)")
    parser.add_argument("--agent", default="planned",
                        choices=["planned", "reactive", "classic"],
                        help=(
                            "Agent to use: 'planned' (Planned Agent, default), "
                            "'reactive' (ReAct Agent), or 'classic' (Classic engine). "
                            "Use --search-type to override the Classic retriever."
                        ))
    parser.add_argument("--search-type", default="auto",
                        help=(
                            "Classic retriever override when --agent classic is set. "
                            "Values: auto, similaritysearch, contextualsearch, "
                            "hybridsearch, communitysearch (default: auto)"
                        ))
    parser.add_argument("--output",      default=None,
                        help="Output directory for result CSV/JSON")
    parser.add_argument("--detailed",    action="store_true",
                        help="Print per-question breakdown in addition to the summary")
    parser.add_argument("--limit",       type=int, default=None,
                        help="Only run the first N questions (useful for quick smoke tests)")
    args = parser.parse_args()

    # ── Silence the noise: deprecation warnings + the app's INFO logging ───────
    # The GraphRAG app modules (openai_service, agent_hallucination_check, …)
    # log at INFO on every call. logging.disable(INFO) hard-suppresses INFO/DEBUG
    # from every logger regardless of its own level, so the only thing on screen
    # is our clean per-question output.
    warnings.simplefilter("ignore")
    logging.disable(logging.INFO)

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

    # Map --agent choice to (mode, rag_pattern) for the /query endpoint
    _AGENT_MAP = {
        "planned":  ("agentic", "planned"),
        "reactive": ("agentic", "reactive"),
        "classic":  ("classic", args.search_type.lower()),
    }
    mode, rag_pattern = _AGENT_MAP[args.agent]

    questions = load_questions(dataset_dir)
    if args.limit:
        questions = questions[:args.limit]

    print(f"\n{_B}GraphRAG Evaluation{_X}  dataset={args.dataset}  "
          f"graph={args.graphname}  agent={args.agent}  "
          + (f"retriever={args.search_type}  " if args.agent == "classic" else "")
          + f"questions={len(questions)}"
          + (f"  {_Y}(limit={args.limit}){_X}" if args.limit else "")
          + "\n", flush=True)

    results = run_eval(
        questions   = questions,
        graphname   = args.graphname,
        url         = graphrag_url,
        username    = username,
        password    = password,
        mode        = mode,
        rag_pattern = rag_pattern,
        detailed    = args.detailed,
    )
    write_results(results, output_dir, detailed=args.detailed)

    # os._exit skips the interpreter shutdown phase, which otherwise emits
    # destructor/asyncio noise ("ImportError: sys.meta_path is None …") from
    # the GraphRAG embedding-store and LLM clients after our output is done.
    sys.stdout.flush()
    sys.stderr.flush()
    exit_code = 1 if any(
        r.error or not r.answered_question or r.metric_errors
        for r in results
    ) else 0
    os._exit(exit_code)
