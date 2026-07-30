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

# Dedicated operational graph queries. These are installed only on
# ``GraphRAGChatHistory`` by ``common.chat_history.bootstrap``; they are not
# part of application-graph initialization or Migration Assistant checks.
CHAT_HISTORY_QUERIES = [
    "common/gsql/chat_history/Chat_History_Health",
    "common/gsql/chat_history/Chat_Begin_Turn",
    "common/gsql/chat_history/Chat_Complete_Turn",
    "common/gsql/chat_history/Chat_List_My_Conversations",
    "common/gsql/chat_history/Chat_Get_My_Conversation",
    "common/gsql/chat_history/Chat_Search_My_Messages",
    "common/gsql/chat_history/Chat_Get_My_Feedback",
    "common/gsql/chat_history/Chat_Update_My_Feedback",
    "common/gsql/chat_history/Chat_Delete_My_Conversation",
    "common/gsql/chat_history/Chat_Get_My_Trace",
    "common/gsql/chat_history/Chat_Get_All_Feedback_Admin",
    "common/gsql/chat_history/Chat_Expire_Traces_Admin",
    "common/gsql/chat_history/Chat_Import_Legacy_Message",
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
