# Copyright (c) 2026 TigerGraph, Inc.
#
# This program may be redistributed and/or modified under the terms of the GNU
# Affero General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""In-memory BM25 keyword index over document chunks (GML-2367).

Keyword search is enabled per graph with the ``keyword_search`` setting. The
index is built from the text of the graph's chunks (their Content vertices);
nothing is written to the graph. One index is kept in memory per graph and
saved with the graph's data (``configs/graph_data/<graph>/``), so a
restart loads it instead of building it again.

An index is built by ``build_keyword_index`` or on first use, and checked
again at most every ``_RECHECK_SECONDS``: when the graph's chunk count
changed, it is built again. A saved index is used only when its tokenizer
version and chunk count match. ``invalidate_keyword_index`` drops the index
and its file after a rebuild.
"""

from __future__ import annotations

import gzip
import heapq
import json
import logging
import math
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from common.config import get_graphrag_config
from common.utils.chunk_context import HEADING_PATH_ATTR
from common.utils.keyword_tokens import TOKENIZER_VERSION, tokenize

logger = logging.getLogger(__name__)

_RECHECK_SECONDS = 60
_INDEX_FILE = "keyword_index.json.gz"
# What a saved index holds besides the tokenizer; change it when _load
# indexes other text, so saved indexes are built again.
_INDEX_CONTENT = "path+text"
K1 = 1.2
B = 0.75


@dataclass
class BM25Index:
    ids: list[str] = field(default_factory=list)
    lengths: list[int] = field(default_factory=list)
    postings: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    avg_length: float = 0.0

    @classmethod
    def build(cls, docs: list[tuple[str, list[str]]]) -> "BM25Index":
        index = cls()
        for chunk_id, tokens in docs:
            pos = len(index.ids)
            index.ids.append(chunk_id)
            index.lengths.append(len(tokens))
            for term, tf in Counter(tokens).items():
                index.postings.setdefault(term, []).append((pos, tf))
        index.avg_length = (sum(index.lengths) / len(index.lengths)) if index.lengths else 0.0
        return index

    def __len__(self) -> int:
        return len(self.ids)

    def search(self, tokens: list[str], top_k: int) -> list[tuple[str, float]]:
        """``(chunk_id, score)`` of the ``top_k`` best chunks for ``tokens``."""
        n = len(self.ids)
        if not n or top_k <= 0:
            return []
        scores: dict[int, float] = {}
        for term in dict.fromkeys(tokens):
            posting = self.postings.get(term)
            if not posting:
                continue
            df = len(posting)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for pos, tf in posting:
                norm = K1 * (1 - B + B * self.lengths[pos] / self.avg_length)
                scores[pos] = scores.get(pos, 0.0) + idf * tf * (K1 + 1) / (tf + norm)
        best = heapq.nlargest(top_k, scores.items(), key=lambda kv: kv[1])
        return [(self.ids[pos], score) for pos, score in best]

    def _idf(self, term: str) -> float:
        n = len(self.ids)
        df = len(self.postings.get(term) or ())
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def match(self, tokens: list[str], top_k: int) -> KeywordMatch:
        """``search`` with the term coverage of its results."""
        chunks = self.search(tokens, top_k)
        terms = list(dict.fromkeys(tokens))
        out = KeywordMatch(chunks=chunks, query_terms=len(terms))
        if not terms or not chunks:
            out.missing_terms = terms
            return out
        weight = {t: self._idf(t) for t in terms}
        total = sum(weight.values()) or 1.0
        positions = {cid: pos for pos, cid in enumerate(self.ids)}
        holders = {t: {p for p, _ in self.postings.get(t) or ()} for t in terms}
        found = {cid: {t for t in terms if positions[cid] in holders[t]} for cid, _ in chunks}
        best = found[chunks[0][0]]
        union = set().union(*found.values())
        out.coverage = sum(weight[t] for t in best) / total
        out.union_coverage = sum(weight[t] for t in union) / total
        out.top_score_ratio = chunks[0][1] / (total * (K1 + 1))
        out.missing_terms = sorted((t for t in terms if t not in union),
                                   key=lambda t: -weight[t])
        return out


@dataclass
class KeywordMatch:
    """Keyword search results with how well they match the query.

    BM25 scores have no fixed scale: they grow with the number and rarity of
    the query words. The match is judged by term coverage instead: the share
    of the query's weight (the IDF of each distinct query word) found in the
    results. A query word that no chunk contains has the highest weight.
    """
    chunks: list[tuple[str, float]] = field(default_factory=list)
    # Weighted share of the query words in the best chunk, and in all results.
    coverage: float = 0.0
    union_coverage: float = 0.0
    # Best score divided by the largest score the query could get.
    top_score_ratio: float = 0.0
    # Query words that no returned chunk contains, most distinctive first.
    missing_terms: list[str] = field(default_factory=list)
    query_terms: int = 0

    @property
    def strength(self) -> str:
        return match_strength(self)


# Coverage cutoffs for the match strength, set from labelled eval questions:
# at STRONG_COVERAGE or more, the passages held every fact of the reference
# answer for most questions; below WEAK_COVERAGE, for few.
STRONG_COVERAGE = 0.45
WEAK_COVERAGE = 0.3


def match_strength(match: "KeywordMatch") -> str:
    """``strong``, ``partial``, ``weak`` or ``none``. Strong means the best
    passage holds nearly all distinctive words of the query; it does not mean
    the passage answers the question."""
    if not match.chunks or not match.query_terms:
        return "none"
    if match.coverage >= STRONG_COVERAGE:
        return "strong"
    if match.coverage >= WEAK_COVERAGE:
        return "partial"
    return "weak"


@dataclass
class _Entry:
    index: BM25Index
    chunk_count: int
    checked_at: float
    built_at: float
    build_seconds: float


_indexes: dict[str, _Entry] = {}
_lock = threading.Lock()


def _index_path(graphname: str) -> str:
    return os.path.join("configs", "graph_data", graphname, _INDEX_FILE)


def _save(graphname: str, entry: _Entry) -> None:
    path = _index_path(graphname)
    data = {
        "tokenizer": TOKENIZER_VERSION,
        "content": _INDEX_CONTENT,
        "chunk_count": entry.chunk_count,
        "built_at": entry.built_at,
        "build_seconds": entry.build_seconds,
        "ids": entry.index.ids,
        "lengths": entry.index.lengths,
        "postings": entry.index.postings,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"keyword index for {graphname} not saved: {e}")


def _load_saved(graphname: str, chunk_count: int, now: float) -> _Entry | None:
    """The saved index, if it matches the tokenizer and the chunk count."""
    path = _index_path(graphname)
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning(f"saved keyword index for {graphname} unreadable: {e}")
        return None
    if (data.get("tokenizer") != TOKENIZER_VERSION or data.get("content") != _INDEX_CONTENT
            or data.get("chunk_count") != chunk_count):
        return None
    ids, lengths = data["ids"], data["lengths"]
    index = BM25Index(
        ids=ids,
        lengths=lengths,
        postings={t: [tuple(p) for p in ps] for t, ps in data["postings"].items()},
        avg_length=(sum(lengths) / len(lengths)) if lengths else 0.0,
    )
    logger.info(f"keyword index for {graphname}: loaded {len(index)} chunks from {path}")
    return _Entry(index, chunk_count, now, data.get("built_at", now), data.get("build_seconds", 0.0))


def _remove_saved(graphname: str) -> None:
    try:
        os.remove(_index_path(graphname))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"saved keyword index for {graphname} not removed: {e}")


def keyword_search_enabled(graphname: str) -> bool:
    return bool(get_graphrag_config(graphname).get("keyword_search", False))


def _chunk_paths(conn) -> dict[str, str]:
    """``{chunk id: section path}``. A graph whose chunks have no
    ``heading_path`` attribute keeps the path at the start of the chunk text,
    so its paths are empty here."""
    try:
        rows = conn.getVertices("DocumentChunk", select=HEADING_PATH_ATTR)
    except Exception:
        rows = conn.getVertices("DocumentChunk", select="idx")
    return {r["v_id"]: (r.get("attributes") or {}).get(HEADING_PATH_ATTR) or "" for r in rows}


def _load(conn) -> BM25Index:
    # Each chunk is indexed as its section path and its text: the path holds
    # the document's own headings, which keyword search should find. The
    # model-written summary is left out, so a match is always on words the
    # document contains. A chunk's Content vertex has the chunk's id; other
    # Content vertices hold whole documents.
    # Text is read in batches that fit the database's response size limit;
    # one response with every chunk's text passes it on a large graph.
    paths = _chunk_paths(conn)
    ids = list(paths)
    texts: dict[str, str] = {}
    for i in range(0, len(ids), _LOAD_BATCH):
        texts.update(_texts_within_limit(conn, ids[i:i + _LOAD_BATCH]))
    docs = [(c, tokenize(paths[c]) + tokenize(texts.get(c, ""))) for c in ids if c in texts]
    return BM25Index.build(docs)


# 200 chunks at the default chunk size stay well under TigerGraph's default
# 4 MB response limit. Chunkers can exceed the configured size, so a batch the
# database rejects as too large is split again.
_LOAD_BATCH = 200


def _too_large(e: Exception) -> bool:
    msg = str(e).lower()
    return "response size" in msg and "exceeds limit" in msg


def _texts_within_limit(conn, chunk_ids: list[str]) -> dict[str, str]:
    """Text of each chunk; a batch the database rejects as too large is read
    in two halves."""
    if not chunk_ids:
        return {}
    try:
        return _query_texts(conn, chunk_ids)
    except Exception as e:
        if len(chunk_ids) > 1 and _too_large(e):
            half = len(chunk_ids) // 2
            return {**_texts_within_limit(conn, chunk_ids[:half]),
                    **_texts_within_limit(conn, chunk_ids[half:])}
        logger.info(f"keyword index: batch content fetch failed ({e}); reading chunks one by one")
    return _chunk_texts_one_by_one(conn, chunk_ids)


def _build(conn, chunk_count: int, now: float) -> _Entry:
    started = time.perf_counter()
    index = _load(conn)
    seconds = time.perf_counter() - started
    logger.info(
        f"keyword index for {conn.graphname}: {len(index)} chunks, "
        f"{len(index.postings)} terms in {seconds:.2f}s"
    )
    entry = _Entry(index, chunk_count, now, now, seconds)
    _save(conn.graphname, entry)
    return entry


def build_keyword_index(conn) -> _Entry:
    """Build the graph's index now, replacing any cached one."""
    now = time.time()
    with _lock:
        entry = _build(conn, int(conn.getVertexCount("DocumentChunk")), now)
        _indexes[conn.graphname] = entry
    return entry


