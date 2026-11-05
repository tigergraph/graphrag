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

"""Deep Agent chat engine.

A LangChain Deep Agent (``deepagents.create_deep_agent``) running on
LangGraph answers chat questions. It decides on its own which GraphRAG and
TigerGraph tools to call, plans multi-step work with its todo list, hands
focused research to subagents, and keeps:

* short-term memory (STM): the conversation thread, persisted by a LangGraph
  checkpointer keyed by user, graph and conversation;
* long-term memory (LTM): files under ``/memories/`` persisted in a LangGraph
  store, scoped per user and graph, and an ``AGENTS.md`` loaded into every
  conversation.

Knowledge-graph creation, ingestion and retrieval are unchanged: the tools
call the existing retrievers and query generators.
"""

from deep_agent.agent import DeepGraphRAGAgent

__all__ = ["DeepGraphRAGAgent"]
