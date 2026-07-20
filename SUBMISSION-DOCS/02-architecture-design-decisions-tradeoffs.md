# Architecture, Design Decisions & Tradeoffs

## Key design decisions (summary)

- **Python + TigerGraph over Go + SQLite:** one client, one schema owner, one authorization boundary instead of two persistence stacks (see below).
- **Star schema, fine-grained vertices:** `ChatUser → ChatConversation → ChatMessage`, not a JSON blob or a message chain, so writes are single upserts and reads are bounded hops (see [01-schema.md](01-schema.md)).
- **Traces as separate vertices from messages:** different lifecycle (retention-expired vs. permanent) and different size (large serialized payloads vs. short text).
- **Identity resolved via TigerGraph's own `SHOW USER`, never the wire username:** the Basic-auth username collapses multiple real users onto one shared identity for token/secret logins.
- **Isolation enforced by traversal, not filtering:** every scoped query seeds at one `ChatUser`; there is no unscoped form to misuse.
- **Argument-content inspection at the proxy, not tool naming:** the agent reaches the graph through a general-purpose call path, so gating by registered tool/function name alone doesn't stop `getVertices('ChatConversation')`.

## Core design decision

**Rejected: keep the Go service and replace SQLite with TigerGraph inside it.**

This would have minimized changes to the existing Go code, but trace persistence already lives in Python and also needed to move to TigerGraph. The result would have been two TigerGraph persistence implementations in two languages, duplicated schema ownership, and authorization still split across codebases: the existing defect rather than a fix.

**Chosen: consolidate chat history and traces in Python and retire the Go service.**

A Python TigerGraph layer was already required for traces. Keeping conversations beside it provides one client, one schema owner, and one authorization boundary.

This was relatively low-cost because the Python service was already the full proxy in front of the Go service. Nothing outside `graphrag` called port `8002`, and nginx had no route to it. The change therefore replaced a small number of `httpx` call bodies with repository calls rather than reimplementing the external API.

### Where the data actually lives now

