# Schema Reference (Task 1)

Defined in `common/gsql/chat_history/ChatHistory_Schema.gsql`, applied as the
schema-change job `add_chat_history_schema`.

Video walkthrough of this schema as it actually appears in TigerGraph:
[https://www.loom.com/share/afdc5f0627c44100ad6334d5952027e3](https://www.loom.com/share/afdc5f0627c44100ad6334d5952027e3)

All types are `Chat`-prefixed. GraphRAG lets users extend a graph with their own
domain types, and a bare `User` / `Message` / `Conversation` is a plausible name
for someone to pick. The prefix prevents collision, and it also makes the
agent-exclusion rule a prefix match instead of a hand-maintained list.

### Why this shape, and not a JSON blob or relational tables

A conversation is stored as one vertex per user/conversation/message/trace/step
rather than a single serialized-JSON document per conversation or a
normalized-relational mapping, because every read and write here is a
primary-id hop, not a scan: appending a message is one idempotent upsert,
never a read-modify-write of a growing blob, and fan-out per vertex is bounded
by conversation length, not by total history size. That keeps cost flat as
users and history grow: the same query plan holds at 1,000 users or 100,000
users, unlike a blob (full rewrite on every append) or a chain (unbounded
traversal depth per read).

---

## Vertex types

### `ChatUser`
One per person who has ever sent a message. Created lazily on first write.

| Property | Type | Notes |
|---|---|---|
| `user_name` | STRING | **PRIMARY_ID.** The TigerGraph-resolved username, never the Basic-auth wire username |

### `ChatConversation`
One thread.

| Property | Type | Notes |
|---|---|---|
| `conversation_id` | STRING | **PRIMARY_ID.** UUID generated per thread |
| `name` | STRING | Display title; may be empty |
| `create_epoch` | UINT | Insert-only, never overwritten on update |
| `update_epoch` | UINT | Bumped on every message append; drives "newest first" ordering |
| `deleted_epoch` | UINT | `0` = live. Soft-hide for listings only; real deletion is a hard delete |

### `ChatMessage`
One turn, user or assistant.

| Property | Type | Notes |
|---|---|---|
| `message_id` | STRING | **PRIMARY_ID.** Makes writes idempotent, so a client retry re-upserts rather than duplicating |
| `content` | STRING | The message text |
| `role` | STRING | `user` or `system` |
| `model_name` | STRING | Model that produced it; empty for user turns |
| `response_time` | DOUBLE | Seconds; assistant turns only |
| `seq` | INT | Position in the thread. Assigned server-side |
| `feedback` | INT | `0` none, `1` thumbs up, `2` thumbs down |
| `comment` | STRING | Free-text feedback |
| `create_epoch` | UINT | Insert-only. Tiebreaker when two messages share a `seq` |

### `ChatTrace`
One execution trace per assistant message. A separate vertex rather than columns
on `ChatMessage`, because traces expire on a retention timer and messages don't:
different lifecycle, different vertex.

| Property | Type | Notes |
|---|---|---|
| `trace_id` | STRING | **PRIMARY_ID.** Deliberately equal to the `message_id`. The relationship is 1:1, so reusing the id makes lookup a primary-id hit and keeps retries idempotent |
| `user_query` | STRING | The question as asked |
| `response_type` | STRING | Which engine/path answered |
| `response_time` | DOUBLE | Total seconds |
| `answered_question` | BOOL | Whether the agent believed it answered |
| `natural_language_response` | STRING | Final answer text |
| `tokens_in` / `tokens_out` | INT | Token usage for the whole answer |
| `plan` | STRING | Serialized agent plan (JSON, truncated at 16k chars) |
| `create_epoch` | UINT | Drives retention expiry |

### `ChatTraceStep`
One step of the agent's plan or tool-call loop.

| Property | Type | Notes |
|---|---|---|
| `step_id` | STRING | **PRIMARY_ID.** Format `"{message_id}:{index}"` |
| `name` | STRING | Node/tool name, e.g. `graphrag__hybrid_search` |
| `idx` | INT | Ordinal within the trace |
| `duration_ms` | INT | |
| `input` / `output` | STRING | Serialized payloads, truncated at 16k chars each |
| `tokens_in` / `tokens_out` | INT | |
| `status` | STRING | `ok` or `error` |

---

## Edge types

All directed. **No `REVERSE_EDGE` on any of them.** See
[03-access-control.md](03-access-control.md) for why that is deliberate, and why
it is not the thing actually enforcing isolation.

| Edge | From → To | Purpose |
|---|---|---|
| `OWNS_CONVERSATION` | `ChatUser` → `ChatConversation` | The only entry point into a user's data. Every scoped query starts here |
| `HAS_MESSAGE` | `ChatConversation` → `ChatMessage` | Star, not a chain. Reading a thread is one hop regardless of length |
| `REPLIES_TO` | `ChatMessage` → `ChatMessage` | Which turn a message answered. Lets one parent have two children (regenerated answer), a shape `seq` alone can't express |
| `HAS_TRACE` | `ChatMessage` → `ChatTrace` | 1:1 |
| `HAS_STEP` | `ChatTrace` → `ChatTraceStep` | Star |
| `NEXT_STEP` | `ChatTraceStep` → `ChatTraceStep` | Step ordering. Edges rather than an integer because planned-engine steps form a DAG, where two steps can both depend on one earlier step and run in parallel |
| `RETRIEVED` | `ChatTrace` → `DocumentChunk` \| `Entity` \| `Community`<br>`ChatTraceStep` → `DocumentChunk` \| `Entity` \| `Community` | **Provenance.** Points at corpus vertices that already exist, with no text duplication |

### Why `RETRIEVED` is declared from both `ChatTrace` and `ChatTraceStep`

Per-step attribution is more useful in principle. In practice the current agent
reports retrieval at whole-answer granularity
(`query_sources["retrieved_citations"]`), so most traces populate only the
trace-level edge. The step-level pairs exist so the schema doesn't need to change
when per-node attribution becomes available. The write path handles both shapes.

---

## Worked example

Two users on the same graph. Alice asks one question; Bob asks a different one.
This is the actual vertex/edge structure that lands in TigerGraph. See it live
in the schema walkthrough:
[https://www.loom.com/share/afdc5f0627c44100ad6334d5952027e3](https://www.loom.com/share/afdc5f0627c44100ad6334d5952027e3)

```
ChatUser                    ChatConversation                  ChatMessage
user_name: "alice"          conversation_id: "conv-a1"        message_id: "msg-001"
    |                       name: "Q3 revenue"                role: "user"
    |                       create_epoch: 1721390000          content: "What drove Q3 revenue?"
    |                       update_epoch: 1721390042          seq: 1
    |                       deleted_epoch: 0                  create_epoch: 1721390000
    |                              |                                  ^
    +---OWNS_CONVERSATION--------->+                                  |
                                   |                                  |
                                   +---HAS_MESSAGE------------------->+
                                   |                                  |
                                   |                            REPLIES_TO
                                   |                                  |
                                   |                          ChatMessage
                                   |                          message_id: "msg-002"
                                   +---HAS_MESSAGE----------> role: "system"
                                                              content: "Q3 revenue rose 12%..."
                                                              model_name: "gpt-4o"
                                                              response_time: 4.2
                                                              seq: 2
                                                              feedback: 1
                                                                      |
                                                                  HAS_TRACE
                                                                      |
                                                                      v
                                                              ChatTrace
                                                              trace_id: "msg-002"
                                                              user_query: "What drove Q3 revenue?"
                                                              response_type: "agentic"
                                                              answered_question: true
                                                              tokens_in: 3180
                                                              tokens_out: 412
                                                              response_time: 4.2
                                                              create_epoch: 1721390042
                                                                 |        |
                                    +----------------------------+        +--RETRIEVED-->+
                                    |                                                    |
                                 HAS_STEP                                                |
                                    |                                                    v
                    +---------------+---------------+                          DocumentChunk
                    |                               |                          id: "doc7_chunk_3"
                    v                               v                          (EXISTING corpus
            ChatTraceStep                   ChatTraceStep                       vertex, not
            step_id: "msg-002:0"            step_id: "msg-002:1"                created here)
            name: "hybrid_search"           name: "answer"                            ^
            idx: 0                          idx: 1                                    |
            duration_ms: 1840               duration_ms: 2100                         |
            status: "ok"                    status: "ok"                              |
                    |                               ^                                 |
                    +--------NEXT_STEP------------->+                                 |
                    |                                                                 |
                    +----------------------RETRIEVED-------------------------------->+


ChatUser                    ChatConversation                  ChatMessage
user_name: "bob"            conversation_id: "conv-b1"        message_id: "msg-101"
    |                       name: "Headcount"                 role: "user"
    +---OWNS_CONVERSATION-->+---HAS_MESSAGE----------------->  content: "Headcount by region?"
                                                               seq: 1

        ^^^ Bob's subgraph is completely disjoint from Alice's.
            There is no edge between them. The ONLY shared vertices are
            corpus types (DocumentChunk / Entity / Community), which both
            users' traces may point at via RETRIEVED.
```

Two things to read off this:

- **Isolation is structural on the read path.** Every query seeds at one
  `ChatUser` and walks outward. Bob's data is not filtered out of Alice's
  results; it was never in the traversal.
- **The shared corpus is the one join point.** Both users' traces can point at
  `doc7_chunk_3`. That shared vertex is also the theoretical path *between* their
  subgraphs, which is why reverse traversal matters. See
  [03-access-control.md](03-access-control.md).

---

## Installation

Applied by `init_supportai` during graph initialization, in this order:

1. Corpus schema (`Document`, `DocumentChunk`, `Entity`, `Community`, and so on)
2. **Chat-history schema** (`add_chat_history_schema`)
3. All queries, including the eight `Chat_*` queries

Order is load-bearing: `RETRIEVED` declares pairs to `DocumentChunk`, `Entity`,
and `Community`, so those types must exist before the job runs.

Idempotent. The step is skipped if `"- VERTEX ChatConversation"` already appears
in the graph's `ls` output, matching the pattern already used for the corpus
schema and vector index.

## Installed queries

| Query | Scope | Seeds from |
|---|---|---|
| `Chat_List_Conversations` | One user | `ChatUser` |
| `Chat_Get_Conversation` | One user | `ChatUser` |
| `Chat_Get_Trace` | One user | `ChatUser` |
| `Chat_Search_My_Messages` | One user | `ChatUser` |
| `Chat_Get_Feedback` | One user | `ChatUser` |
| `Chat_Delete_Conversation` | One user | `ChatUser` |
| `Chat_Get_All_Feedback` | **All users**, admin only | `ChatUser.*` |
| `Chat_Expire_Traces` | **All traces**, maintenance | `ChatTrace.*` |

The two unscoped queries are deliberately separate queries rather than a flag on
a scoped one. A flag would put "return everyone's data" one wrong argument away.
Neither is reachable by the agent (see [03-access-control.md](03-access-control.md)).
