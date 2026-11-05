# Deep Agent chat engine

Agent mode answers chat questions with a [LangChain Deep Agent](https://docs.langchain.com/oss/python/deepagents/overview)
(`deepagents.create_deep_agent`) running on LangGraph, with short-term and long-term
memory persisted on-prem. Knowledge-graph creation, ingestion, ECC, retrieval queries
and the classic engine are unchanged. Only the component that takes a question and
decides which tools to use was replaced.

Code: `graphrag/app/deep_agent/` (engine), `graphrag/app/routers/deep_agent.py` (REST).

## How a question is answered

```
question ──► Deep Agent (configured chat model, LangGraph)
               │  write_todos      plan multi-part questions
               │  task             delegate to a subagent
               │     ├─ graph-analyst        structured graph data
               │     └─ document-researcher  ingested documents
               │  GraphRAG / TigerGraph / MCP tools   (run as the logged-in user)
               │  ls/read_file/write_file/edit_file   (scratch files + /memories/)
               ▼
           DeepAgentAnswer {answer, citations, answered_question}
```

* The model decides everything: whether a question needs tools (greetings are answered
  directly), which tools, in which order, when to delegate and when to stop. There is
  no separate triage, planner, executor or synthesizer.
* The final answer is a structured `DeepAgentAnswer` tool call (LangChain `ToolStrategy`),
  so any tool-calling chat model works. A plain-text final message is accepted too.
* Every tool returns JSON. Errors come back as text, so the agent can change approach.
  Each tool output is capped at `max_tool_output_chars`. Deep Agents also moves very
  large results to scratch files.
* The response keeps the existing `GraphRAGResponse` shape. `query_sources.agent_steps`
  (one entry per tool call, plus the answer step with token usage) feeds the Trace page.
  `reasoning` lists the retrieved passages, with cited ones prefixed `* `. Also included:
  `citations`, `retrieved_citations`, `todos` and `token_usage`.

## Tools

Built per request in `deep_agent/tools.py`. `GET /{graph}/agent/tools` lists what a
graph actually exposes.

### GraphRAG tools (existing retrievers and generators)

| Tool | Backed by | Use |
|---|---|---|
| `get_graph_schema` | `common.db.schema_utils.render_schema_rep` | Vertex/edge types, attributes, descriptions |
| `query_graph` | `MapQuestionToSchema` → `GenerateFunction`, with `GenerateCypher` fallback when `USE_CYPHER=true` | Natural-language question → generated query on structured data |
| `hybrid_search` | `HybridRetriever` (`GraphRAG_Hybrid_Vector_Search`) | Vector search + graph expansion over document chunks |
| `similarity_search` | `SimilarityRetriever` (`Content_Similarity_Vector_Search`) | Point vector search |
| `contextual_search` | `SiblingRetriever` (`Chunk_Sibling_Vector_Search`) | Matches plus neighbouring chunks (tables, lists) |
| `community_search` | `CommunityRetriever` (`GraphRAG_Community_Vector_Search`) | Community summaries for broad questions |

Retrieval parameters (`top_k`, `num_hops`, `community_level`) default to `graphrag_config`
and are clamped by `tools/tool_guards.py`.

### TigerGraph tools (read-only pyTigerGraph on the user's connection)

| Tool | pyTigerGraph |
|---|---|
| `get_vertex_count` | `getVertexCount` |
| `get_edge_count` | `getEdgeCount` |
| `get_vertices` | `getVertices` (limit ≤ 200) |
| `get_vertices_by_id` | `getVerticesById` |
| `get_edges` | `getEdges` (limit ≤ 200) |
| `list_installed_queries` | `getEndpoints` + `getQueryDescription` |
| `run_installed_query` | `runInstalledQuery` |
| `run_cypher_query` (only with `USE_CYPHER=true`) | `check_cypher` + interpreted openCypher. Write clauses are rejected |

`run_installed_query` never runs GraphRAG's own maintenance queries (every query under
`common/gsql/`) or queries whose names look like data changes (delete, update, insert,
load, …). `deep_agent.installed_query_allowlist` (globs) narrows it further, and
`allow_installed_queries: false` removes both installed-query tools. The name check is a
guard, not a guarantee. TigerGraph RBAC on the user's connection is the real boundary.

### MCP add-on tools

The external MCP servers configured for the graph (Setup → MCP servers), filtered by
each server's `allowed_tools`, are exposed under sanitized names (`server__tool`).

### Built-in Deep Agent tools