def get_keyword_index(conn) -> BM25Index | None:
    """The graph's keyword index, or ``None`` when keyword search is off for
    the graph, it has no chunks, or it cannot be read."""
    graph = conn.graphname
    if not keyword_search_enabled(graph):
        return None
    now = time.time()
    entry = _indexes.get(graph)
    if entry and now - entry.checked_at < _RECHECK_SECONDS:
        return entry.index if len(entry.index) else None
    with _lock:
        entry = _indexes.get(graph)
        if entry and now - entry.checked_at < _RECHECK_SECONDS:
            return entry.index if len(entry.index) else None
        try:
            chunk_count = int(conn.getVertexCount("DocumentChunk"))
            if entry is None:
                entry = _load_saved(graph, chunk_count, now)
            if entry is None or entry.chunk_count != chunk_count:
                entry = _build(conn, chunk_count, now)
            else:
                entry.checked_at = now
        except Exception as e:
            logger.warning(f"keyword index unavailable for {graph}: {e}")
            entry = _Entry(BM25Index(), -1, now, 0.0, 0.0)
        _indexes[graph] = entry
    return entry.index if len(entry.index) else None


def invalidate_keyword_index(graphname: str) -> None:
    """Drop the graph's index and its saved file; the next use builds it."""
    with _lock:
        _indexes.pop(graphname, None)
        _remove_saved(graphname)


