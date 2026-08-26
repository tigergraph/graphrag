# Multihop30 Dataset

30 multi-hop question-answer pairs for evaluating GraphRAG retrieval and answer quality.

## Dataset Composition

| Source | Questions | Type | Hops |
|--------|-----------|------|------|
| HotpotQA | Q1–Q10 | Bridge + Comparison | 2 |
| 2WikiMultiHopQA | Q11–Q20 | Bridge + Comparison | 2 |
| MuSiQue | Q21–Q30 | Bridge | 2–3 |

All 30 questions require reasoning across multiple passages. This makes retrieval quality
critical — the system must surface the right supporting chunks to answer correctly.

## Files

| File | Description |
|------|-------------|
| `questions.csv` | Single column `question` — 30 questions |
| `answers.csv` | Single column `ground_truth` — 30 reference answers |
| `ground_truth_chunks.csv` | Columns `question_index`, `chunk_index`, `context` — supporting chunks per question (2–4 rows per question). Used as the denominator in Recall@5. |
| `data/hotpotqa_corpus.txt` | Raw text corpus for Q1–Q10 |
| `data/2wikimultihopqa_corpus.txt` | Raw text corpus for Q11–Q20 |
| `data/musique_corpus.txt` | Raw text corpus for Q21–Q30 |

## Running Evaluations

### Step 1 — One-time graph setup (ingest corpus + build knowledge graph)

```bash
./graphrag/tests/regression/run_setup.sh --dataset Multihop30
# Note the graphname printed at the end, e.g. multihop30_abc12345
```

### Step 2 — Recall@5 (retrieval quality)

```bash
./graphrag/tests/regression/run_recall.sh \
  --dataset Multihop30 --graphname <graphname> --mode auto --match embedding
```

### Step 3 — Answer Correctness + Hallucination (answer quality)

```bash
./graphrag/tests/regression/run_eval.sh \
  --dataset Multihop30 --graphname <graphname> --mode auto
```

### Smoke test (5 questions)

```bash
./graphrag/tests/regression/run_recall.sh \
  --dataset Multihop30 --graphname <graphname> --mode auto --match embedding --limit 5

./graphrag/tests/regression/run_eval.sh \
  --dataset Multihop30 --graphname <graphname> --mode auto --limit 5
```

Results are written to `graphrag/tests/regression/results/` as a timestamped CSV.
