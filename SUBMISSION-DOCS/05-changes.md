# Files Added & Modified

## New: storage and access control

| File | Purpose |
|---|---|
| `common/chat_history/repository.py` | `ConversationRepository`, `TraceRepository`, `TraceRetention`, `AdminFeedbackRepository`, the only sanctioned reader/writer of chat data |
| `common/chat_history/guard.py` | Recognizes chat types/queries, filters schema listings, refuses agent calls naming a chat type in any argument |
| `common/chat_history/trace_writer.py` | Bounded background queue for off-response-path trace writes |
| `common/chat_history/retention_sweep.py` | Timer-driven trace expiry across all graphs |
| `common/chat_history/__init__.py` | Public re-exports |

## New: GSQL

| File | Purpose |
|---|---|
| `common/gsql/chat_history/ChatHistory_Schema.gsql` | The `add_chat_history_schema` schema-change job |
| `Chat_List_Conversations.gsql` | A user's conversations, newest first |
| `Chat_Get_Conversation.gsql` | One conversation's messages, ordered, with `REPLIES_TO` parents resolved |
| `Chat_Get_Trace.gsql` | One trace, steps in order, union of trace- and step-level `RETRIEVED` |
| `Chat_Search_My_Messages.gsql` | Substring search over a user's own messages |
| `Chat_Get_Feedback.gsql` | A user's own flagged messages |
| `Chat_Get_All_Feedback.gsql` | All users' feedback, admin only, separate query by design |
| `Chat_Delete_Conversation.gsql` | Hard delete, cascading bottom-up |
| `Chat_Expire_Traces.gsql` | Retention expiry by cutoff epoch |

## New: agent tools

| File | Purpose |
|---|---|
| `graphrag/app/tools/chat_history_tools.py` | `list_my_conversations`, `get_my_conversation`, `search_my_messages`, operating on `ctx.chat_repo` |

## New: misc

| File | Purpose |
|---|---|
| `.dockerignore` | Excludes caches, `.git`, scratch files from the build context |

---

## Modified

| File | Change |
|---|---|
| `graphrag/app/routers/ui.py` | **Largest change.** Replaces every `httpx` call to the retired service with repository calls. Adds `_chat_principal` (fail-closed identity), `_chat_conn`, `_chat_repo_for_agent`, and wire-format translators (`_conversation_to_wire`, `_message_to_wire`, `_trace_to_wire`) so the existing frontend is unmodified. `_save_trace_log` rewritten to build `ChatTraceStep` rows from `query_sources` and queue the write. Rewrites `/ui/feedback`, `/ui/trace/{message_id}`, `/ui/user/{user_id}`, `/ui/conversation/{id}` (GET + DELETE), `/ui/get_feedback` |
| `common/metrics/tg_proxy.py` | **Task 3 enforcement point.** Schema filtering, argument-content refusal, `Chat_` query-name refusal on agent-issued calls |
| `graphrag/app/supportai/supportai.py` | `init_supportai` applies the chat schema after the corpus schema (idempotently) and installs the `Chat_*` queries |
| `common/db/schema_utils.py` | Adds `CHAT_HISTORY_VERTEX_TYPES` / `CHAT_HISTORY_EDGE_TYPES`, folded into `GRAPHRAG_STRUCTURAL_*` so users can't declare colliding domain types |
| `common/db/query_sets.py` | Adds `CHAT_HISTORY_QUERIES` to the install + migration sets |
| `common/config.py` | Exposes `chat_store_config` from `chat_config` |
| `common/llm_services/base_llm.py` | Adds the "Conversation History" section to both engines' prompts (scope + how to phrase a refusal); tightens planner guidance so required tool args are filled from the question rather than left to `arg_bindings`. Unrelated: pins `with_structured_output(..., method="function_calling")` |
| `common/py_schemas/schemas.py` | `PlanStep` tolerates field synonyms (`name`→`id`, `params`→`args`) from LLM-generated plans |
| `graphrag/app/tools/tool_registry.py` | Registers the three `chat_history__*` tools; withholds them entirely when `ctx.chat_repo` is `None` |
| `graphrag/app/tools/graphrag_tools.py` | Adds `chat_repo` to `GraphRAGToolContext` |
| `graphrag/app/agent/agent.py`, `agentic_agent.py` | Thread `chat_repo` from `make_agent` into the tool context |
| `graphrag/app/agent/agentic_executor.py` | `arg_bindings` falls back to treating a value as a literal when it doesn't name an earlier step |
| `graphrag/app/agent/agentic_planner.py` | Logs the generated plan (truncated) for diagnosability |
| `graphrag/app/main.py` | Starts/stops `RetentionSweeper` on startup/shutdown |
| `docker-compose.yml` | Removes the `chat-history` service; enables the previously-commented-out `tigergraph` service |
| `configs/nginx.conf` | Converted from a git symlink to a real file (symlinks break on Windows checkout); adds the WebSocket upgrade route for `/ui/*/chat`. No route change was needed for this feature: the retired service was never reachable through nginx |

## Deliberately left alone

- **`chat-history/`** (Go source): disconnected but still on disk. See
  [02-architecture.md](02-architecture.md).
- **`graphrag_config["chat_history_api"]`**: inert, kept so existing config files
  keep loading.
