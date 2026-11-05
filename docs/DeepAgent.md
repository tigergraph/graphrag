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

## Calling the agent from another agent or service

`/{graph}/agent/*` is served by the backend on port 8000. The bundled nginx also routes
it on port 80, so `http://<host>/{graph}/agent/chat` and `http://<host>:8000/{graph}/agent/chat`
are equivalent.

### Instructions for the calling agent

Paste this block into the calling agent's system prompt or tool documentation:

```markdown
## TigerGraph GraphRAG agent

You can ask a GraphRAG agent questions about the TigerGraph knowledge graph "<GRAPH>".
It searches the graph and the documents ingested into it, and answers with citations.
Use it for any question about that data. Don't answer such questions from your own knowledge.

Endpoint: POST http://<host>/<GRAPH>/agent/chat
Auth: HTTP Basic with a TigerGraph user. Answers respect that user's TigerGraph permissions.
Body (JSON):
  query            (string, required)   the question, in natural language, any language
  conversation_id  (string, optional)   omit for a new conversation; send the id from the previous
                                        response to ask a follow-up ("what about them?")
  include_fields   (list, optional)     ["query_sources"] to also get the trace (tool calls,
                                        cited passage ids, token usage)
Response (JSON):
  conversation_id            reuse it for follow-ups on the same topic
  natural_language_response  the answer (Markdown)
  answered_question          false = the knowledge graph did not have the information
  response_type              "deep_agent"
  query_sources              only when requested

Rules:
- Ask one self-contained question per call. For follow-ups, pass the same conversation_id;
  start a new conversation (omit the id) when the topic changes.
- If answered_question is false, don't invent an answer: report that the knowledge graph doesn't
  contain it, or rephrase the question more specifically and ask once more.
- A call can take 10-120 seconds; use a timeout of at least 180 s.
- Errors: 401 = bad credentials, 503 = database or vector store unavailable (retry later),
  422 = malformed body. Agent failures return 200 with answered_question=false and a generic message.
- The GraphRAG agent remembers stated preferences (e.g. "answer in Japanese") for this user across
  conversations. Don't send such instructions unless you mean them to persist.
```

### Tool definition

```json
{
  "name": "ask_graphrag",
  "description": "Ask the TigerGraph GraphRAG agent a question about the knowledge graph and its documents. Returns a cited answer. Pass conversation_id from a previous answer to ask a follow-up.",
  "input_schema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "Self-contained question in natural language."},
      "conversation_id": {"type": "string", "description": "Id from a previous answer, to continue that conversation."}
    },
    "required": ["query"]
  }
}
```

### Examples

```bash
curl -s -u USER:PASS -X POST http://<host>/MyGraph/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"query": "How many publications are in the graph?"}'

# follow-up in the same conversation
curl -s -u USER:PASS -X POST http://<host>/MyGraph/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"query": "Which of them were issued by the FSA?", "conversation_id": "<id from the first answer>"}'
```

```python
import requests

def ask_graphrag(query, conversation_id=None, host="http://<host>", graph="MyGraph",
                 auth=("USER", "PASS")):
    body = {"query": query}
    if conversation_id:
        body["conversation_id"] = conversation_id
    r = requests.post(f"{host}/{graph}/agent/chat", json=body, auth=auth, timeout=180)
    r.raise_for_status()
    return r.json()  # answer in natural_language_response; reuse conversation_id
```

### Things that behave differently than expected

* With a token (`Authorization: Bearer`), `conversation_id` comes back `null`. Every call
  is then stateless and nothing is remembered. Use Basic authentication for follow-ups.
* Long-term memory is per TigerGraph user. Callers that share one account share its
  stored preferences. Give each calling agent its own TigerGraph user if that matters.
* The traffic is plain HTTP. Put TLS in front (or terminate it in nginx) before exposing
  the endpoint beyond a trusted network, since requests carry Basic credentials.
