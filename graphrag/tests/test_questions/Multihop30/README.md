# Multihop30 Dataset

30 multi-hop question-answer pairs for evaluating GraphRAG retrieval quality (Recall@5).

## Dataset Composition

| Source | Questions | Type | Hops |
|--------|-----------|------|------|
| HotpotQA | Q1–Q10 | Bridge + Comparison | 2 |
| 2WikiMultiHopQA | Q11–Q20 | Bridge + Comparison | 2 |
| MuSiQue | Q21–Q30 | Bridge | 2–3 |

All 30 questions require reasoning across multiple passages to arrive at the
correct answer. This makes retrieval quality critical — the model must surface
the right supporting passages to answer correctly.

## Files

| File | Description |
|------|-------------|
| `questions.csv` | Single column `question` — 30 questions |
| `answers.csv` | Single column `ground_truth` — 30 reference answers |
| `ground_truth_contexts.csv` | Columns `question_index`, `context` — supporting passages per question (2–3 rows per question) |
| `data/hotpotqa_corpus.txt` | Corpus for Q1–Q10 (supporting + distractor passages) |
| `data/2wikimultihopqa_corpus.txt` | Corpus for Q11–Q20 |
| `data/musique_corpus.txt` | Corpus for Q21–Q30 |
| `build_dataset.py` | Script to regenerate CSVs from HuggingFace datasets |
| `build_corpus.py` | Script to regenerate corpus files from build_dataset.py output |

## Recall@5 Metric

`ground_truth_contexts.csv` is used exclusively by `recall_evaluator.py`.

For each question, the evaluator checks how many of its ground-truth supporting
passages appear in the **top-5 chunks retrieved** by GraphRAG, using token
overlap matching (ROUGE-1 style, threshold ≥ 0.5).

```
Recall@5 = matched_ground_truth_passages / total_ground_truth_passages
```

Averaged over all 30 questions to produce the final **Avg Recall@5** score.

## Running Evaluations

### One-time graph setup (ingest corpus + build knowledge graph)
```bash
./graphrag/tests/regression/run_setup.sh --dataset Multihop30
# Note the graphname printed at the end, e.g. multihop30_abc12345
```

### Recall@5 evaluation
```bash
./graphrag/tests/regression/run_recall.sh \
  --dataset Multihop30 \
  --graphname multihop30_abc12345
```

### Answer Correctness + Hallucination (optional, uses answers.csv)
```bash
./graphrag/tests/regression/run_eval.sh \
  --dataset Multihop30 \
  --graphname multihop30_abc12345
```

## Regenerating the Dataset from HuggingFace

The pre-built CSV files are committed to the repository. To regenerate from
the original sources (useful for updating to a newer dataset version):

```bash
pip install datasets
python graphrag/tests/test_questions/Multihop30/build_dataset.py
python graphrag/tests/test_questions/Multihop30/build_corpus.py
```

`build_dataset.py` samples from HuggingFace and writes:
- `questions.csv`, `answers.csv`, `ground_truth_contexts.csv`
- `_passages_by_source.json` (intermediate file consumed by `build_corpus.py`)

`build_corpus.py` reads `_passages_by_source.json` and writes `data/*.txt`.