There is no SQLite file anywhere in the write or read path. `chats.db` was the
Go service's store; it's retired along with the service (see
[Status of the legacy chat-history service](#status-of-the-legacy-chat-history-service)
below) and nothing in the current code opens it. Every conversation, message,
trace, and trace step is a vertex in TigerGraph, written through
`common/chat_history/repository.py` via `upsertVertex`/`upsertEdge`, and read
back through the installed `Chat_*` queries, never cached in the app process
and never touching local disk. That's why the data survives a `graphrag`
container restart: durability comes from TigerGraph, not from the app that
happens to be running in front of it.

## System architecture

```text
  UI  ──► /ui/* endpoints (graphrag:8000) ──► repository layer ──► TigerGraph
                    │                              raw connection
                    │
  Agent ──► chat_history__* tools ───────────────────────┘
        │
        └─► general retrieval tools ──► TigerGraphConnectionProxy ──► TigerGraph
                                        filtered and guarded
```

The system deliberately uses two connection paths:

- **Repository to raw connection:** the reviewed path for chat data. It is not filtered because filtering the repository against itself would break it.
- **Agent to proxy connection:** all agent-issued graph operations are filtered and guarded. The proxy refuses chat types and enforces the Task 3 controls.



## Repository and authorization model

All chat-history persistence goes through `common/chat_history/repository.py`.

- `ConversationRepository`: scoped to one principal; handles conversations, messages, and feedback.
- `TraceRepository`: scoped to one principal; handles execution traces.
- `TraceRetention`: unscoped across all graphs; performs background expiry sweeps.
- `AdminFeedbackRepository`: unscoped across users; supports the admin feedback view, with caller-side role enforcement.

The principal-scoped repositories bind the principal at construction and expose no method that accepts a user ID. The all-users capability is isolated in `AdminFeedbackRepository`, so it cannot be reached by passing an incorrect user ID to a scoped repository.

## Write and read behavior



### Messages

Messages are written synchronously within the request that produced them.

- Writes are idempotent on `message_id`; retries re-upsert the same vertex.
- `None` fields are omitted, so a feedback-only update cannot erase existing content.
- `seq` is assigned server-side. A rewrite keeps the message's existing value; a new message appends after the current maximum.
- Write failures are logged rather than raised because persistence occurs after the answer has streamed. Raising at that point could only discard a response already visible to the user.
- `seq` uses a read-then-write assignment. Concurrent appends to the same conversation could collide, but the expected flow is one user and one assistant writing in order. Reads use `create_epoch` as a tie-breaker.



### Traces

Traces are written asynchronously through `TraceWriter` in `common/chat_history/trace_writer.py`.

**What actually gets written**, per assistant turn (full field list in
[01-schema.md](01-schema.md)):
- One `ChatTrace` vertex: the question as asked, which engine answered,
  whether it believed it answered, token counts, total response time, and the
  serialized agent plan (truncated at 16k chars).
- One `ChatTraceStep` vertex per tool/node the run touched: name, ordinal,
  duration, truncated input/output, token counts, and `ok`/`error` status,
  chained in order by `NEXT_STEP`.
- `RETRIEVED` edges from the trace (and/or its steps) to whichever
  `DocumentChunk`/`Entity`/`Community` vertices were actually cited, never
  the retrieved text itself, only the edge, so provenance doesn't duplicate
  corpus content into chat storage.

- A single background thread drains a bounded queue of 512 items.
- `submit()` does not block or raise because it runs on the response path.
- When the queue is full, the trace is dropped and counted rather than retried. Retrying could compound overload, while an unbounded queue risks exhausting memory. Traces are diagnostic; service availability takes priority.
- `stats()` exposes submitted, written, dropped, and failed counts so loss remains observable.
- This replaces a synchronous writer that also ran a full directory scan through `_cleanup_old_traces` on every response.
- A trace can still be lost under load or during restart; this is an accepted tradeoff.



### Retrieved targets

`RETRIEVED` targets are verified before edges are created. TigerGraph's `upsertEdge` can create a missing endpoint, so linking an unverified ID could fabricate an empty `DocumentChunk` in the shared corpus. Because agent output may reference re-ingested or deleted chunks, `_live_targets()` batch-verifies targets with one lookup per type and drops unresolved IDs.

### Conversation reads

`Chat_Get_Conversation` returns the conversation, messages ordered by `seq` and then `create_epoch`, and a parent map resolved from `REPLIES_TO`.

Ownership is enforced by traversal rather than post-filtering. Messages are reachable only through `OWNS_CONVERSATION` from the caller's `ChatUser`; another user's `conversation_id` therefore returns no result. There is no separate ownership branch that can be inverted or omitted.

**How a read actually reaches TigerGraph.** Both entry points funnel through the
same principal-scoped repository. There is no second read path with its own
rules:

- **UI:** `/ui/user/{user_id}` and `/ui/conversation/{id}` in `ui.py` resolve
  the caller's identity, construct a `ConversationRepository` bound to it, and
  call `list_conversations()` / `get_conversation()`, which run
  `Chat_List_Conversations` / `Chat_Get_Conversation` and translate the result
  into the existing frontend wire format.
- **Agent:** `chat_history__list_my_conversations` /
  `chat_history__get_my_conversation` /
  `chat_history__search_my_messages` in `chat_history_tools.py` call the
  identical repository methods on `ctx.chat_repo`, the same queries, the same
  ownership traversal, just a different caller.

Either way, retrieval is always a fresh query against TigerGraph, not a cache:
there is no in-process store of conversation state that could drift from what
the graph actually holds.

## Conversation context windows

TigerGraph stores and returns the full conversation. Context limits are applied only when history is sent to the model:

- **Agentic planner and triage:** the conversation is serialized to JSON and truncated to 2,000 characters in `agentic_planner.plan_question` and `_triage_question`.
- **Classic path:** `agent_graph.contextualize_question` receives the last four turns through `conversation[-4:]`.

These limits keep per-turn prompt cost and latency bounded without deleting stored history. Users can still retrieve the complete conversation through the tools or `/ui/conversation/{id}`.

The different window types are pre-existing behavior: the classic path is turn-based, while the agentic path is character-based. The classic path also rewrites follow-up questions into self-contained queries before retrieval.

## Retention and deletion

`RetentionSweeper` in `common/chat_history/retention_sweep.py` starts with the service and stops at shutdown.

- It deletes expired traces and trace steps; messages are not affected.
- Trace retention is a privacy control because traces can contain user prompts and retrieved document text.
- The default sweep interval is 24 hours, with a five-minute startup delay to avoid competing with schema initialization.
- `retention_days <= 0` disables retention instead of deleting all traces.
- In multi-worker deployments, each worker runs the sweep. Deletes are idempotent, so this causes redundant daily queries rather than incorrect behavior; external coordination was not worth the added dependency.

Conversation deletion is a hard, irreversible delete that cascades through messages, traces, and steps from the bottom up, ensuring no edge outlives its endpoints. Tombstones were rejected because they would retain prompts and retrieved text indefinitely, undermining the erasure control. Shared corpus vertices are never deleted.

Configuration is under `chat_config` in `server_config.json`:

- `conversationAccessRoles`, default `["superuser", "globaldesigner"]`: roles allowed to view feedback across all users.
- `traceRetentionDays`, default `30`: trace retention period; `0` disables expiry.
- `traceRetentionSweepHours`, default `24`: sweep interval.



## Other accepted tradeoffs

- **No reverse edges:** identifying every conversation that cited a chunk is not a cheap live query; it requires an admin scan or offline projection. The isolation model partly offsets the graph-querying benefit.
- **Availability coupling:** TigerGraph failure now affects both retrieval and history. Previously it could leave history available, but retrieval failure already prevented useful answers.
- **Graph-agnostic route fan-out:** `/ui/user` and `/ui/conversation` do not accept a graph name, so they query all graphs available to the caller. This costs N queries for N graphs but preserves the existing frontend contract.
- **Schema hiding is non-load-bearing:** it is retained as low-cost obscurity, but security depends on validating argument content rather than hiding schema names.



## Assumptions and boundaries

- **Scale:** approximately 1,000 users, 50 conversations per user, 20 messages per conversation, and 50 trace steps per answer: about 50,000 conversations, 1 million messages, and 25 million trace steps. This informed query design but is not enforced.
- **Read fan-out:** bounded by a user's own history under the stated scale assumption.
- **Shared corpus access:** there are no document-level ACLs. Anyone with a role on a graph can see every document in that graph, as before. Conversation isolation and document authorization are separate concerns.
- **Database isolation:** users are assumed not to have direct database access. All controls are application-layer and can be bypassed through direct TigerGraph access.
- **Credential model:** GraphRAG logins are TigerGraph credentials, so network isolation of ports `9000` and `14240`, together with minimal role provisioning, is load-bearing.
- **Greenfield deployment:** there is no migration from `chats.db`. The SQLite schema does not record which graph a conversation belongs to, yet conversations must live in the graph where the chat ran so `RETRIEVED` edges can resolve. Migration would require guessing the graph or assigning all history to one graph and losing the linkage that motivates the design.



## Status of the legacy chat-history service

The Go chat-history service is retired and disconnected but remains in the repository pending mechanical cleanup.

- It has been removed from `docker-compose.yml`, including its image, build context, and port `8002`.
- No traffic routes to it. Even before retirement, the UI called `graphrag:8000/ui/*`, and the Python service proxied requests to `chat-history:8002`; nginx never exposed the Go service directly.
- The `chat-history/` source remains because deleting it also requires updates to `graphrag-k8s.yml`, CI workflows, a UI settings field, and the README. Those changes do not affect the behavior described here.
- `graphrag_config["chat_history_api"]` is inert and retained only so existing `server_config.json` files continue to load. It should be deprecated or removed because a no-op configuration option is misleading.
- The legacy settings `apiPort`, `dbPath`, `dbLogPath`, and `logPath` have been removed because they configured the retired SQLite store.

The legacy service is not a fallback, and no active code path can use it.

## Future considerations

Not implemented; noted here as a direction worth evaluating if conversation
length or context-window cost becomes a real constraint.

**Incremental rolling-summary index.** Today, context truncation is
size-based and stateless: the agentic path serializes and cuts the
conversation at 2,000 characters, the classic path takes the last four turns
(see [Conversation context windows](#conversation-context-windows)). Either
way, older turns are simply dropped from what the model sees, even if they
were relevant. An alternative is a maintained summary that grows
incrementally: each new turn folded into a running summary vertex (or
attribute) rather than every turn being re-truncated from scratch, so the
model gets compressed context from the *whole* conversation instead of a
recency-only window.

**Tradeoffs that kept this out of scope for now:**
- It requires an LLM call to update the rolling summary (either per turn or
  on a cadence), adding latency and token cost to the write path that today's
  truncation doesn't pay.
- A useful version of this also wants the conversation embedded for semantic
  (not just recency-based) retrieval, which adds embedding compute, storage,
  and a second representation of the conversation that has to be kept in sync
  with the raw messages, versus today's model, where `ChatMessage` is the
  only source of truth and nothing else can drift from it.
- Net effect: better long-conversation context at the cost of steady-state
  compute and an added consistency surface, in exchange for a problem
  (context loss on long threads) that isn't yet observed at the stated scale
  assumption of ~20 messages per conversation.