def keyword_index_status(graphname: str) -> dict:
    """What the setup page shows for the graph's keyword index."""
    entry = _indexes.get(graphname)
    built = entry is not None and entry.built_at > 0
    return {
        "enabled": keyword_search_enabled(graphname),
        "built": built,
        "chunks": len(entry.index) if built else 0,
        "terms": len(entry.index.postings) if built else 0,
        "built_at": entry.built_at if built else None,
        "build_seconds": round(entry.build_seconds, 2) if built else None,
    }


def keyword_search(conn, question: str, top_k: int) -> list[tuple[str, float]]:
    """``(chunk_id, score)`` of the chunks that best match the words of
    ``question``; empty when keyword search is off for the graph."""
    index = get_keyword_index(conn)
    if index is None:
        return []
    return index.search(tokenize(question), top_k)


def keyword_match(conn, question: str, top_k: int) -> KeywordMatch | None:
    """``keyword_search`` with how well the results match ``question``;
    ``None`` when keyword search is off for the graph."""
    index = get_keyword_index(conn)
    if index is None:
        return None
    return index.match(tokenize(question), top_k)


def chunk_texts(conn, chunk_ids: list[str]) -> dict[str, str]:
    """Text of each chunk, read in one request."""
    if not chunk_ids:
        return {}
    try:
        return _query_texts(conn, chunk_ids)
    except Exception as e:
        # The query fails as a whole when one chunk no longer exists, for
        # example after a delete that the index has not seen yet.
        logger.info(f"keyword search: batch content fetch failed ({e}); reading chunks one by one")
    return _chunk_texts_one_by_one(conn, chunk_ids)


def _query_texts(conn, chunk_ids: list[str]) -> dict[str, str]:
    res = conn.runInstalledQuery(
        "Selected_Set_Display",
        params={"json_list_vts": json.dumps([{"v": c, "t": "DocumentChunk"} for c in chunk_ids])},
        usePost=True,
    )
    out = {}
    for row in res[0].get("results", []):
        text = "\n".join(row.get("attributes", {}).get("context") or [])
        if text:
            out[row["v_id"]] = text
    return out


def _chunk_texts_one_by_one(conn, chunk_ids: list[str]) -> dict[str, str]:
    out = {}
    for chunk_id in chunk_ids:
        try:
            rows = conn.getVerticesById("Content", chunk_id)
        except Exception as e:
            logger.warning(f"keyword search: content fetch failed for {chunk_id}: {e}")
            continue
        text = rows[0].get("attributes", {}).get("text", "") if rows else ""
        if text:
            out[chunk_id] = text
    return out