`write_todos`, `task` (subagents), and `ls`, `read_file`, `write_file`, `edit_file`,
`glob`, `grep` on a virtual filesystem. There is no host filesystem access, and `execute`
is unavailable because the backend is not a sandbox.

### Not exposed

The mutating and ingestion endpoints, `GenerateGSQL` (writes and installs queries),
`FindExistingQuery` (replaced by `list_installed_queries`/`run_installed_query`), and
`EntityRelationshipRetriever` (its query is not installed by default).

## Memory

| | What | Where | Scope |
|---|---|---|---|
| STM | The conversation thread (messages, todos, scratch files) | LangGraph checkpointer | `graph:user:conversation_id` |
| LTM | Files under `/memories/`, `AGENTS.md` loaded into every conversation | LangGraph store, namespace `("deep_agent", graph, user, "memories")` | user + graph, across conversations |

* The chat UI (WebSocket and `GET /ui/{graph}/query`) passes its conversation id, so a
  conversation continues on its thread. When a thread does not exist yet (for example an
  older conversation), it is seeded with the history from the chat-history service.
* Long threads are compacted by the Deep Agent summarization middleware.
* The agent records lasting user preferences in `/memories/AGENTS.md`. It is told never
  to store retrieved data, answers or credentials there.
* `POST /{graph}/query` and `/{graph}/query_with_history` keep their stateless contract:
  each runs on a private in-memory thread (seeded with the request's `history`), with no
  server-side conversation.
* Memory is keyed by user name, so it needs a password (Basic) login. Token logins on the
  REST API get no persistent thread and no long-term memory. In the chat UI the token's
  user is resolved first.

## Configuration

```json
"graphrag_config": {
  "agent_mode": "agentic",
  "agent_engine": "deep",
  "deep_agent": {
    "memory_backend": "sqlite",
    "memory_path": "/code/deep_agent_data/memory.sqlite",
    "postgres_uri": "",
    "recursion_limit": 80,
    "long_term_memory": true,
    "subagents": true,
    "allow_installed_queries": true,
    "installed_query_allowlist": ["*"],
    "max_tool_output_chars": 60000
  }
}
```

* `agent_engine: "legacy"` switches Agent mode back to the previous planner/react engine.
  Classic mode, and the automatic fallback to Classic for models without tool calling,
  are unchanged.
* Persistence settings are process-wide. The environment variables
  `DEEP_AGENT_MEMORY_BACKEND`, `DEEP_AGENT_MEMORY_PATH` and `DEEP_AGENT_POSTGRES_URI`
  override them.
* `sqlite` (default) needs only `langgraph-checkpoint-sqlite`. `docker-compose.yml` mounts
  `./data/deep_agent/` at `/code/deep_agent_data` so memory survives container recreation.
  Run one GraphRAG replica per SQLite file.
* `postgres` also needs `langgraph-checkpoint-postgres`, `psycopg[binary]` and
  `psycopg-pool`. Use it for multiple replicas.
* If the configured backend cannot start, the service logs a warning and keeps memory in
  process until restart.
* Prompt customization still applies: the user portions of `chatbot_response.txt` (answer
  style) and `agentic_agent.txt` (additional instructions) are added to the Deep Agent
  prompt.

## REST API

All routes authenticate like the other graph routes (Basic or token) and act on the
caller's own conversations and memories.

| Method | Path | |
|---|---|---|
| POST | `/{graph}/agent/chat` | `{"query", "conversation_id"?, "include_fields"?}` → answer + `conversation_id` (new when omitted) |
| GET | `/{graph}/agent/tools` | Tools available on the graph, by family |
| GET | `/{graph}/agent/threads` | The caller's conversations |
| GET | `/{graph}/agent/threads/{conversation_id}` | Messages of one conversation, with tool calls |
| DELETE | `/{graph}/agent/threads/{conversation_id}` | Forget one conversation |
| GET | `/{graph}/agent/memories` | Long-term memory files |
| GET / PUT / DELETE | `/{graph}/agent/memories/{path}` | Read / write `{"content"}` / delete one file, e.g. `AGENTS.md` |

The existing endpoints (`/{graph}/query`, `/{graph}/query_with_history`,
`/ui/{graph}/chat`, `/ui/{graph}/query`) use the Deep Agent whenever Agent mode is
selected.

```bash
curl -u user:pass -X POST localhost:8000/MyGraph/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"query": "Which suppliers deliver to more than 3 plants?", "include_fields": ["query_sources"]}'
```
