# Copyright (c) 2024-2026 TigerGraph, Inc.
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

"""Counts and sizes of what a retrieval query returned in ``final_retrieval``."""

import re
from dataclasses import dataclass

# DocumentChunk ids are "<document id>_chunk_<n>"; every other entry (entity
# text, community summaries, whole documents, the community
# ``Similarity_Context``) is counted separately.
_CHUNK_ID = re.compile(r"_chunk_\d+$")


@dataclass
class RetrievalStats:
    chunks: int = 0
    chunk_chars: int = 0
    others: int = 0
    other_chars: int = 0


def _chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return sum(_chars(v) for v in value)
    return len(str(value))


def is_grouped(value) -> bool:
    """Whether a ``final_retrieval`` value is a contextual-search group,
    ``{chunk: {"distance", "content"}}``, rather than an entry's own text."""
    return isinstance(value, dict) and bool(value) and all(
        isinstance(v, dict) and "content" in v for v in value.values()
    )


def retrieved_entries(final_retrieval: dict) -> dict:
    """``{id: text}`` for everything a retrieval returned, each id once.

    Most retrievals map an id to its text. Contextual (sibling) search nests
    one level deeper, ``{seed: {chunk: {"distance", "content"}}}``, grouping
    each match with the chunks around it; its entries are the inner chunks.
    """
    entries = {}
    for key, value in final_retrieval.items():
        if is_grouped(value):
            for inner_key, inner in value.items():
                entries.setdefault(str(inner_key), inner.get("content", ""))
        else:
            entries.setdefault(str(key), value)
    return entries


def retrieval_stats(final_retrieval: dict) -> RetrievalStats:
    stats = RetrievalStats()
    for key, value in retrieved_entries(final_retrieval).items():
        if _CHUNK_ID.search(key):
            stats.chunks += 1
            stats.chunk_chars += _chars(value)
        else:
            stats.others += 1
            stats.other_chars += _chars(value)
    return stats


def describe_retrieval(query_name: str, final_retrieval: dict, verb: str = "retrieved") -> str:
    """One line for the log and the step summary: e.g. "<query> retrieved 20
    chunk(s), 16,102,331 chars; 3 other entries, 12,345 chars"."""
    s = retrieval_stats(final_retrieval)
    line = f"{query_name} {verb} {s.chunks} chunk(s), {s.chunk_chars:,} chars"
    if s.others:
        line += (f"; {s.others} other entr{'y' if s.others == 1 else 'ies'}, "
                 f"{s.other_chars:,} chars")
    return line
