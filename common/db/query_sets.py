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

"""Canonical lists of shipped GSQL query paths.

Single source of truth shared by the SupportAI graph initializer, the ECC
rebuild, and the Migration Assistant so the sets can't drift apart. Paths are
stems (no ``.gsql`` suffix), matching the ECC installer; callers that open the
file directly wrap them with :func:`with_gsql`.
"""

# GraphRAG streaming / processing queries the ECC rebuild installs.
GRAPHRAG_REQUIRED_QUERIES = [
    "common/gsql/graphrag/StreamIds",
    "common/gsql/graphrag/StreamDocContent",
    "common/gsql/graphrag/StreamChunkContent",
    "common/gsql/graphrag/SetEpochProcessing",
    "common/gsql/graphrag/get_vertices_or_remove",
]

# Community-detection (Louvain) queries.
GRAPHRAG_COMMUNITY_QUERIES = [
    "common/gsql/graphrag/louvain/graphrag_louvain_init",
    "common/gsql/graphrag/louvain/graphrag_louvain_communities",
    "common/gsql/graphrag/louvain/modularity",
    "common/gsql/graphrag/louvain/stream_community",
    "common/gsql/graphrag/get_community_children",
    "common/gsql/graphrag/communities_have_desc",
    "common/gsql/graphrag/graphrag_delete_all_communities",
    "common/gsql/graphrag/graphrag_stream_entity_community_pairs",
    "common/gsql/graphrag/graphrag_stream_all_ids",
]

# SupportAI status / processing queries installed at graph initialization.
SUPPORTAI_INIT_QUERIES = [
    "common/gsql/supportai/Scan_For_Updates",
    "common/gsql/supportai/Update_Vertices_Processing_Status",
    "common/gsql/supportai/Selected_Set_Display",
]

# Retrievers installed on vector-enabled graphs and used by chat/search. Only
# the vector variants and Display queries are installed on schema-aware v2.0
# graphs; the legacy non-vector retrievers are intentionally omitted.
SUPPORTAI_RETRIEVER_QUERIES = [
    "common/gsql/supportai/retrievers/Chunk_Sibling_Vector_Search",
    "common/gsql/supportai/retrievers/Content_Similarity_Vector_Search",
    "common/gsql/supportai/retrievers/GraphRAG_Community_Vector_Search",
    "common/gsql/supportai/retrievers/GraphRAG_Hybrid_Vector_Search",
    "common/gsql/supportai/retrievers/GraphRAG_Community_Search_Display",
    "common/gsql/supportai/retrievers/GraphRAG_Hybrid_Search_Display",
]

# Eventual-consistency-checker queries. The ECC checker is opt-in and off by
# default, so these are NOT part of what a graph normally needs — they are
# excluded from the Migration Assistant's required set.
ECC_CHECKER_QUERIES = [
    "common/gsql/supportai/ECC_Status",
    "common/gsql/supportai/Check_Nonexistent_Vertices",
]

# What the Migration Assistant verifies for a GraphRAG graph: everything a
# GraphRAG graph actually installs. Excludes the opt-in ECC-checker queries.
MIGRATION_QUERIES = (
    GRAPHRAG_REQUIRED_QUERIES
    + GRAPHRAG_COMMUNITY_QUERIES
    + SUPPORTAI_INIT_QUERIES
    + SUPPORTAI_RETRIEVER_QUERIES
)


def with_gsql(paths: list[str]) -> list[str]:
    """Append the ``.gsql`` suffix to each stem for callers that open files."""
    return [p + ".gsql" for p in paths]
